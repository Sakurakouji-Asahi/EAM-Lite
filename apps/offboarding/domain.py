"""Pure domain helpers for Sprint 10 employee asset clearance."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone


SHANGHAI = ZoneInfo("Asia/Shanghai")

FORMAL_NON_TERMINAL_ASSET_STATUSES = frozenset(
    {
        "pending_label",
        "in_use",
        "idle",
        "loaned",
        "under_repair",
        "pending_disposal",
    }
)
TERMINAL_ASSET_STATUSES = frozenset({"disposed", "sold", "other_disposed"})
ACTIVE_CLEARANCE_STATUSES = frozenset({"open", "blocked"})
RESOLVED_ITEM_RESOLUTIONS = frozenset({"returned", "transferred", "disposed"})
UNRESOLVED_ITEM_RESOLUTIONS = frozenset({"pending", "disposal_in_progress"})


def business_date(value=None) -> date:
    """Return/validate a Shanghai business date without using server-local time."""

    if value is None:
        return timezone.localdate(timezone=SHANGHAI)
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            raise ValidationError(
                {"termination_date": "日期时间必须包含时区。"}
            )
        return value.astimezone(SHANGHAI).date()
    if type(value) is not date:
        raise ValidationError({"termination_date": "必须是有效日期。"})
    return value


def validate_termination_date(*, employee, termination_date) -> date:
    """Validate the explicit date required for initial-clearance completion."""

    if termination_date is None:
        raise ValidationError({"termination_date": "必须填写实际离职日期。"})
    result = business_date(termination_date)
    if employee.hire_date is not None and result < employee.hire_date:
        raise ValidationError({"termination_date": "实际离职日期不得早于入职日期。"})
    if result > business_date():
        raise ValidationError({"termination_date": "实际离职日期不得晚于当前上海业务日。"})
    return result


def clearance_status_for_unresolved(unresolved: int) -> str:
    """Derive, rather than accept, the active clearance status."""

    if unresolved < 0:
        raise ValidationError("未解决数量不能为负数。")
    return "blocked" if unresolved else "open"


def is_resolved_resolution(value: str) -> bool:
    return value in RESOLVED_ITEM_RESOLUTIONS


def location_path(location) -> str:
    """Create an immutable location path while rejecting corrupt tree cycles."""

    names, current, seen = [], location, set()
    while current is not None:
        if current.pk in seen:
            raise ValidationError("位置树存在循环，不能建立清退快照。")
        seen.add(current.pk)
        names.append(current.name)
        current = current.parent
    return " / ".join(reversed(names))


__all__ = [
    "ACTIVE_CLEARANCE_STATUSES",
    "FORMAL_NON_TERMINAL_ASSET_STATUSES",
    "RESOLVED_ITEM_RESOLUTIONS",
    "SHANGHAI",
    "TERMINAL_ASSET_STATUSES",
    "UNRESOLVED_ITEM_RESOLUTIONS",
    "business_date",
    "clearance_status_for_unresolved",
    "is_resolved_resolution",
    "location_path",
    "validate_termination_date",
]
