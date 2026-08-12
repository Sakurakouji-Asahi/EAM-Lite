from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

import pytest

from apps.finance.domain import (
    METHOD_DOUBLE_DECLINING_BALANCE,
    METHOD_MANUAL,
    METHOD_NO_DEPRECIATION,
    METHOD_STRAIGHT_LINE,
    METHOD_SUM_OF_YEARS_DIGITS,
    METHOD_UNITS_OF_PRODUCTION,
    POSTING_MONTHLY,
    POSTING_YEARLY,
    SALVAGE_AMOUNT,
    SALVAGE_RATE,
    START_CURRENT_MONTH,
    START_NEXT_MONTH,
    START_SPECIFIED_DATE,
    START_SPECIFIED_MONTH,
    STOP_EVENT_DATE,
    STOP_NEXT_MONTH,
    DepreciationError,
    ManualAmount,
    Period,
    ScheduleInput,
    active_intervals,
    add_months_safe,
    aggregate_actual_balances,
    calculate_life_end,
    calculate_salvage,
    double_declining_candidates,
    eligible_fraction,
    generate_schedule,
    money,
    month_period,
    post_depreciation,
    resolve_start_date,
    resolve_stop_date,
    sum_of_years_digits_annual_raw,
    units_of_production_raw,
    validate_opening_balances,
    year_period,
)


def monthly_spec(method=METHOD_STRAIGHT_LINE, **overrides):
    values = {
        "original_cost": "12000.00",
        "method": method,
        "posting_period": POSTING_MONTHLY,
        "commissioning_date": date(2025, 12, 15),
        "start_rule": START_NEXT_MONTH,
        "useful_life_months": 60,
        "salvage_mode": SALVAGE_RATE,
        "salvage_rate": "0.05",
    }
    values.update(overrides)
    return ScheduleInput(**values)


def period_starts(start, count):
    return [add_months_safe(start, index) for index in range(count)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", Decimal("0.00")),
        ("0.004", Decimal("0.00")),
        ("0.005", Decimal("0.01")),
        ("-0.005", Decimal("-0.01")),
        (Decimal("263.888888"), Decimal("263.89")),
    ],
)
def test_money_uses_exact_round_half_up(value, expected):
    assert money(value) == expected


def test_public_calculations_enforce_minimum_precision_independent_of_caller():
    with localcontext() as context:
        context.prec = 6
        result = generate_schedule(
            monthly_spec(
                original_cost="10000",
                useful_life_months=36,
                salvage_mode=SALVAGE_AMOUNT,
                salvage_rate=None,
                salvage_amount="500",
            )
        )
    assert result.lines[0].calculated_unrounded > Decimal("263.8888")
    assert result.lines[-1].planned_amount == Decimal("263.85")
    assert result.planned_total == Decimal("9500.00")


@pytest.mark.parametrize("value", [0.1, float("inf"), float("nan"), True])
def test_financial_inputs_never_accept_float_or_bool(value):
    with pytest.raises(DepreciationError, match="禁止 float|有效 Decimal"):
        money(value)


def test_salvage_rate_and_amount_are_mutually_exclusive_and_bounded():
    assert calculate_salvage(
        original_cost="12000", salvage_mode=SALVAGE_RATE, salvage_rate="0.05"
    ) == Decimal("600.00")
    assert calculate_salvage(
        original_cost="10000", salvage_mode=SALVAGE_AMOUNT, salvage_amount="500"
    ) == Decimal("500.00")

    with pytest.raises(DepreciationError, match="仅填写 salvage_rate"):
        calculate_salvage(
            original_cost="100",
            salvage_mode=SALVAGE_RATE,
            salvage_rate="0.05",
            salvage_amount="5",
        )
    with pytest.raises(DepreciationError, match="不得超过原值"):
        calculate_salvage(
            original_cost="100", salvage_mode=SALVAGE_AMOUNT, salvage_amount="100.01"
        )


def test_safe_month_arithmetic_start_rules_and_stop_rules():
    assert add_months_safe(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months_safe(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months_safe(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert month_period(date(2024, 2, 29)) == Period(
        date(2024, 2, 1), date(2024, 3, 1)
    )
    assert year_period(date(2024, 2, 29)).end == date(2025, 2, 28)

    commissioning = date(2026, 8, 16)
    assert resolve_start_date(
        commissioning_date=commissioning, start_rule=START_CURRENT_MONTH
    ) == date(2026, 8, 1)
    assert resolve_start_date(
        commissioning_date=commissioning, start_rule=START_NEXT_MONTH
    ) == date(2026, 9, 1)
    assert resolve_start_date(
        commissioning_date=commissioning,
        start_rule=START_SPECIFIED_MONTH,
        specified_start=date(2026, 10, 23),
    ) == date(2026, 10, 1)
    assert resolve_start_date(
        commissioning_date=commissioning,
        start_rule=START_SPECIFIED_DATE,
        specified_start=date(2026, 10, 23),
    ) == date(2026, 10, 23)
    with pytest.raises(DepreciationError, match="不得早于"):
        resolve_start_date(
            commissioning_date=commissioning,
            start_rule=START_SPECIFIED_DATE,
            specified_start=date(2026, 8, 15),
        )
    assert resolve_start_date(
        commissioning_date=commissioning,
        start_rule=START_SPECIFIED_DATE,
        specified_start=date(2020, 1, 1),
        allow_historical_override=True,
    ) == date(2020, 1, 1)

    assert resolve_stop_date(
        event_date=date(2026, 8, 16), stop_rule=STOP_EVENT_DATE
    ) == date(2026, 8, 16)
    assert resolve_stop_date(
        event_date=date(2026, 8, 16), stop_rule=STOP_NEXT_MONTH
    ) == date(2026, 9, 1)


def test_eligible_fraction_merges_overlaps_and_uses_real_calendar_days():
    august = Period(date(2026, 8, 1), date(2026, 9, 1))
    assert eligible_fraction(
        august, [Period(date(2026, 8, 16), date(2026, 9, 1))]
    ) == Decimal(16) / Decimal(31)
    # Overlap on 6-10 is counted once: eligible dates are 1-15 inclusive.
    assert eligible_fraction(
        august,
        [
            Period(date(2026, 8, 1), date(2026, 8, 11)),
            Period(date(2026, 8, 6), date(2026, 8, 16)),
        ],
    ) == Decimal(15) / Decimal(31)
    november = Period(date(2026, 11, 1), date(2026, 12, 1))
    assert eligible_fraction(
        november, [Period(date(2026, 11, 1), date(2026, 11, 11))]
    ) == Decimal(10) / Decimal(30)


def test_pause_removes_days_and_extends_life_by_actual_calendar_days():
    pause = Period(date(2026, 1, 11), date(2026, 1, 21))
    end = calculate_life_end(
        start_date=date(2026, 1, 1), useful_life_months=1, suspensions=[pause]
    )
    assert end == date(2026, 2, 11)
    assert active_intervals(
        start_date=date(2026, 1, 1), end_date=end, suspensions=[pause]
    ) == (
        Period(date(2026, 1, 1), date(2026, 1, 11)),
        Period(date(2026, 1, 21), date(2026, 2, 11)),
    )

    # A pause beginning before the original end extends by its complete
    # remaining duration; the portion beyond the old end is paused too.
    crossing = Period(date(2026, 1, 20), date(2026, 2, 10))
    assert calculate_life_end(
        start_date=date(2026, 1, 1), useful_life_months=1, suspensions=[crossing]
    ) == date(2026, 2, 22)

    # A historical suspension ending on or before the profile start has no
    # overlap with this useful life and must not shorten its natural end.
    before_start = Period(date(2025, 11, 1), date(2025, 12, 1))
    assert calculate_life_end(
        start_date=date(2026, 1, 1),
        useful_life_months=1,
        suspensions=[before_start],
    ) == date(2026, 2, 1)


def test_straight_line_normative_12000_example():
    result = generate_schedule(monthly_spec())
    assert result.salvage_value == Decimal("600.00")
    assert result.depreciable_amount == Decimal("11400.00")
    assert len(result.lines) == 60
    assert {line.planned_amount for line in result.lines} == {Decimal("190.00")}
    assert result.planned_total == Decimal("11400.00")
    assert result.lines[-1].closing_book_value == Decimal("600.00")


def test_straight_line_36_month_tail_is_exactly_263_85():
    result = generate_schedule(
        monthly_spec(
            original_cost="10000",
            useful_life_months=36,
            salvage_mode=SALVAGE_AMOUNT,
            salvage_rate=None,
            salvage_amount="500",
        )
    )
    assert [line.planned_amount for line in result.lines[:35]] == [
        Decimal("263.89")
    ] * 35
    assert result.lines[35].planned_amount == Decimal("263.85")
    assert result.lines[35].formula_snapshot["final_period_correction"] is True
    assert result.planned_total == Decimal("9500.00")
    assert result.lines[-1].closing_book_value == Decimal("500.00")


def test_specified_date_prorates_first_and_tail_month_without_losing_total():
    result = generate_schedule(
        monthly_spec(
            commissioning_date=date(2026, 8, 16),
            start_rule=START_SPECIFIED_DATE,
            specified_start=date(2026, 8, 16),
        )
    )
    assert len(result.lines) == 61
    assert result.lines[0].eligible_fraction == Decimal(16) / Decimal(31)
    assert abs(
        result.lines[0].calculated_unrounded
        - Decimal("190") * Decimal(16) / Decimal(31)
    ) < Decimal("1e-25")
    assert result.lines[0].planned_amount == Decimal("98.06")
    assert result.lines[-1].eligible_fraction == Decimal(15) / Decimal(31)
    assert result.planned_total == Decimal("11400.00")
    assert result.lines[-1].closing_book_value == Decimal("600.00")


def test_yearly_straight_line_requires_integer_years_and_uses_annual_amount():
    result = generate_schedule(
        monthly_spec(posting_period=POSTING_YEARLY, annual_posting_month=12)
    )
    assert [line.planned_amount for line in result.lines] == [
        Decimal("2280.00")
    ] * 5
    assert result.planned_total == Decimal("11400.00")

    with pytest.raises(DepreciationError, match="可被 12 整除"):
        generate_schedule(
            monthly_spec(
                posting_period=POSTING_YEARLY,
                annual_posting_month=12,
                useful_life_months=61,
            )
        )


def test_double_declining_balance_normative_switch_is_locked_from_month_36():
    result = generate_schedule(monthly_spec(METHOD_DOUBLE_DECLINING_BALANCE))
    expected = {
        1: ("double_declining_balance", Decimal("400.00"), Decimal("11600.00")),
        2: ("double_declining_balance", Decimal("386.67"), Decimal("11213.33")),
        3: ("double_declining_balance", Decimal("373.78"), Decimal("10839.55")),
        36: ("straight_line", Decimal("122.53"), Decimal("3540.72")),
        60: ("straight_line", Decimal("122.53"), Decimal("600.00")),
    }
    for sequence, (method, amount, closing) in expected.items():
        line = result.lines[sequence - 1]
        assert (line.method_applied, line.planned_amount, line.closing_book_value) == (
            method,
            amount,
            closing,
        )
    assert all(
        line.method_applied == METHOD_STRAIGHT_LINE for line in result.lines[35:]
    )

    # Once selected, straight line cannot switch back even if a later isolated
    # candidate would otherwise favour DDB.
    candidate = double_declining_candidates(
        book_value_before="1000",
        depreciable_balance_before="900",
        useful_life_periods="60",
        remaining_useful_periods="60",
        already_switched=True,
    )
    assert candidate.use_straight_line is True
    assert candidate.selected_raw == candidate.straight_line_raw


def test_sum_of_years_digits_locks_annual_targets_and_monthly_tail():
    assert sum_of_years_digits_annual_raw(
        depreciable_amount="11400", useful_life_years=5, depreciation_year=1
    ) == Decimal("3800")
    result = generate_schedule(monthly_spec(METHOD_SUM_OF_YEARS_DIGITS))
    assert [line.planned_amount for line in result.lines[:11]] == [
        Decimal("316.67")
    ] * 11
    assert result.lines[11].planned_amount == Decimal("316.63")
    assert sum(line.planned_amount for line in result.lines[:12]) == Decimal(
        "3800.00"
    )
    assert sum(line.planned_amount for line in result.lines[12:24]) == Decimal(
        "3040.00"
    )
    assert result.planned_total == Decimal("11400.00")
    assert result.lines[-1].closing_book_value == Decimal("600.00")


def test_sum_of_years_digits_midyear_stop_does_not_force_full_annual_target():
    result = generate_schedule(
        monthly_spec(
            METHOD_SUM_OF_YEARS_DIGITS,
            stop_date=date(2026, 6, 16),
        )
    )
    assert result.lines[-1].eligible_fraction == Decimal("0.5")
    assert result.lines[-1].planned_amount == Decimal("158.33")
    assert result.lines[-1].formula_snapshot["annual_target_correction"] is False
    assert result.planned_total == Decimal("1741.68")


def test_yearly_sum_of_years_digits_uses_one_line_per_depreciation_year():
    result = generate_schedule(
        monthly_spec(
            METHOD_SUM_OF_YEARS_DIGITS,
            posting_period=POSTING_YEARLY,
            annual_posting_month=12,
        )
    )
    assert [line.planned_amount for line in result.lines] == [
        Decimal("3800.00"),
        Decimal("3040.00"),
        Decimal("2280.00"),
        Decimal("1520.00"),
        Decimal("760.00"),
    ]


def test_units_of_production_normative_amount_explicit_zero_and_cap():
    assert units_of_production_raw(
        depreciable_amount="10000",
        expected_total_units="100000",
        current_units="8000",
    ) == Decimal("800")
    spec = monthly_spec(
        METHOD_UNITS_OF_PRODUCTION,
        original_cost="10500",
        salvage_mode=SALVAGE_AMOUNT,
        salvage_rate=None,
        salvage_amount="500",
        expected_total_units="100000",
        work_unit="小时",
        stop_date=date(2026, 2, 1),
    )
    result = generate_schedule(spec, usage_by_period={date(2026, 1, 1): "8000"})
    assert result.lines[0].planned_units == Decimal("8000")
    assert result.lines[0].planned_amount == Decimal("800.00")

    zero = generate_schedule(spec, usage_by_period={date(2026, 1, 1): "0"})
    assert zero.lines[0].planned_amount == Decimal("0.00")
    with pytest.raises(DepreciationError, match="缺少明确的当期工作量"):
        generate_schedule(spec)

    capped = generate_schedule(spec, usage_by_period={date(2026, 1, 1): "110000"})
    assert capped.lines[0].planned_units == Decimal("100000")
    assert capped.lines[0].planned_amount == Decimal("10000.00")
    assert capped.lines[0].formula_snapshot["units_capped"] is True


def test_manual_schedule_requires_structured_amount_and_reason_even_for_zero():
    spec = monthly_spec(METHOD_MANUAL, stop_date=date(2026, 2, 1))
    result = generate_schedule(
        spec,
        manual_by_period={
            date(2026, 1, 1): ManualAmount(amount="0", reason="本期停产")
        },
    )
    assert result.lines[0].planned_amount == Decimal("0.00")
    assert result.lines[0].formula_snapshot["manual_reason"] == "本期停产"

    with pytest.raises(DepreciationError, match="必须填写原因"):
        generate_schedule(
            spec,
            manual_by_period={date(2026, 1, 1): ManualAmount("0", "")},
        )
    with pytest.raises(DepreciationError, match="不得超过剩余"):
        generate_schedule(
            spec,
            manual_by_period={date(2026, 1, 1): ManualAmount("11400.01", "更正")},
        )


def test_no_depreciation_makes_zero_schedule_and_no_actual_entry_amount():
    result = generate_schedule(
        monthly_spec(METHOD_NO_DEPRECIATION, stop_date=date(2026, 2, 1))
    )
    assert len(result.lines) == 1
    assert result.lines[0].planned_amount == Decimal("0.00")
    assert result.lines[0].closing_book_value == Decimal("12000.00")
    assert result.planned_total == Decimal("0.00")


def test_posting_floor_and_final_period_correction_have_common_precedence():
    assert post_depreciation(
        calculated_unrounded="100.005", depreciable_balance_before="20.00"
    ) == Decimal("20.00")
    assert post_depreciation(
        calculated_unrounded="1.00",
        depreciable_balance_before="20.00",
        final_period=True,
    ) == Decimal("20.00")
    assert post_depreciation(
        calculated_unrounded="-10", depreciable_balance_before="20.00"
    ) == Decimal("0.00")


def test_opening_actual_and_theoretical_values_are_separate_and_exactly_reconciled():
    assert validate_opening_balances(
        original_cost="10000",
        opening_actual_accumulated_depreciation="2500",
        opening_impairment="500",
        opening_book_value="7000.01",
    ) == Decimal("7000.00")
    with pytest.raises(DepreciationError, match="差异超过 0.01"):
        validate_opening_balances(
            original_cost="10000",
            opening_actual_accumulated_depreciation="2500",
            opening_impairment="500",
            opening_book_value="7000.02",
        )

    result = generate_schedule(
        monthly_spec(
            opening_actual_accumulated_depreciation="2500",
            opening_impairment="500",
            opening_book_value="9000",
        )
    )
    # 12,000 - 2,500 - 500 = 9,000; the actual opening amount stays visible in
    # planned_accumulated instead of being overwritten by a theoretical value.
    assert result.opening_book_value == Decimal("9000.00")
    assert result.lines[0].planned_accumulated == Decimal("2640.00")


def test_actual_balance_uses_algebraic_sum_of_original_adjustment_and_reversal_entries():
    balances = aggregate_actual_balances(
        original_cost="1000",
        depreciation_entries=["100", "50", "-50"],
        opening_impairments=["20"],
        impairments=["20"],
        impairment_reversals=["10"],
    )
    assert balances.accumulated_depreciation == Decimal("100.00")
    assert balances.impairment_balance == Decimal("30.00")
    assert balances.book_value == Decimal("870.00")

    with pytest.raises(DepreciationError, match="累计折旧代数和不得小于 0"):
        aggregate_actual_balances(
            original_cost="1000", depreciation_entries=["100", "-100.01"]
        )
    with pytest.raises(DepreciationError, match="累计减值小于 0"):
        aggregate_actual_balances(
            original_cost="1000", impairment_reversals=["0.01"]
        )


def test_machine_tokens_and_period_field_combinations_are_strict():
    with pytest.raises(DepreciationError, match="posting_period"):
        generate_schedule(monthly_spec(posting_period="month"))
    with pytest.raises(DepreciationError, match="salvage_mode"):
        generate_schedule(monthly_spec(salvage_mode="percentage"))
    with pytest.raises(DepreciationError, match="annual_posting_month"):
        generate_schedule(monthly_spec(annual_posting_month=12))
    with pytest.raises(DepreciationError, match="1..12"):
        generate_schedule(
            monthly_spec(posting_period=POSTING_YEARLY, annual_posting_month=None)
        )


def test_schedule_is_pure_and_reproducible_for_equal_inputs():
    specification = monthly_spec()
    first = generate_schedule(specification)
    second = generate_schedule(specification)
    assert first == second
    assert isinstance(first.lines, tuple)
