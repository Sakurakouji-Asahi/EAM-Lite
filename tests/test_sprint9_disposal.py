from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.assets.lifecycle_services import (
    cancel_disposal,
    complete_disposal,
    initiate_disposal,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
    reverse_disposal,
)
from apps.assets.models import (
    AssetDisposalReversal,
    AssetMovement,
    AttachmentLink,
)
from apps.audit.models import AuditLog
from apps.maintenance.domain import add_calendar_cycle
from apps.maintenance.models import MaintenancePlan
from apps.maintenance.services import (
    complete_maintenance,
    create_maintenance_plan,
    set_maintenance_plan_status,
)
from apps.masterdata.services import set_employee_active
from tests.test_sprint3_support import direct_attachment, make_user
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db(transaction=True)


def _new_plan(ctx, key, *, status="active"):
    plan = create_maintenance_plan(
        actor=ctx["equipment"],
        company=ctx["company"],
        asset=ctx["asset"],
        name=f"{key} 保养计划",
        cycle_value=2,
        cycle_unit="week",
        responsible_employee=ctx["responsible"],
        advance_notice_days=2,
        standard_content="检查易损件并记录",
        first_due_date=timezone.localdate() + timedelta(days=4),
    )
    if status != "active":
        plan = set_maintenance_plan_status(
            actor=ctx["equipment"],
            plan=plan,
            status=status,
            reason="处置联动验收准备" if status == "ended" else "",
        )
    return plan


def _ready_disposal(ctx, key):
    today = timezone.localdate()
    disposal = initiate_disposal(
        actor=ctx["equipment"],
        asset=ctx["asset"],
        disposal_type="scrap",
        application_date=today,
        planned_disposal_date=today,
        reason="Sprint 9 保养联动验收",
        idempotency_key=f"{key}-start",
        expected_status=ctx["asset"].asset_status,
    )
    disposal = record_disposal_actual_details(
        actor=ctx["equipment"],
        disposal=disposal,
        actual_disposal_date=today,
        handled_by=ctx["equipment"],
        idempotency_key=f"{key}-actual",
    )
    disposal = lock_disposal_financial_snapshot(
        actor=ctx["finance"],
        disposal=disposal,
        disposal_income=Decimal("0.00"),
        idempotency_key=f"{key}-snapshot",
    )
    attachment = direct_attachment(
        ctx["company"],
        ctx["equipment"],
        key=f"private/disposals/{key}.jpg",
        filename=f"{key}.jpg",
    )
    AttachmentLink.objects.create(
        company=ctx["company"],
        attachment=attachment,
        asset_disposal=disposal,
        role="disposal",
        security_class="A0",
        created_by=ctx["equipment"],
    )
    return disposal


def test_disposal_completion_ends_only_live_plans_and_reversal_restores_exact_source_states():
    ctx = maintenance_context("S9DISPRESTORE")
    record = complete_maintenance(
        actor=ctx["responsible_user"],
        plan=ctx["plan"],
        scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="处置前已完成一次有效保养",
        result="normal",
        idempotency_key="S9DISPRESTORE-maint",
    )
    suspended = _new_plan(ctx, "S9DISPRESTORE-S", status="suspended")
    manually_ended = _new_plan(ctx, "S9DISPRESTORE-M", status="ended")
    disposal = complete_disposal(
        actor=ctx["equipment"],
        disposal=_ready_disposal(ctx, "S9DISPRESTORE"),
        idempotency_key="S9DISPRESTORE-complete",
    )

    for plan, previous in ((ctx["plan"], "active"), (suspended, "suspended")):
        plan.refresh_from_db()
        assert plan.status == "ended"
        assert plan.ended_reason == "asset_disposal"
        assert plan.ended_by_disposal_id == disposal.pk
        assert plan.status_before_disposal == previous
        assert plan.ended_at is not None
    manually_ended.refresh_from_db()
    assert manually_ended.status == "ended"
    assert manually_ended.ended_reason == "manual"
    assert manually_ended.ended_by_disposal_id is None

    reversal = reverse_disposal(
        actor=ctx["finance"],
        disposal=disposal,
        reason="处置终态录入错误",
        idempotency_key="S9DISPRESTORE-reverse",
    )
    repeated = reverse_disposal(
        actor=ctx["finance"],
        disposal=disposal,
        reason="处置终态录入错误",
        idempotency_key="S9DISPRESTORE-reverse",
    )
    assert repeated.pk == reversal.pk

    ctx["plan"].refresh_from_db()
    suspended.refresh_from_db()
    manually_ended.refresh_from_db()
    disposal.refresh_from_db()
    ctx["asset"].refresh_from_db()
    assert disposal.status == "reversed"
    assert ctx["asset"].asset_status == "in_use"
    assert ctx["plan"].status == "active"
    assert ctx["plan"].last_maintenance_date == record.completed_date
    assert ctx["plan"].next_maintenance_date == add_calendar_cycle(
        record.completed_date, ctx["plan"].cycle_value, ctx["plan"].cycle_unit
    )
    assert suspended.status == "suspended"
    assert suspended.last_maintenance_date is None
    assert suspended.next_maintenance_date == suspended.first_due_date
    for restored in (ctx["plan"], suspended):
        assert restored.ended_reason is None
        assert restored.ended_by_disposal_id is None
        assert restored.status_before_disposal is None
        assert restored.ended_at is None
    assert manually_ended.status == "ended" and manually_ended.ended_reason == "manual"
    assert AssetDisposalReversal.objects.filter(asset_disposal=disposal).count() == 1
    assert AuditLog.objects.filter(
        action="maintenance.plan_ended_by_disposal"
    ).count() == 2
    assert AuditLog.objects.filter(
        action="maintenance.plan_restored_by_disposal"
    ).count() == 2


def test_cancel_before_terminal_does_not_change_maintenance_plan():
    ctx = maintenance_context("S9DISPCANCEL")
    original = (
        ctx["plan"].status,
        ctx["plan"].last_maintenance_date,
        ctx["plan"].next_maintenance_date,
    )
    today = timezone.localdate()
    disposal = initiate_disposal(
        actor=ctx["equipment"],
        asset=ctx["asset"],
        disposal_type="scrap",
        application_date=today,
        planned_disposal_date=today,
        reason="发起后取消",
        idempotency_key="S9DISPCANCEL-start",
        expected_status=ctx["asset"].asset_status,
    )
    cancel_disposal(
        actor=ctx["equipment"],
        disposal=disposal,
        reason="终态前撤回",
        idempotency_key="S9DISPCANCEL-cancel",
    )
    ctx["plan"].refresh_from_db()
    assert (
        ctx["plan"].status,
        ctx["plan"].last_maintenance_date,
        ctx["plan"].next_maintenance_date,
    ) == original
    assert ctx["plan"].ended_by_disposal_id is None


def test_disposal_maintenance_end_failure_rolls_back_entire_completion(monkeypatch):
    ctx = maintenance_context("S9DISPROLLBACK")
    disposal = _ready_disposal(ctx, "S9DISPROLLBACK")
    before_movements = AssetMovement.objects.filter(asset=ctx["asset"]).count()

    import apps.assets.lifecycle_services as lifecycle_services

    original_audit = lifecycle_services._audit

    def fail_on_maintenance_end(**kwargs):
        if kwargs.get("action") == "maintenance.plan_ended_by_disposal":
            raise RuntimeError("simulated maintenance-end audit failure")
        return original_audit(**kwargs)

    monkeypatch.setattr(lifecycle_services, "_audit", fail_on_maintenance_end)
    with pytest.raises(RuntimeError, match="maintenance-end audit failure"):
        complete_disposal(
            actor=ctx["equipment"],
            disposal=disposal,
            idempotency_key="S9DISPROLLBACK-complete",
        )

    ctx["asset"].refresh_from_db()
    ctx["plan"].refresh_from_db()
    disposal.refresh_from_db()
    assert ctx["asset"].asset_status == "pending_disposal"
    assert ctx["plan"].status == "active"
    assert ctx["plan"].ended_by_disposal_id is None
    assert disposal.status == "finance_locked"
    assert AssetMovement.objects.filter(asset=ctx["asset"]).count() == before_movements
    assert not AuditLog.objects.filter(
        action="maintenance.plan_ended_by_disposal"
    ).exists()


def test_reversal_rejects_invalid_plan_responsible_and_preserves_all_terminal_state():
    ctx = maintenance_context("S9DISPINVALIDRESP")
    disposal = complete_disposal(
        actor=ctx["equipment"],
        disposal=_ready_disposal(ctx, "S9DISPINVALIDRESP"),
        idempotency_key="S9DISPINVALIDRESP-complete",
    )
    hr = make_user("s9dispinvalidresp-hr", "hr")
    set_employee_active(
        actor=hr,
        employee=ctx["responsible"],
        is_active=False,
    )
    before_movements = AssetMovement.objects.filter(asset=ctx["asset"]).count()

    with pytest.raises(ValidationError, match="责任人"):
        reverse_disposal(
            actor=ctx["finance"],
            disposal=disposal,
            reason="责任人已失效，不得恢复保养计划",
            idempotency_key="S9DISPINVALIDRESP-reverse",
        )

    ctx["asset"].refresh_from_db()
    ctx["plan"].refresh_from_db()
    disposal.refresh_from_db()
    assert ctx["asset"].asset_status == "disposed"
    assert disposal.status == "confirmed"
    assert ctx["plan"].status == "ended"
    assert ctx["plan"].ended_reason == "asset_disposal"
    assert ctx["plan"].ended_by_disposal_id == disposal.pk
    assert ctx["plan"].status_before_disposal == "active"
    assert not AssetDisposalReversal.objects.filter(asset_disposal=disposal).exists()
    assert AssetMovement.objects.filter(asset=ctx["asset"]).count() == before_movements
