from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.assets.lifecycle_services import (
    complete_disposal,
    correct_asset_code,
    initiate_disposal,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
    reverse_disposal,
)
from apps.assets.models import (
    Asset,
    AssetCodeHistory,
    AssetDisposal,
    AssetDisposalReversal,
    AssetMovement,
    AssetQrIdentity,
    AttachmentLink,
)
from apps.audit.models import AuditLog
from apps.finance.models import AssetDepreciationProfile, DepreciationProfileEvent
from apps.finance.services import confirm_depreciation_batch, generate_depreciation_batch
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_sprint3_support import direct_attachment, make_employee
from tests.test_sprint7_support import (
    active_asset_context,
    active_fixed_asset_context,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _completed_disposal(context, asset, key):
    today = timezone.localdate()
    disposal = initiate_disposal(
        actor=context["equipment"], asset=asset, disposal_type="scrap",
        application_date=today, planned_disposal_date=today,
        reason="达到报废条件", idempotency_key=f"{key}-start",
        expected_status=asset.asset_status,
    )
    disposal = record_disposal_actual_details(
        actor=context["equipment"], disposal=disposal,
        actual_disposal_date=today, handled_by=context["equipment"],
        idempotency_key=f"{key}-actual",
    )
    if AssetDepreciationProfile.objects.filter(
        asset=asset, status__in=("active", "suspended")
    ).exclude(method="no_depreciation").exists():
        period_start = today.replace(day=1)
        period_end = (
            period_start.replace(day=28) + timedelta(days=4)
        ).replace(day=1)
        batch = generate_depreciation_batch(
            actor=context["finance"], company=context["company"],
            period_start=period_start, period_end=period_end,
            idempotency_key=f"{key}-depreciation",
        )
        confirm_depreciation_batch(
            actor=context["finance"], batch=batch,
            reason="处置前确认必需折旧期间",
        )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"], disposal=disposal,
        disposal_income=Decimal("0.00"), idempotency_key=f"{key}-snapshot",
    )
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key=f"private/disposals/{key}.jpg", filename=f"{key}.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        asset_disposal=disposal, role="disposal", security_class="A0",
        created_by=context["equipment"],
    )
    disposal = complete_disposal(
        actor=context["equipment"], disposal=disposal,
        idempotency_key=f"{key}-complete",
    )
    return disposal


def test_code_correction_preserves_old_registry_and_history_and_rotates_qr():
    context, asset, old_qr = active_asset_context("S7CODE")
    old_issued = asset.current_issued_code
    old_code = asset.asset_code
    old_status = asset.asset_status
    counter = SequenceCounter.objects.get(
        company=context["company"], coding_scheme=old_issued.coding_scheme,
        scope_key=old_issued.scope_key,
    )
    before_value = counter.current_value

    new_issued = correct_asset_code(
        actor=context["admin"], asset=asset,
        effective_date=timezone.localdate(),
        idempotency_key="S7CODE-correct", reason="首次录入编号有误",
    )
    replayed = correct_asset_code(
        actor=context["admin"], asset=asset,
        effective_date=timezone.localdate(),
        idempotency_key="S7CODE-correct", reason="首次录入编号有误",
    )
    asset.refresh_from_db()
    old_issued.refresh_from_db()
    old_qr.refresh_from_db()
    counter.refresh_from_db()
    new_qr = AssetQrIdentity.objects.get(asset=asset, status="active")

    assert replayed.pk == new_issued.pk
    assert new_issued.pk != old_issued.pk
    assert asset.asset_code == new_issued.display_code != old_code
    assert asset.current_issued_code_id == new_issued.pk
    assert asset.asset_status == old_status
    assert old_issued.status == "replaced"
    assert IssuedCode.objects.filter(pk=old_issued.pk).exists()
    assert counter.current_value == before_value + 1
    assert AssetCodeHistory.objects.filter(
        asset=asset, event_type="corrected",
        old_issued_code=old_issued, new_issued_code=new_issued,
    ).count() == 1
    assert old_qr.status == "revoked"
    assert new_qr.version == old_qr.version + 1
    assert new_qr.label_status == "ready_to_print"
    assert AuditLog.objects.filter(action="asset_code.corrected").count() == 1


def test_code_correction_permission_collision_and_transaction_rollback(monkeypatch):
    context, asset, old_qr = active_asset_context("S7CODERB")
    old_issued = asset.current_issued_code
    counter = SequenceCounter.objects.get(
        company=context["company"], coding_scheme=old_issued.coding_scheme,
        scope_key=old_issued.scope_key,
    )
    before = {
        "code": asset.asset_code,
        "issued": asset.current_issued_code_id,
        "counter": counter.current_value,
        "qr": old_qr.pk,
    }
    with pytest.raises(PermissionDenied):
        correct_asset_code(
            actor=context["equipment"], asset=asset,
            effective_date=timezone.localdate(), idempotency_key="S7CODERB-denied",
            reason="越权编号修正",
        )

    # Fail after the new IssuedCode/history have been prepared; the atomic
    # boundary must restore the old counter, registry state and QR identity.
    monkeypatch.setattr(
        "apps.assets.qr_services.generate_public_token",
        lambda: (_ for _ in ()).throw(RuntimeError("simulated QR failure")),
    )
    with pytest.raises(RuntimeError, match="simulated QR failure"):
        correct_asset_code(
            actor=context["admin"], asset=asset,
            effective_date=timezone.localdate(), idempotency_key="S7CODERB-fail",
            reason="模拟中途失败",
        )

    asset.refresh_from_db()
    old_issued.refresh_from_db()
    old_qr.refresh_from_db()
    counter.refresh_from_db()
    assert asset.asset_code == before["code"]
    assert asset.current_issued_code_id == before["issued"]
    assert counter.current_value == before["counter"]
    assert old_issued.status == "active"
    assert old_qr.status == "active"
    assert AssetCodeHistory.objects.filter(
        asset=asset, event_type="corrected"
    ).count() == 0
    assert IssuedCode.objects.filter(company=context["company"]).count() == 1


def test_formal_asset_code_cannot_be_overwritten_outside_controlled_service():
    context, asset, _qr = active_asset_context("S7CODERAW")
    with pytest.raises(ValidationError):
        Asset.objects.filter(pk=asset.pk).update(asset_code="RAW-OVERWRITE")
    asset.asset_code = "RAW-SAVE"
    with pytest.raises(ValidationError):
        asset.save()


def test_reverse_disposal_is_finance_only_and_exactly_restores_own_stop_event():
    context, asset, _qr, profile, _policy = active_fixed_asset_context(
        "S7REVERSE", stop_rule="next_month"
    )
    # The reversal must restore the saved suspended state, not blindly active.
    from apps.finance.services import create_profile_event

    create_profile_event(
        actor=context["finance"], profile=profile, event_type="suspend",
        effective_date=timezone.localdate() - timedelta(days=1),
        reason="处置前暂停",
    )
    profile.refresh_from_db()
    disposal = _completed_disposal(context, asset, "S7REVERSE")
    stop = DepreciationProfileEvent.objects.get(
        source_disposal=disposal, depreciation_profile=profile,
        event_type="disposal_stop",
    )
    profile.refresh_from_db()
    assert stop.previous_profile_status == "suspended"
    expected_next_month = (
        timezone.localdate().replace(day=28) + timedelta(days=4)
    ).replace(day=1)
    assert stop.effective_date == expected_next_month
    assert profile.status == "stopped"

    with pytest.raises(PermissionDenied):
        reverse_disposal(
            actor=context["equipment"], disposal=disposal,
            reason="越权冲销", idempotency_key="S7REVERSE-denied",
        )
    reversal = reverse_disposal(
        actor=context["finance"], disposal=disposal,
        reason="终态类型录入错误", idempotency_key="S7REVERSE-ok",
    )
    replayed = reverse_disposal(
        actor=context["finance"], disposal=disposal,
        reason="终态类型录入错误", idempotency_key="S7REVERSE-ok",
    )
    asset.refresh_from_db()
    disposal.refresh_from_db()
    profile.refresh_from_db()
    restore = DepreciationProfileEvent.objects.get(reverses_event=stop)

    assert replayed.pk == reversal.pk
    assert disposal.status == "reversed"
    assert asset.asset_status == "in_use"
    assert profile.status == "suspended"
    assert restore.event_type == "disposal_restore"
    assert restore.source_disposal_id == disposal.pk
    assert restore.effective_date == stop.effective_date
    assert AssetDisposalReversal.objects.filter(asset_disposal=disposal).count() == 1
    assert AssetMovement.objects.filter(
        asset=asset, movement_type="disposal_reversal",
        from_status="disposed", to_status="in_use",
    ).count() == 1
    assert AttachmentLink.objects.filter(asset_disposal=disposal).count() == 1


def test_reversal_requires_replacement_if_original_responsible_employee_invalid():
    context, asset, _qr = active_asset_context("S7REPL")
    disposal = _completed_disposal(context, asset, "S7REPL")
    old_employee = context["employee"]
    old_employee.is_active = False
    old_employee.save(update_fields=("is_active",))

    with pytest.raises(ValidationError, match="替代责任人"):
        reverse_disposal(
            actor=context["finance"], disposal=disposal,
            reason="责任人失效测试", idempotency_key="S7REPL-missing",
        )
    replacement = make_employee(
        context["company"], context["department"], "S7REPL-E2"
    )
    reversal = reverse_disposal(
        actor=context["finance"], disposal=disposal,
        reason="责任人失效测试", idempotency_key="S7REPL-valid",
        replacement_responsible_employee=replacement,
    )
    asset.refresh_from_db()
    movement = AssetMovement.objects.get(
        asset=asset, movement_type="disposal_reversal"
    )
    assert reversal.restored_asset_status == "in_use"
    assert asset.responsible_employee_id == replacement.pk
    assert movement.from_employee_id == old_employee.pk
    assert movement.to_employee_id == replacement.pk


def test_reversal_blocks_later_business_and_rolls_back_all_partial_changes():
    context, asset, _qr, profile, _policy = active_fixed_asset_context(
        "S7REVBLK", stop_rule="event_date"
    )
    disposal = _completed_disposal(context, asset, "S7REVBLK")
    stop = DepreciationProfileEvent.objects.get(
        source_disposal=disposal, event_type="disposal_stop"
    )
    later_profile = AssetDepreciationProfile.objects.create(
        company=profile.company,
        asset=profile.asset,
        depreciation_policy=profile.depreciation_policy,
        version=profile.version + 1,
        method=profile.method,
        posting_period=profile.posting_period,
        start_rule=profile.start_rule,
        stop_rule=profile.stop_rule,
        start_date=profile.start_date,
        useful_life_months=profile.useful_life_months,
        salvage_mode=profile.salvage_mode,
        salvage_rate=profile.salvage_rate,
        salvage_amount=profile.salvage_amount,
        opening_book_value=profile.opening_book_value,
        opening_actual_accumulated_depreciation=(
            profile.opening_actual_accumulated_depreciation
        ),
        expected_total_units=profile.expected_total_units,
        work_unit=profile.work_unit,
        annual_posting_month=profile.annual_posting_month,
        effective_from=stop.effective_date,
        effective_to=None,
        status="draft",
        change_reason="处置后新增折旧 Profile 冲突",
        created_by=context["finance"],
    )
    with pytest.raises(ValidationError, match="新折旧 Profile"):
        reverse_disposal(
            actor=context["finance"], disposal=disposal,
            reason="尝试冲销", idempotency_key="S7REVBLK-key",
        )
    asset.refresh_from_db()
    disposal.refresh_from_db()
    assert asset.asset_status == "disposed"
    assert disposal.status == "confirmed"
    assert AssetDepreciationProfile.objects.filter(pk=later_profile.pk).exists()
    assert not AssetDisposalReversal.objects.filter(asset_disposal=disposal).exists()
    assert not DepreciationProfileEvent.objects.filter(reverses_event=stop).exists()
