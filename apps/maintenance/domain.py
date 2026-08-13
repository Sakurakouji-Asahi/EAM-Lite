"""Calendar and due-state rules for preventive maintenance."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone


SHANGHAI = ZoneInfo("Asia/Shanghai")
CYCLE_UNITS = frozenset({"day", "week", "month", "year"})


def business_date(value=None) -> date:
    """Return one Shanghai business date without accepting naive datetimes."""

    if value is None:
        return timezone.localdate(timezone=SHANGHAI)
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            raise ValidationError("日期时间必须包含时区。")
        return value.astimezone(SHANGHAI).date()
    if not isinstance(value, date):
        raise ValidationError("必须是有效日期。")
    return value


def add_calendar_cycle(base_date, cycle_value, cycle_unit) -> date:
    """Advance a date using the single approved day/week/month/year rule."""

    base = business_date(base_date)
    if isinstance(cycle_value, bool):
        raise ValidationError({"cycle_value": "周期数值必须为正整数。"})
    try:
        value = int(cycle_value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"cycle_value": "周期数值必须为正整数。"}) from exc
    if value <= 0 or value != cycle_value:
        raise ValidationError({"cycle_value": "周期数值必须为正整数。"})
    if cycle_unit not in CYCLE_UNITS:
        raise ValidationError({"cycle_unit": "周期单位只允许日、周、月、年。"})
    if cycle_unit == "day":
        return base + timedelta(days=value)
    if cycle_unit == "week":
        return base + timedelta(days=7 * value)
    if cycle_unit == "month":
        month_index = base.year * 12 + base.month - 1 + value
        target_year, zero_month = divmod(month_index, 12)
        target_month = zero_month + 1
        return date(
            target_year,
            target_month,
            min(base.day, calendar.monthrange(target_year, target_month)[1]),
        )
    target_year = base.year + value
    return date(
        target_year,
        base.month,
        min(base.day, calendar.monthrange(target_year, base.month)[1]),
    )


def due_status(plan, as_of=None) -> str:
    """Return one shared due-state token for list, detail and dashboard."""

    if getattr(plan, "status", None) != "active":
        return "inactive"
    due = getattr(plan, "next_maintenance_date", None)
    if due is None:
        return "insufficient_data"
    today = business_date(as_of)
    if today > due:
        return "overdue"
    if today == due:
        return "due_today"
    notice_days = getattr(plan, "advance_notice_days", None)
    if notice_days is None or notice_days < 0:
        return "insufficient_data"
    if today >= due - timedelta(days=notice_days):
        return "upcoming"
    return "not_due"


__all__ = [
    "CYCLE_UNITS",
    "SHANGHAI",
    "add_calendar_cycle",
    "business_date",
    "due_status",
]
