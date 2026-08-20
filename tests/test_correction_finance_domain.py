from datetime import date
from decimal import Decimal

from apps.finance.domain import ScheduleInput, generate_schedule


def _midyear_yearly_spec(method):
    return ScheduleInput(
        original_cost=Decimal("12000.00"),
        method=method,
        posting_period="yearly",
        commissioning_date=date(2026, 7, 1),
        start_rule="specified_date",
        specified_start=date(2026, 7, 1),
        useful_life_months=60,
        salvage_mode="rate",
        salvage_rate=Decimal("0.05"),
        annual_posting_month=12,
    )


def test_yearly_straight_line_uses_annual_posting_month_accounting_window():
    result = generate_schedule(
        _midyear_yearly_spec("straight_line")
    )

    first = result.lines[0]
    assert (first.period_start, first.period_end) == (
        date(2026, 1, 1),
        date(2027, 1, 1),
    )
    assert first.eligible_fraction == Decimal(184) / Decimal(365)
    assert first.planned_amount == Decimal("1149.37")
    assert result.planned_total == Decimal("11400.00")
    assert result.lines[-1].closing_book_value == Decimal("600.00")


def test_yearly_syd_keeps_depreciation_year_weights_across_posting_windows():
    result = generate_schedule(_midyear_yearly_spec("sum_of_years_digits"))

    assert [line.planned_amount for line in result.lines[:2]] == [
        Decimal("1915.62"),
        Decimal("3412.69"),
    ]
    assert [
        item["depreciation_year"]
        for item in result.lines[1].formula_snapshot["annual_components"]
    ] == [1, 2]
    assert result.planned_total == Decimal("11400.00")
    assert result.lines[-1].closing_book_value == Decimal("600.00")


def test_yearly_ddb_does_not_compound_early_at_accounting_window_boundary():
    result = generate_schedule(_midyear_yearly_spec("double_declining_balance"))

    assert [line.planned_amount for line in result.lines[:2]] == [
        Decimal("2419.73"),
        Decimal("3828.14"),
    ]
    assert [
        item["depreciation_year"]
        for item in result.lines[1].formula_snapshot["annual_components"]
    ] == [1, 2]
    assert result.planned_total == Decimal("11400.00")
    assert result.lines[-1].closing_book_value == Decimal("600.00")


def test_old_asset_continues_after_opening_cutoff_but_keeps_original_life_end():
    result = generate_schedule(
        ScheduleInput(
            original_cost=Decimal("12000.00"),
            method="straight_line",
            posting_period="monthly",
            commissioning_date=date(2024, 1, 1),
            start_rule="specified_date",
            specified_start=date(2024, 1, 1),
            useful_life_months=60,
            salvage_mode="rate",
            salvage_rate=Decimal("0.05"),
            opening_actual_accumulated_depreciation=Decimal("4560.00"),
            opening_book_value=Decimal("7440.00"),
            actual_continuation_date=date(2026, 1, 1),
        )
    )

    assert result.start_date == date(2024, 1, 1)
    assert result.actual_continuation_date == date(2026, 1, 1)
    assert result.natural_end_date == date(2029, 1, 1)
    assert len(result.lines) == 36
    assert result.lines[0].period_start == date(2026, 1, 1)
    assert {line.planned_amount for line in result.lines} == {Decimal("190.00")}
    assert result.planned_total == Decimal("6840.00")
    assert result.lines[-1].closing_book_value == Decimal("600.00")
