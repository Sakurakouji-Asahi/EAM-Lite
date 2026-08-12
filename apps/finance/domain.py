"""Pure Decimal depreciation calculations for EAM-Lite Sprint 4.

This module deliberately has no Django or database dependency.  It implements
the normative calculation rules from ``docs/08-Depreciation-Calculation-Spec.md``
and leaves persistence, permissions, locking, idempotency and audit to the
finance service layer.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import wraps
from typing import Iterable, Mapping, Sequence


MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")

METHOD_STRAIGHT_LINE = "straight_line"
METHOD_UNITS_OF_PRODUCTION = "units_of_production"
METHOD_DOUBLE_DECLINING_BALANCE = "double_declining_balance"
METHOD_SUM_OF_YEARS_DIGITS = "sum_of_years_digits"
METHOD_MANUAL = "manual"
METHOD_NO_DEPRECIATION = "no_depreciation"
METHODS = frozenset(
    {
        METHOD_STRAIGHT_LINE,
        METHOD_UNITS_OF_PRODUCTION,
        METHOD_DOUBLE_DECLINING_BALANCE,
        METHOD_SUM_OF_YEARS_DIGITS,
        METHOD_MANUAL,
        METHOD_NO_DEPRECIATION,
    }
)

POSTING_MONTHLY = "monthly"
POSTING_YEARLY = "yearly"
POSTING_PERIODS = frozenset({POSTING_MONTHLY, POSTING_YEARLY})

SALVAGE_RATE = "rate"
SALVAGE_AMOUNT = "amount"
SALVAGE_MODES = frozenset({SALVAGE_RATE, SALVAGE_AMOUNT})

START_CURRENT_MONTH = "current_month"
START_NEXT_MONTH = "next_month"
START_SPECIFIED_MONTH = "specified_month"
START_SPECIFIED_DATE = "specified_date"
START_RULES = frozenset(
    {
        START_CURRENT_MONTH,
        START_NEXT_MONTH,
        START_SPECIFIED_MONTH,
        START_SPECIFIED_DATE,
    }
)

STOP_EVENT_DATE = "event_date"
STOP_NEXT_MONTH = "next_month"
STOP_RULES = frozenset({STOP_EVENT_DATE, STOP_NEXT_MONTH})


class DepreciationError(ValueError):
    """Raised when a calculation input violates the normative specification."""


DecimalInput = Decimal | int | str


def _decimal_calculation(function):
    """Run one public calculation with the normative minimum precision."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 28
            return function(*args, **kwargs)

    return wrapped


def decimal_value(value: DecimalInput, *, field_name: str = "value") -> Decimal:
    """Return an exact finite Decimal and reject every binary-float path."""

    if isinstance(value, bool) or isinstance(value, float):
        raise DepreciationError(f"{field_name} 必须由 Decimal、整数或十进制字符串提供，禁止 float。")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DepreciationError(f"{field_name} 不是有效 Decimal。") from exc
    if not result.is_finite():
        raise DepreciationError(f"{field_name} 必须是有限 Decimal。")
    return result


@_decimal_calculation
def money(value: DecimalInput) -> Decimal:
    """Quantize a monetary value to two decimals using ``ROUND_HALF_UP``."""

    with localcontext() as context:
        context.prec = 28
        return decimal_value(value, field_name="金额").quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )


def _nonnegative_decimal(value: DecimalInput, *, field_name: str) -> Decimal:
    result = decimal_value(value, field_name=field_name)
    if result < ZERO:
        raise DepreciationError(f"{field_name} 不得小于 0。")
    return result


def _nonnegative_money(value: DecimalInput, *, field_name: str) -> Decimal:
    result = money(value)
    if result < ZERO:
        raise DepreciationError(f"{field_name} 不得小于 0。")
    return result


def _require_date(value: date, *, field_name: str) -> date:
    # datetime is a date subclass, but accepting it would silently discard a
    # timezone and violate the explicit Shanghai-business-date boundary.
    if type(value) is not date:
        raise DepreciationError(f"{field_name} 必须是明确的业务日期 date。")
    return value


@dataclass(frozen=True, order=True)
class Period:
    """A half-open date interval ``[start, end)``."""

    start: date
    end: date

    def __post_init__(self) -> None:
        _require_date(self.start, field_name="期间开始日")
        _require_date(self.end, field_name="期间结束日")
        if self.end <= self.start:
            raise DepreciationError("期间必须满足 end > start，并按半开区间解释。")

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def add_months_safe(value: date, months: int) -> date:
    """Add calendar months, clamping a missing target day to month end."""

    value = _require_date(value, field_name="日期")
    if isinstance(months, bool) or not isinstance(months, int):
        raise DepreciationError("加月数必须是整数。")
    month_index = value.year * 12 + value.month - 1 + months
    if month_index < 12:
        raise DepreciationError("加月结果超出支持的日期范围。")
    target_year, zero_based_month = divmod(month_index, 12)
    target_month = zero_based_month + 1
    try:
        target_day = min(value.day, monthrange(target_year, target_month)[1])
        return date(target_year, target_month, target_day)
    except (ValueError, OverflowError) as exc:
        raise DepreciationError("加月结果超出支持的日期范围。") from exc


def month_period(value: date) -> Period:
    """Return the natural-month period containing ``value``."""

    value = _require_date(value, field_name="日期")
    start = value.replace(day=1)
    return Period(start, add_months_safe(start, 1))


def depreciation_year_period(start_date: date, year_index: int) -> Period:
    """Return a 12-month depreciation year, indexed from zero."""

    start_date = _require_date(start_date, field_name="折旧起点")
    if isinstance(year_index, bool) or not isinstance(year_index, int) or year_index < 0:
        raise DepreciationError("折旧年度序号必须是非负整数。")
    start = add_months_safe(start_date, 12 * year_index)
    return Period(start, add_months_safe(start_date, 12 * (year_index + 1)))


def year_period(start_date: date, year_index: int = 0) -> Period:
    """Public concise alias for :func:`depreciation_year_period`."""

    return depreciation_year_period(start_date, year_index)


def resolve_start_date(
    *,
    commissioning_date: date,
    start_rule: str,
    specified_start: date | None = None,
    allow_historical_override: bool = False,
) -> date:
    """Resolve the depreciation qualification start from an approved token."""

    commissioning_date = _require_date(
        commissioning_date, field_name="达到可使用状态日期"
    )
    if start_rule not in START_RULES:
        raise DepreciationError("未知折旧起算规则。")
    if start_rule == START_CURRENT_MONTH:
        if specified_start is not None:
            raise DepreciationError("current_month 不得同时填写指定起算值。")
        return commissioning_date.replace(day=1)
    if start_rule == START_NEXT_MONTH:
        if specified_start is not None:
            raise DepreciationError("next_month 不得同时填写指定起算值。")
        return add_months_safe(commissioning_date.replace(day=1), 1)
    if specified_start is None:
        raise DepreciationError("指定月份/日期起算必须填写 specified_start。")
    specified_start = _require_date(specified_start, field_name="指定起算值")
    result = (
        specified_start.replace(day=1)
        if start_rule == START_SPECIFIED_MONTH
        else specified_start
    )
    if not allow_historical_override and result < commissioning_date:
        raise DepreciationError("指定起算值不得早于达到可使用状态日期。")
    return result


def resolve_stop_date(*, event_date: date, stop_rule: str) -> date:
    """Resolve an event into the exclusive end of depreciation eligibility."""

    event_date = _require_date(event_date, field_name="停止/处置事件日期")
    if stop_rule == STOP_EVENT_DATE:
        return event_date
    if stop_rule == STOP_NEXT_MONTH:
        return add_months_safe(event_date.replace(day=1), 1)
    raise DepreciationError("stop_rule 只能是 event_date 或 next_month。")


@_decimal_calculation
def calculate_salvage(
    *,
    original_cost: DecimalInput,
    salvage_mode: str,
    salvage_rate: DecimalInput | None = None,
    salvage_amount: DecimalInput | None = None,
) -> Decimal:
    """Resolve and validate the current salvage amount."""

    cost = _nonnegative_money(original_cost, field_name="原值")
    if salvage_mode not in SALVAGE_MODES:
        raise DepreciationError("salvage_mode 只能是 rate 或 amount。")
    if salvage_mode == SALVAGE_RATE:
        if salvage_rate is None or salvage_amount is not None:
            raise DepreciationError("rate 模式必须仅填写 salvage_rate。")
        rate = decimal_value(salvage_rate, field_name="残值率")
        if not ZERO <= rate <= ONE:
            raise DepreciationError("残值率必须位于 0 到 1 之间。")
        result = money(cost * rate)
    else:
        if salvage_amount is None or salvage_rate is not None:
            raise DepreciationError("amount 模式必须仅填写 salvage_amount。")
        result = _nonnegative_money(salvage_amount, field_name="固定残值金额")
    if result > cost:
        raise DepreciationError("残值金额不得超过原值。")
    return result


def _coerce_period(value: Period | tuple[date, date]) -> Period:
    if isinstance(value, Period):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return Period(value[0], value[1])
    raise DepreciationError("资格/暂停区间必须是 Period 或二元日期 tuple。")


def merge_periods(periods: Iterable[Period | tuple[date, date]]) -> tuple[Period, ...]:
    """Return the union of overlapping or adjacent half-open periods."""

    ordered = sorted(_coerce_period(item) for item in periods)
    if not ordered:
        return ()
    merged: list[Period] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end:
            merged[-1] = Period(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return tuple(merged)


def _intersection(first: Period, second: Period) -> Period | None:
    start = max(first.start, second.start)
    end = min(first.end, second.end)
    return Period(start, end) if start < end else None


def eligible_days(
    period: Period,
    eligibility_intervals: Iterable[Period | tuple[date, date]],
) -> int:
    """Count distinct eligible calendar days inside ``period``."""

    total = 0
    for interval in merge_periods(eligibility_intervals):
        overlap = _intersection(period, interval)
        if overlap is not None:
            total += overlap.days
    return total


@_decimal_calculation
def eligible_fraction(
    period: Period,
    eligibility_intervals: Iterable[Period | tuple[date, date]],
) -> Decimal:
    """Return eligible calendar days divided by all calendar days in a period."""

    return Decimal(eligible_days(period, eligibility_intervals)) / Decimal(period.days)


def calculate_life_end(
    *,
    start_date: date,
    useful_life_months: int,
    suspensions: Iterable[Period | tuple[date, date]] = (),
) -> date:
    """Resolve life end and extend it by actual suspended calendar days."""

    start_date = _require_date(start_date, field_name="折旧起点")
    if (
        isinstance(useful_life_months, bool)
        or not isinstance(useful_life_months, int)
        or useful_life_months <= 0
    ):
        raise DepreciationError("使用寿命月数必须是正整数。")
    candidate = add_months_safe(start_date, useful_life_months)
    # Once a suspension starts before the tentative end, none of its calendar
    # days consume useful life, including the portion after that tentative end.
    # Adding the whole overlap in chronological order also makes later pauses
    # eligible when an earlier pause pushes the end past their start.
    for suspension in merge_periods(suspensions):
        if suspension.end <= start_date:
            continue
        effective_start = max(start_date, suspension.start)
        if effective_start < candidate:
            candidate += suspension.end - effective_start
    return candidate


def active_intervals(
    *,
    start_date: date,
    end_date: date,
    suspensions: Iterable[Period | tuple[date, date]] = (),
) -> tuple[Period, ...]:
    """Subtract suspended periods from one qualification interval."""

    start_date = _require_date(start_date, field_name="资格开始日")
    end_date = _require_date(end_date, field_name="资格结束日")
    if end_date <= start_date:
        return ()
    result: list[Period] = []
    cursor = start_date
    window = Period(start_date, end_date)
    for suspension in merge_periods(suspensions):
        overlap = _intersection(window, suspension)
        if overlap is None:
            continue
        if cursor < overlap.start:
            result.append(Period(cursor, overlap.start))
        cursor = max(cursor, overlap.end)
    if cursor < end_date:
        result.append(Period(cursor, end_date))
    return tuple(result)


@_decimal_calculation
def straight_line_raw(
    *,
    depreciable_amount: DecimalInput,
    useful_life_periods: DecimalInput,
    fraction: DecimalInput = ONE,
) -> Decimal:
    base = _nonnegative_decimal(depreciable_amount, field_name="可折旧金额")
    periods = decimal_value(useful_life_periods, field_name="使用寿命期间数")
    ratio = decimal_value(fraction, field_name="有资格比例")
    if periods <= ZERO:
        raise DepreciationError("使用寿命期间数必须大于 0。")
    if not ZERO <= ratio <= ONE:
        raise DepreciationError("有资格比例必须位于 0 到 1 之间。")
    return base / periods * ratio


@_decimal_calculation
def units_of_production_raw(
    *,
    depreciable_amount: DecimalInput,
    expected_total_units: DecimalInput,
    current_units: DecimalInput,
) -> Decimal:
    base = _nonnegative_decimal(depreciable_amount, field_name="可折旧金额")
    total = decimal_value(expected_total_units, field_name="预计总工作量")
    current = _nonnegative_decimal(current_units, field_name="当期工作量")
    if total <= ZERO:
        raise DepreciationError("预计总工作量必须大于 0。")
    return base / total * min(current, total)


@dataclass(frozen=True)
class DDBCandidates:
    ddb_raw: Decimal
    straight_line_raw: Decimal
    use_straight_line: bool
    selected_raw: Decimal


@_decimal_calculation
def double_declining_candidates(
    *,
    book_value_before: DecimalInput,
    depreciable_balance_before: DecimalInput,
    useful_life_periods: DecimalInput,
    remaining_useful_periods: DecimalInput,
    fraction: DecimalInput = ONE,
    already_switched: bool = False,
) -> DDBCandidates:
    book_value = _nonnegative_decimal(book_value_before, field_name="期初账面价值")
    depreciable_balance = _nonnegative_decimal(
        depreciable_balance_before, field_name="剩余可折旧金额"
    )
    life = decimal_value(useful_life_periods, field_name="使用寿命期间数")
    remaining = decimal_value(
        remaining_useful_periods, field_name="剩余使用寿命期间数"
    )
    ratio = decimal_value(fraction, field_name="有资格比例")
    if life <= ZERO or remaining <= ZERO:
        raise DepreciationError("使用寿命期间数和剩余期间数必须大于 0。")
    if not ZERO <= ratio <= ONE:
        raise DepreciationError("有资格比例必须位于 0 到 1 之间。")
    ddb_raw = book_value * Decimal(2) / life * ratio
    sl_raw = depreciable_balance / remaining * ratio
    use_straight_line = already_switched or sl_raw >= ddb_raw
    return DDBCandidates(
        ddb_raw=ddb_raw,
        straight_line_raw=sl_raw,
        use_straight_line=use_straight_line,
        selected_raw=sl_raw if use_straight_line else ddb_raw,
    )


@_decimal_calculation
def sum_of_years_digits_annual_raw(
    *,
    depreciable_amount: DecimalInput,
    useful_life_years: int,
    depreciation_year: int,
) -> Decimal:
    base = _nonnegative_decimal(depreciable_amount, field_name="可折旧金额")
    if (
        isinstance(useful_life_years, bool)
        or not isinstance(useful_life_years, int)
        or useful_life_years <= 0
    ):
        raise DepreciationError("使用寿命年数必须是正整数。")
    if (
        isinstance(depreciation_year, bool)
        or not isinstance(depreciation_year, int)
        or not 1 <= depreciation_year <= useful_life_years
    ):
        raise DepreciationError("折旧年度必须位于使用寿命范围内。")
    denominator = Decimal(useful_life_years * (useful_life_years + 1)) / Decimal(2)
    return (
        base
        * Decimal(useful_life_years - depreciation_year + 1)
        / denominator
    )


@_decimal_calculation
def post_depreciation(
    *,
    calculated_unrounded: DecimalInput,
    depreciable_balance_before: DecimalInput,
    final_period: bool = False,
) -> Decimal:
    """Apply nonnegative rounding, salvage floor and final-period correction."""

    raw = decimal_value(calculated_unrounded, field_name="未舍入折旧额")
    remaining = _nonnegative_money(
        depreciable_balance_before, field_name="剩余可折旧金额"
    )
    if final_period:
        return remaining
    return min(money(max(raw, ZERO)), remaining)


@dataclass(frozen=True)
class ManualAmount:
    amount: DecimalInput
    reason: str


@dataclass(frozen=True)
class ScheduleInput:
    original_cost: DecimalInput
    method: str
    posting_period: str
    commissioning_date: date
    start_rule: str
    useful_life_months: int
    salvage_mode: str = SALVAGE_RATE
    salvage_rate: DecimalInput | None = None
    salvage_amount: DecimalInput | None = None
    specified_start: date | None = None
    annual_posting_month: int | None = None
    opening_actual_accumulated_depreciation: DecimalInput = ZERO
    opening_impairment: DecimalInput = ZERO
    opening_book_value: DecimalInput | None = None
    expected_total_units: DecimalInput | None = None
    work_unit: str | None = None
    allow_historical_start: bool = False
    suspensions: tuple[Period, ...] = field(default_factory=tuple)
    stop_date: date | None = None


@dataclass(frozen=True)
class ScheduleLine:
    sequence_no: int
    period_start: date
    period_end: date
    opening_book_value: Decimal
    calculated_unrounded: Decimal
    planned_amount: Decimal
    planned_accumulated: Decimal
    closing_book_value: Decimal
    eligible_fraction: Decimal
    planned_units: Decimal | None
    method_applied: str
    formula_snapshot: Mapping[str, object]


@dataclass(frozen=True)
class ScheduleResult:
    original_cost: Decimal
    salvage_value: Decimal
    opening_book_value: Decimal
    depreciable_amount: Decimal
    start_date: date
    natural_end_date: date
    schedule_end_date: date
    lines: tuple[ScheduleLine, ...]

    @property
    def planned_total(self) -> Decimal:
        return money(sum((line.planned_amount for line in self.lines), ZERO))


@dataclass(frozen=True)
class ActualBalances:
    original_cost: Decimal
    accumulated_depreciation: Decimal
    impairment_balance: Decimal
    book_value: Decimal


@_decimal_calculation
def validate_opening_balances(
    *,
    original_cost: DecimalInput,
    opening_actual_accumulated_depreciation: DecimalInput,
    opening_impairment: DecimalInput,
    opening_book_value: DecimalInput,
    tolerance: DecimalInput = MONEY_QUANTUM,
) -> Decimal:
    """Validate the opening equation and return its canonical book value."""

    cost = _nonnegative_money(original_cost, field_name="原值")
    accumulated = _nonnegative_money(
        opening_actual_accumulated_depreciation, field_name="期初实际累计折旧"
    )
    impairment = _nonnegative_money(opening_impairment, field_name="期初减值")
    supplied = _nonnegative_money(opening_book_value, field_name="期初账面价值")
    allowed = _nonnegative_decimal(tolerance, field_name="勾稽容差")
    expected = money(cost - accumulated - impairment)
    if expected < ZERO:
        raise DepreciationError("期初累计折旧和减值不得使账面价值小于 0。")
    if abs(supplied - expected) > allowed:
        raise DepreciationError("期初账面价值与原值、累计折旧和减值勾稽差异超过 0.01。")
    return expected


@_decimal_calculation
def aggregate_actual_balances(
    *,
    original_cost: DecimalInput,
    depreciation_entries: Iterable[DecimalInput] = (),
    opening_impairments: Iterable[DecimalInput] = (),
    impairments: Iterable[DecimalInput] = (),
    impairment_reversals: Iterable[DecimalInput] = (),
) -> ActualBalances:
    """Aggregate posted original and reversal effects without dropping history."""

    cost = _nonnegative_money(original_cost, field_name="原值")
    accumulated = money(
        sum(
            (money(item) for item in depreciation_entries),
            ZERO,
        )
    )
    if accumulated < ZERO:
        raise DepreciationError("实际累计折旧代数和不得小于 0。")
    opening = sum(
        (
            _nonnegative_money(item, field_name="期初减值")
            for item in opening_impairments
        ),
        ZERO,
    )
    additions = sum(
        (_nonnegative_money(item, field_name="减值") for item in impairments), ZERO
    )
    reversals = sum(
        (
            _nonnegative_money(item, field_name="减值转回")
            for item in impairment_reversals
        ),
        ZERO,
    )
    impairment = money(opening + additions - reversals)
    if impairment < ZERO:
        raise DepreciationError("减值转回不得使累计减值小于 0。")
    book_value = money(cost - accumulated - impairment)
    if book_value < ZERO:
        raise DepreciationError("已确认分录不得使账面价值小于 0。")
    return ActualBalances(cost, accumulated, impairment, book_value)


def actual_balances(**kwargs) -> ActualBalances:
    """Public concise alias for :func:`aggregate_actual_balances`."""

    return aggregate_actual_balances(**kwargs)


def _schedule_periods(
    *, start_date: date, schedule_end: date, posting_period: str
) -> tuple[Period, ...]:
    if schedule_end <= start_date:
        return ()
    periods: list[Period] = []
    if posting_period == POSTING_MONTHLY:
        cursor = start_date.replace(day=1)
        while cursor < schedule_end:
            following = add_months_safe(cursor, 1)
            periods.append(Period(cursor, following))
            cursor = following
    else:
        index = 0
        while True:
            period = depreciation_year_period(start_date, index)
            if period.start >= schedule_end:
                break
            periods.append(period)
            index += 1
    return tuple(periods)


def _mapping_value(mapping: Mapping[date, object], period: Period, *, label: str):
    if period.start not in mapping:
        raise DepreciationError(f"{period.start.isoformat()} 缺少明确的{label}。")
    return mapping[period.start]


def _last_eligible_index(fractions: Sequence[Decimal]) -> int | None:
    for index in range(len(fractions) - 1, -1, -1):
        if fractions[index] > ZERO:
            return index
    return None


def _syd_monthly_allocations(
    *,
    periods: Sequence[Period],
    active: Sequence[Period],
    start_date: date,
    useful_life_years: int,
    depreciable_amount: Decimal,
    suspensions: Sequence[Period],
    completed_natural_life: bool,
) -> tuple[list[Decimal], list[Decimal], set[int]]:
    """Allocate rounded annual SYD targets over natural-month schedule lines."""

    raw_by_line = [ZERO for _ in periods]
    posted_by_line = [ZERO for _ in periods]
    corrected_lines: set[int] = set()
    target_posted_before = ZERO
    for year_index in range(useful_life_years):
        year_start = (
            start_date
            if year_index == 0
            else calculate_life_end(
                start_date=start_date,
                useful_life_months=12 * year_index,
                suspensions=suspensions,
            )
        )
        year_end = calculate_life_end(
            start_date=start_date,
            useful_life_months=12 * (year_index + 1),
            suspensions=suspensions,
        )
        year_window = Period(year_start, year_end)
        annual_raw = sum_of_years_digits_annual_raw(
            depreciable_amount=depreciable_amount,
            useful_life_years=useful_life_years,
            depreciation_year=year_index + 1,
        )
        annual_target = (
            money(depreciable_amount - target_posted_before)
            if year_index == useful_life_years - 1
            else money(annual_raw)
        )
        allocations: list[tuple[int, Decimal]] = []
        for line_index, period in enumerate(periods):
            overlap = _intersection(period, year_window)
            if overlap is None:
                continue
            days = eligible_days(overlap, active)
            if days == 0:
                continue
            fraction = Decimal(days) / Decimal(period.days)
            raw = annual_raw / Decimal(12) * fraction
            allocations.append((line_index, raw))
            raw_by_line[line_index] += raw
        if not allocations:
            continue
        # A natural-month row can extend past a mid-month stop.  Its database
        # period_end therefore cannot prove that the whole depreciation year
        # was eligible; only the active qualification interval can do that.
        full_year_reached = completed_natural_life or (
            bool(active) and active[-1].end >= year_end
        )
        year_posted = ZERO
        for allocation_index, (line_index, raw) in enumerate(allocations):
            if allocation_index == len(allocations) - 1 and full_year_reached:
                posted = money(annual_target - year_posted)
                corrected_lines.add(line_index)
            else:
                posted = money(raw)
            posted_by_line[line_index] += posted
            year_posted += posted
        if full_year_reached:
            target_posted_before += annual_target
    return raw_by_line, posted_by_line, corrected_lines


@_decimal_calculation
def generate_schedule(
    specification: ScheduleInput,
    *,
    usage_by_period: Mapping[date, DecimalInput] | None = None,
    manual_by_period: Mapping[date, ManualAmount] | None = None,
) -> ScheduleResult:
    """Generate a reproducible theoretical schedule for all six V1 methods.

    Mapping keys are schedule ``period_start`` dates.  Work-usage zero is valid
    only when the key is explicitly present.  Manual zero likewise requires a
    present :class:`ManualAmount` with a nonblank reason.
    """

    if not isinstance(specification, ScheduleInput):
        raise DepreciationError("specification 必须是 ScheduleInput。")
    if specification.method not in METHODS:
        raise DepreciationError("未知折旧方法。")
    if specification.posting_period not in POSTING_PERIODS:
        raise DepreciationError("posting_period 只能是 monthly 或 yearly。")
    if (
        isinstance(specification.useful_life_months, bool)
        or not isinstance(specification.useful_life_months, int)
        or specification.useful_life_months <= 0
    ):
        raise DepreciationError("使用寿命月数必须是正整数。")
    if specification.posting_period == POSTING_MONTHLY:
        if specification.annual_posting_month is not None:
            raise DepreciationError("monthly 政策不得填写 annual_posting_month。")
    elif (
        isinstance(specification.annual_posting_month, bool)
        or not isinstance(specification.annual_posting_month, int)
        or not 1 <= specification.annual_posting_month <= 12
    ):
        raise DepreciationError("yearly 政策必须填写 1..12 的 annual_posting_month。")
    if specification.method in {
        METHOD_STRAIGHT_LINE,
        METHOD_DOUBLE_DECLINING_BALANCE,
    } and specification.posting_period == POSTING_YEARLY:
        if specification.useful_life_months % 12:
            raise DepreciationError("年周期年限平均法/双倍余额法要求寿命月数可被 12 整除。")
    if specification.method == METHOD_SUM_OF_YEARS_DIGITS:
        if specification.useful_life_months % 12:
            raise DepreciationError("年数总和法要求使用寿命为 12 的整数倍。")

    cost = _nonnegative_money(specification.original_cost, field_name="原值")
    salvage = calculate_salvage(
        original_cost=cost,
        salvage_mode=specification.salvage_mode,
        salvage_rate=specification.salvage_rate,
        salvage_amount=specification.salvage_amount,
    )
    opening_ad = _nonnegative_money(
        specification.opening_actual_accumulated_depreciation,
        field_name="期初实际累计折旧",
    )
    opening_impairment = _nonnegative_money(
        specification.opening_impairment, field_name="期初减值"
    )
    canonical_opening_book = money(cost - opening_ad - opening_impairment)
    if canonical_opening_book < ZERO:
        raise DepreciationError("期初累计折旧和减值不得使账面价值小于 0。")
    if specification.opening_book_value is not None:
        canonical_opening_book = validate_opening_balances(
            original_cost=cost,
            opening_actual_accumulated_depreciation=opening_ad,
            opening_impairment=opening_impairment,
            opening_book_value=specification.opening_book_value,
        )
    depreciable_amount = money(max(canonical_opening_book - salvage, ZERO))

    start_date = resolve_start_date(
        commissioning_date=specification.commissioning_date,
        start_rule=specification.start_rule,
        specified_start=specification.specified_start,
        allow_historical_override=specification.allow_historical_start,
    )
    suspensions = merge_periods(specification.suspensions)
    natural_end = calculate_life_end(
        start_date=start_date,
        useful_life_months=specification.useful_life_months,
        suspensions=suspensions,
    )
    schedule_end = natural_end
    if specification.stop_date is not None:
        stop_date = _require_date(specification.stop_date, field_name="停止生效日")
        schedule_end = min(schedule_end, stop_date)
    active = active_intervals(
        start_date=start_date,
        end_date=schedule_end,
        suspensions=suspensions,
    )
    periods = _schedule_periods(
        start_date=start_date,
        schedule_end=schedule_end,
        posting_period=specification.posting_period,
    )
    fractions = [eligible_fraction(period, active) for period in periods]
    last_eligible = _last_eligible_index(fractions)
    completed_natural_life = schedule_end >= natural_end

    usage_by_period = usage_by_period or {}
    manual_by_period = manual_by_period or {}
    if specification.method != METHOD_UNITS_OF_PRODUCTION and usage_by_period:
        raise DepreciationError("只有工作量法可以提供 usage_by_period。")
    if specification.method != METHOD_MANUAL and manual_by_period:
        raise DepreciationError("只有手工折旧可以提供 manual_by_period。")

    expected_units: Decimal | None = None
    if specification.method == METHOD_UNITS_OF_PRODUCTION:
        if specification.expected_total_units is None:
            raise DepreciationError("工作量法必须填写预计总工作量。")
        expected_units = decimal_value(
            specification.expected_total_units, field_name="预计总工作量"
        )
        if expected_units <= ZERO:
            raise DepreciationError("预计总工作量必须大于 0。")
        if not str(specification.work_unit or "").strip():
            raise DepreciationError("工作量法必须填写工作量单位。")
    elif specification.expected_total_units is not None or specification.work_unit is not None:
        raise DepreciationError("非工作量法不得填写预计总工作量或工作量单位。")

    syd_raw: list[Decimal] | None = None
    syd_posted: list[Decimal] | None = None
    syd_corrected: set[int] = set()
    useful_years = specification.useful_life_months // 12
    if (
        specification.method == METHOD_SUM_OF_YEARS_DIGITS
        and specification.posting_period == POSTING_MONTHLY
    ):
        syd_raw, syd_posted, syd_corrected = _syd_monthly_allocations(
            periods=periods,
            active=active,
            start_date=start_date,
            useful_life_years=useful_years,
            depreciable_amount=depreciable_amount,
            suspensions=suspensions,
            completed_natural_life=completed_natural_life,
        )

    lines: list[ScheduleLine] = []
    book_value = canonical_opening_book
    accumulated = opening_ad
    planned_sum = ZERO
    cumulative_units = ZERO
    ddb_switched = False
    useful_periods = Decimal(
        specification.useful_life_months
        if specification.posting_period == POSTING_MONTHLY
        else specification.useful_life_months // 12
    )
    remaining_fraction_sums = [ZERO for _ in fractions]
    running_fraction = ZERO
    for index in range(len(fractions) - 1, -1, -1):
        running_fraction += fractions[index]
        remaining_fraction_sums[index] = running_fraction

    for index, (period, fraction) in enumerate(zip(periods, fractions, strict=True)):
        opening_book = book_value
        remaining_db = money(max(book_value - salvage, ZERO))
        raw = ZERO
        planned_units: Decimal | None = None
        method_applied = specification.method
        snapshot: dict[str, object] = {
            "method": specification.method,
            "posting_period": specification.posting_period,
            "eligible_fraction": str(fraction),
            "salvage_floor": str(salvage),
            "final_period_correction": False,
        }

        if fraction == ZERO or remaining_db == ZERO:
            posted = money(ZERO)
            snapshot["skipped"] = "ineligible" if fraction == ZERO else "salvage_floor"
        elif specification.method == METHOD_STRAIGHT_LINE:
            raw = straight_line_raw(
                depreciable_amount=depreciable_amount,
                useful_life_periods=useful_periods,
                fraction=fraction,
            )
            final = completed_natural_life and index == last_eligible
            posted = post_depreciation(
                calculated_unrounded=raw,
                depreciable_balance_before=remaining_db,
                final_period=final,
            )
            snapshot["final_period_correction"] = final
        elif specification.method == METHOD_DOUBLE_DECLINING_BALANCE:
            remaining_periods = remaining_fraction_sums[index]
            candidates = double_declining_candidates(
                book_value_before=book_value,
                depreciable_balance_before=remaining_db,
                useful_life_periods=useful_periods,
                remaining_useful_periods=remaining_periods,
                fraction=fraction,
                already_switched=ddb_switched,
            )
            ddb_switched = candidates.use_straight_line
            raw = candidates.selected_raw
            method_applied = (
                METHOD_STRAIGHT_LINE if ddb_switched else METHOD_DOUBLE_DECLINING_BALANCE
            )
            final = completed_natural_life and index == last_eligible
            posted = post_depreciation(
                calculated_unrounded=raw,
                depreciable_balance_before=remaining_db,
                final_period=final,
            )
            snapshot.update(
                {
                    "ddb_raw": str(candidates.ddb_raw),
                    "straight_line_candidate_raw": str(
                        candidates.straight_line_raw
                    ),
                    "switched_to_straight_line": ddb_switched,
                    "final_period_correction": final,
                }
            )
        elif specification.method == METHOD_SUM_OF_YEARS_DIGITS:
            if specification.posting_period == POSTING_MONTHLY:
                assert syd_raw is not None and syd_posted is not None
                raw = syd_raw[index]
                posted = min(money(syd_posted[index]), remaining_db)
                snapshot["annual_target_correction"] = index in syd_corrected
            else:
                depreciation_year = index + 1
                annual_raw = sum_of_years_digits_annual_raw(
                    depreciable_amount=depreciable_amount,
                    useful_life_years=useful_years,
                    depreciation_year=min(depreciation_year, useful_years),
                )
                raw = annual_raw * fraction
                final = completed_natural_life and index == last_eligible
                posted = post_depreciation(
                    calculated_unrounded=raw,
                    depreciable_balance_before=remaining_db,
                    final_period=final,
                )
                snapshot.update(
                    {
                        "depreciation_year": depreciation_year,
                        "annual_target": str(money(annual_raw)),
                        "final_period_correction": final,
                    }
                )
        elif specification.method == METHOD_UNITS_OF_PRODUCTION:
            assert expected_units is not None
            supplied_units = _nonnegative_decimal(
                _mapping_value(
                    usage_by_period, period, label="当期工作量（明确 0 也必须录入）"
                ),
                field_name="当期工作量",
            )
            remaining_units = max(expected_units - cumulative_units, ZERO)
            planned_units = min(supplied_units, remaining_units)
            raw = (
                depreciable_amount / expected_units * planned_units
                if expected_units > ZERO
                else ZERO
            )
            reaches_units = cumulative_units + planned_units >= expected_units
            final = reaches_units or (
                completed_natural_life and index == last_eligible
            )
            posted = post_depreciation(
                calculated_unrounded=raw,
                depreciable_balance_before=remaining_db,
                final_period=final,
            )
            cumulative_units += planned_units
            snapshot.update(
                {
                    "input_units": str(supplied_units),
                    "posted_units": str(planned_units),
                    "cumulative_units": str(cumulative_units),
                    "expected_total_units": str(expected_units),
                    "work_unit": str(specification.work_unit),
                    "units_capped": supplied_units != planned_units,
                    "final_period_correction": final,
                }
            )
        elif specification.method == METHOD_MANUAL:
            manual = _mapping_value(
                manual_by_period,
                period,
                label="手工折旧金额和原因（明确 0 也必须录入）",
            )
            if not isinstance(manual, ManualAmount):
                raise DepreciationError("manual_by_period 的值必须是 ManualAmount。")
            reason = str(manual.reason or "").strip()
            if not reason:
                raise DepreciationError("手工折旧必须填写原因，金额为 0 时也不例外。")
            posted = _nonnegative_money(manual.amount, field_name="手工折旧金额")
            if posted > remaining_db:
                raise DepreciationError("手工折旧金额不得超过剩余可折旧金额。")
            raw = posted
            snapshot["manual_reason"] = reason
        else:
            posted = money(ZERO)
            snapshot["skipped"] = "no_depreciation"

        # SYD annual target allocation is already rounded, but the common floor
        # and final-life guarantee still win over every formula.
        if (
            specification.method == METHOD_SUM_OF_YEARS_DIGITS
            and completed_natural_life
            and index == last_eligible
            and specification.method != METHOD_MANUAL
        ):
            posted = remaining_db
            snapshot["final_period_correction"] = True
        posted = min(money(posted), remaining_db)
        planned_sum = money(planned_sum + posted)
        accumulated = money(opening_ad + planned_sum)
        book_value = money(opening_book - posted)
        if book_value < salvage and specification.method != METHOD_NO_DEPRECIATION:
            raise DepreciationError("折旧计划越过残值底线。")
        lines.append(
            ScheduleLine(
                sequence_no=index + 1,
                period_start=period.start,
                period_end=period.end,
                opening_book_value=opening_book,
                calculated_unrounded=raw,
                planned_amount=posted,
                planned_accumulated=accumulated,
                closing_book_value=book_value,
                eligible_fraction=fraction,
                planned_units=planned_units,
                method_applied=method_applied,
                formula_snapshot=snapshot,
            )
        )

        if (
            specification.method == METHOD_UNITS_OF_PRODUCTION
            and expected_units is not None
            and cumulative_units >= expected_units
        ):
            break

    return ScheduleResult(
        original_cost=cost,
        salvage_value=salvage,
        opening_book_value=canonical_opening_book,
        depreciable_amount=depreciable_amount,
        start_date=start_date,
        natural_end_date=natural_end,
        schedule_end_date=schedule_end,
        lines=tuple(lines),
    )


__all__ = [
    "ActualBalances",
    "DDBCandidates",
    "DepreciationError",
    "METHOD_DOUBLE_DECLINING_BALANCE",
    "METHOD_MANUAL",
    "METHOD_NO_DEPRECIATION",
    "METHOD_STRAIGHT_LINE",
    "METHOD_SUM_OF_YEARS_DIGITS",
    "METHOD_UNITS_OF_PRODUCTION",
    "MONEY_QUANTUM",
    "ManualAmount",
    "POSTING_MONTHLY",
    "POSTING_YEARLY",
    "Period",
    "SALVAGE_AMOUNT",
    "SALVAGE_RATE",
    "START_CURRENT_MONTH",
    "START_NEXT_MONTH",
    "START_SPECIFIED_DATE",
    "START_SPECIFIED_MONTH",
    "STOP_EVENT_DATE",
    "STOP_NEXT_MONTH",
    "ScheduleInput",
    "ScheduleLine",
    "ScheduleResult",
    "active_intervals",
    "actual_balances",
    "add_months_safe",
    "aggregate_actual_balances",
    "calculate_life_end",
    "calculate_salvage",
    "decimal_value",
    "depreciation_year_period",
    "double_declining_candidates",
    "eligible_days",
    "eligible_fraction",
    "generate_schedule",
    "merge_periods",
    "money",
    "month_period",
    "post_depreciation",
    "resolve_start_date",
    "resolve_stop_date",
    "straight_line_raw",
    "sum_of_years_digits_annual_raw",
    "units_of_production_raw",
    "validate_opening_balances",
    "year_period",
]
