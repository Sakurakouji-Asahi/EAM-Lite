from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.maintenance.domain import add_calendar_cycle
from apps.maintenance.models import MaintenancePlan, MaintenanceRecord
from apps.maintenance.services import complete_maintenance
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db(transaction=True)


def test_postgresql_concurrent_same_due_completes_once_and_advances_one_cycle():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency acceptance")
    ctx = maintenance_context("S9CONCURRENT")
    plan_id = ctx["plan"].pk
    actor_id = ctx["responsible_user"].pk
    scheduled = ctx["plan"].next_maintenance_date
    completed = timezone.localdate()
    barrier = Barrier(2)

    def worker(key):
        close_old_connections()
        try:
            plan = MaintenancePlan._base_manager.get(pk=plan_id)
            actor = User.objects.get(pk=actor_id)
            try:
                barrier.wait(timeout=15)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                record = complete_maintenance(
                    actor=actor,
                    plan=plan,
                    scheduled_date=scheduled,
                    completed_date=completed,
                    actual_content="并发完成同一到期实例",
                    result="normal",
                    remark="并发验收",
                    idempotency_key=key,
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            return ("ok", str(record.pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                worker,
                ("S9CONCURRENT-A", "S9CONCURRENT-B"),
            )
        )

    assert sorted(item[0] for item in results) == ["ok", "validation"]
    assert "已有确认" in next(item[1] for item in results if item[0] == "validation")
    records = MaintenanceRecord.objects.filter(
        maintenance_plan_id=plan_id,
        scheduled_date=scheduled,
        status="confirmed",
    )
    assert records.count() == 1
    plan = MaintenancePlan._base_manager.get(pk=plan_id)
    assert plan.last_maintenance_date == completed
    assert plan.next_maintenance_date == add_calendar_cycle(
        completed, plan.cycle_value, plan.cycle_unit
    )
    assert AuditLog.objects.filter(
        action="maintenance.completed",
        object_id=str(records.get().pk),
    ).count() == 1
