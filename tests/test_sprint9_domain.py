from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError

from apps.maintenance.domain import add_calendar_cycle, business_date, due_status


@pytest.mark.parametrize(
    ("base", "value", "unit", "expected"),
    (
        (date(2026, 12, 31), 1, "day", date(2027, 1, 1)),
        (date(2026, 12, 28), 1, "week", date(2027, 1, 4)),
        (date(2024, 1, 31), 1, "month", date(2024, 2, 29)),
        (date(2025, 1, 31), 1, "month", date(2025, 2, 28)),
        (date(2024, 2, 29), 1, "year", date(2025, 2, 28)),
        (date(2024, 2, 29), 4, "year", date(2028, 2, 29)),
    ),
)
def test_calendar_cycles_use_real_calendar_boundaries(base, value, unit, expected):
    assert add_calendar_cycle(base, value, unit) == expected


@pytest.mark.parametrize(
    ("value", "unit"),
    ((0, "day"), (-1, "month"), (True, "week"), (1, "runtime_hour")),
)
def test_calendar_cycles_reject_invalid_values_and_runtime_hours(value, unit):
    with pytest.raises(ValidationError):
        add_calendar_cycle(date(2026, 1, 1), value, unit)


def test_business_date_uses_shanghai_boundary_and_rejects_naive_datetimes():
    assert business_date(
        datetime(2026, 8, 12, 16, 30, tzinfo=timezone.utc)
    ) == date(2026, 8, 13)
    with pytest.raises(ValidationError):
        business_date(datetime(2026, 8, 13, 0, 0))


@pytest.mark.parametrize(
    ("as_of", "expected"),
    (
        (date(2026, 8, 6), "not_due"),
        (date(2026, 8, 7), "upcoming"),
        (date(2026, 8, 10), "due_today"),
        (date(2026, 8, 11), "overdue"),
    ),
)
def test_due_status_boundaries_share_one_notice_window(as_of, expected):
    plan = SimpleNamespace(
        status="active",
        next_maintenance_date=date(2026, 8, 10),
        advance_notice_days=3,
    )
    assert due_status(plan, as_of=as_of) == expected


def test_due_status_reports_inactive_and_insufficient_data_explicitly():
    inactive = SimpleNamespace(
        status="suspended",
        next_maintenance_date=date(2026, 8, 10),
        advance_notice_days=3,
    )
    insufficient = SimpleNamespace(
        status="active", next_maintenance_date=None, advance_notice_days=3
    )
    assert due_status(inactive, as_of=date(2026, 8, 1)) == "inactive"
    assert due_status(insufficient, as_of=date(2026, 8, 1)) == "insufficient_data"
