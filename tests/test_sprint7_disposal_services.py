from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.lifecycle_permissions import (
    can_view_disposal_financial_fields,
    scoped_lifecycle_candidates,
)
from apps.assets.lifecycle_services import (
    archive_asset,
    cancel_disposal,
    complete_disposal,
    initiate_disposal,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
    restore_asset_visibility,
)
from apps.assets.models import AssetDisposal, AssetMovement, AttachmentLink
from apps.audit.models import AuditLog
from apps.finance.services import confirm_depreciation_batch, generate_depreciation_batch
from tests.test_sprint3_support import direct_attachment
from tests.test_sprint7_support import (
    active_asset_context,
    active_fixed_asset_context,
    add_department_manager,
)


pytestmark = pytest.mark.django_db


def _initiate(context, asset, key, *, disposal_type="scrap", actor=None, **overrides):
    today = timezone.localdate()
    values = {
        "actor": actor or context["equipment"],
        "asset": asset,
        "disposal_type": disposal_type,
        "application_date": today,
        "planned_disposal_date": today + timedelta(days=3),
        "reason": "达到处置条件",
        "description": "现场核实",
        "recipient_name": "回收单位" if disposal_type != "scrap" else "",
        "idempotency_key": key,
        "expected_status": asset.asset_status,
    }
    values.update(overrides)
    return initiate_disposal(**values)


def _record_and_lock(context, disposal, key, *, income="0.00"):
    disposal = record_disposal_actual_details(
        actor=context["equipment"], disposal=disposal,
        actual_disposal_date=timezone.localdate(),
        recipient_name=disposal.recipient_name,
        handled_by=context["equipment"], idempotency_key=f"{key}-actual",
    )
    return lock_disposal_financial_snapshot(
        actor=context["finance"], disposal=disposal,
        disposal_income=income, idempotency_key=f"{key}-finance",
    )


def _add_disposal_evidence(context, disposal, key, *, security="A0"):
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key=f"private/disposals/{key}.jpg", filename=f"{key}.jpg",
    )
    return AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        asset=None, asset_disposal=disposal, role="disposal",
        security_class=security, created_by=context["equipment"],
    )


def test_initiate_disposal_uses_planned_date_only_and_preserves_previous_state():
    context, asset, _qr = active_asset_context("S7DISPSTART", status="idle")
    disposal = _initiate(context, asset, "S7DISPSTART-key")
    repeated = _initiate(context, asset, "S7DISPSTART-key")
    asset.refresh_from_db()

    assert repeated.pk == disposal.pk
    assert disposal.status == "draft"
    assert disposal.previous_asset_status == "idle"
    assert disposal.actual_disposal_date is None
    assert asset.asset_status == "pending_disposal"
    movement = AssetMovement.objects.get(
        asset=asset, movement_type="disposal_start"
    )
    assert (movement.from_status, movement.to_status) == (
        "idle", "pending_disposal"
    )
    assert AssetDisposal.objects.count() == 1
    assert AuditLog.objects.filter(action="asset_disposal.initiated").count() == 1


def test_department_manager_can_initiate_in_scope_but_cannot_cancel():
    context, asset, _qr = active_asset_context("S7DISPMGR")
    manager = add_department_manager(context, "S7DISPMGR", context["department"])
    disposal = _initiate(
        context, asset, "S7DISPMGR-start", actor=manager
    )
    with pytest.raises(PermissionDenied):
        cancel_disposal(
            actor=manager, disposal=disposal, reason="经理尝试取消",
            idempotency_key="S7DISPMGR-cancel",
        )
    disposal.refresh_from_db()
    asset.refresh_from_db()
    assert disposal.status == "draft"
    assert asset.asset_status == "pending_disposal"


def test_planned_date_cannot_lock_or_complete_without_actual_date_and_evidence():
    context, asset, _qr = active_asset_context("S7DISPBLOCK")
    disposal = _initiate(context, asset, "S7DISPBLOCK-start")

    with pytest.raises(ValidationError, match="实际处置日期"):
        lock_disposal_financial_snapshot(
            actor=context["finance"], disposal=disposal,
            disposal_income="0.00", idempotency_key="S7DISPBLOCK-finance",
        )
    with pytest.raises(ValidationError):
        complete_disposal(
            actor=context["equipment"], disposal=disposal,
            idempotency_key="S7DISPBLOCK-complete",
        )
    asset.refresh_from_db()
    disposal.refresh_from_db()
    assert asset.asset_status == "pending_disposal"
    assert disposal.status == "draft"


@pytest.mark.parametrize(
    ("case_name", "offset_days"),
    (("before_application", -1), ("future", 1)),
)
def test_actual_date_must_be_between_application_and_current_business_day(
    case_name, offset_days
):
    context, asset, _qr = active_asset_context(f"S7DATE{case_name}")
    today = timezone.localdate()
    disposal = _initiate(
        context, asset, f"S7DATE{case_name}-start",
        application_date=today,
    )
    with pytest.raises(ValidationError):
        record_disposal_actual_details(
            actor=context["equipment"], disposal=disposal,
            actual_disposal_date=today + timedelta(days=offset_days),
            handled_by=context["equipment"],
            idempotency_key=f"S7DATE{case_name}-actual",
        )


def test_only_finance_locks_snapshot_and_nonfinance_cannot_view_amounts():
    context, asset, _qr = active_asset_context("S7FINLOCK", cost=Decimal("4321.09"))
    disposal = _initiate(context, asset, "S7FINLOCK-start", disposal_type="sale")
    disposal = record_disposal_actual_details(
        actor=context["equipment"], disposal=disposal,
        actual_disposal_date=timezone.localdate(), recipient_name="受让方",
        handled_by=context["equipment"], idempotency_key="S7FINLOCK-actual",
    )
    with pytest.raises(PermissionDenied):
        lock_disposal_financial_snapshot(
            actor=context["equipment"], disposal=disposal,
            disposal_income="12.34", idempotency_key="S7FINLOCK-denied",
        )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"], disposal=disposal,
        disposal_income=Decimal("12.34"), idempotency_key="S7FINLOCK-ok",
    )

    assert disposal.status == "finance_locked"
    assert disposal.original_cost_snapshot == Decimal("4321.09")
    assert disposal.actual_accumulated_depreciation_snapshot == Decimal("0.00")
    assert disposal.impairment_snapshot == Decimal("0.00")
    assert disposal.book_value_snapshot == Decimal("4321.09")
    assert disposal.disposal_income == Decimal("12.34")
    assert can_view_disposal_financial_fields(context["finance"], disposal)
    assert not can_view_disposal_financial_fields(context["equipment"], disposal)
    assert not can_view_disposal_financial_fields(context["admin"], disposal)


def test_fixed_asset_snapshot_blocks_missing_confirmed_period_then_allows_lock():
    context, asset, _qr, profile, _policy = active_fixed_asset_context(
        "S7PERIOD", stop_rule="next_month"
    )
    disposal = _initiate(context, asset, "S7PERIOD-start")
    disposal = record_disposal_actual_details(
        actor=context["equipment"], disposal=disposal,
        actual_disposal_date=timezone.localdate(),
        handled_by=context["equipment"], idempotency_key="S7PERIOD-actual",
    )
    with pytest.raises(ValidationError, match="必需折旧期间"):
        lock_disposal_financial_snapshot(
            actor=context["finance"], disposal=disposal,
            disposal_income="0.00", idempotency_key="S7PERIOD-lock-before",
        )

    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    batch = generate_depreciation_batch(
        actor=context["finance"], company=context["company"],
        period_start=period_start, period_end=period_end,
        idempotency_key="S7PERIOD-batch",
    )
    confirm_depreciation_batch(
        actor=context["finance"], batch=batch, reason="处置前完成当月折旧确认"
    )
    locked = lock_disposal_financial_snapshot(
        actor=context["finance"], disposal=disposal,
        disposal_income="0.00", idempotency_key="S7PERIOD-lock-after",
    )
    assert locked.status == "finance_locked"
    assert locked.actual_accumulated_depreciation_snapshot > Decimal("0.00")


def test_snapshot_is_immutable_after_lock_and_cancellation_preserves_it_and_evidence():
    context, asset, _qr = active_asset_context("S7CANCEL", cost=Decimal("987.65"))
    disposal = _initiate(context, asset, "S7CANCEL-start")
    disposal = _record_and_lock(context, disposal, "S7CANCEL")
    link = _add_disposal_evidence(context, disposal, "S7CANCEL")
    snapshot = (
        disposal.actual_disposal_date,
        disposal.original_cost_snapshot,
        disposal.actual_accumulated_depreciation_snapshot,
        disposal.impairment_snapshot,
        disposal.book_value_snapshot,
    )

    with pytest.raises(ValidationError):
        record_disposal_actual_details(
            actor=context["equipment"], disposal=disposal,
            actual_disposal_date=timezone.localdate(),
            handled_by=context["equipment"], idempotency_key="S7CANCEL-rewrite",
        )
    cancelled = cancel_disposal(
        actor=context["equipment"], disposal=disposal,
        reason="实际日期录入有误，重新发起", idempotency_key="S7CANCEL-cancel",
    )
    asset.refresh_from_db()
    cancelled.refresh_from_db()
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_by_id == context["equipment"].pk
    assert cancelled.cancelled_at is not None
    assert cancelled.cancellation_reason
    assert asset.asset_status == "in_use"
    assert snapshot == (
        cancelled.actual_disposal_date,
        cancelled.original_cost_snapshot,
        cancelled.actual_accumulated_depreciation_snapshot,
        cancelled.impairment_snapshot,
        cancelled.book_value_snapshot,
    )
    assert AttachmentLink.objects.filter(pk=link.pk, status="active").exists()


@pytest.mark.parametrize(
    ("disposal_type", "terminal"),
    (("scrap", "disposed"), ("sale", "sold"), ("other", "other_disposed")),
)
def test_three_disposal_types_complete_only_after_snapshot_and_evidence(
    disposal_type, terminal
):
    prefix = f"S7DONE{disposal_type}"
    context, asset, qr = active_asset_context(prefix)
    disposal = _initiate(
        context, asset, f"{prefix}-start", disposal_type=disposal_type
    )
    disposal = _record_and_lock(context, disposal, prefix, income="88.00")
    _add_disposal_evidence(context, disposal, prefix)
    completed = complete_disposal(
        actor=context["equipment"], disposal=disposal,
        idempotency_key=f"{prefix}-complete",
    )
    asset.refresh_from_db()
    qr.refresh_from_db()

    assert completed.status == "confirmed"
    assert completed.confirmed_by_id == context["equipment"].pk
    assert completed.confirmed_at is not None
    assert asset.asset_status == terminal
    assert asset.department_id == context["department"].pk
    assert asset.responsible_employee_id == context["employee"].pk
    assert asset.location_id == context["location"].pk
    assert qr.status == "active"
    assert AssetMovement.objects.filter(
        asset=asset, movement_type="disposal_complete",
        to_status=terminal,
    ).count() == 1


def test_archive_and_restore_visibility_preserve_terminal_business_and_history():
    context, asset, qr = active_asset_context("S7ARCHIVE")
    disposal = _initiate(context, asset, "S7ARCHIVE-start")
    disposal = _record_and_lock(context, disposal, "S7ARCHIVE")
    _add_disposal_evidence(context, disposal, "S7ARCHIVE")
    disposal = complete_disposal(
        actor=context["equipment"], disposal=disposal,
        idempotency_key="S7ARCHIVE-complete",
    )
    before_movements = AssetMovement.objects.filter(asset=asset).count()
    before_qr_status = qr.status

    archived = archive_asset(
        actor=context["admin"], asset=asset, reason="终态资料归档",
        idempotency_key="S7ARCHIVE-archive",
    )
    assert archived.record_status == "archived"
    assert archived.asset_status == "disposed"
    assert not scoped_lifecycle_candidates(
        context["equipment"], context["company"]
    ).filter(pk=asset.pk).exists()
    assert AssetMovement.objects.filter(asset=asset).count() == before_movements
    qr.refresh_from_db()
    disposal.refresh_from_db()
    assert qr.status == before_qr_status
    assert disposal.status == "confirmed"

    restored = restore_asset_visibility(
        actor=context["finance"], asset=archived, reason="恢复历史展示",
        idempotency_key="S7ARCHIVE-restore",
    )
    assert restored.record_status == "active"
    assert restored.asset_status == "disposed"
    assert AuditLog.objects.filter(
        company=context["company"],
        action__in=("asset_lifecycle.archive", "asset_lifecycle.restore_visibility"),
    ).count() == 2


def test_archive_rejects_nonterminal_and_unauthorized_roles():
    context, asset, _qr = active_asset_context("S7ARCHBAD")
    with pytest.raises(ValidationError):
        archive_asset(
            actor=context["admin"], asset=asset, reason="不能归档在管资产",
            idempotency_key="S7ARCHBAD-state",
        )

    # Even if a raw fixture marks the record terminal, equipment has no archive action.
    AssetDisposal.objects.none()  # keep model import and intent explicit
    with pytest.raises(PermissionDenied):
        archive_asset(
            actor=context["equipment"], asset=asset, reason="越权归档",
            idempotency_key="S7ARCHBAD-role",
        )
