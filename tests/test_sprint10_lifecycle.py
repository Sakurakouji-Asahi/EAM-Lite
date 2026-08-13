from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.lifecycle_services import (
    _base_update,
    cancel_disposal,
    complete_disposal,
    initiate_disposal,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
    reverse_disposal,
)
from apps.assets.models import Asset, AssetMovement, AttachmentLink
from apps.offboarding.models import EmployeeAssetClearanceItem
from apps.offboarding.services import (
    complete_clearance,
    initiate_clearance,
    return_clearance_item,
    sync_clearance_item,
    transfer_clearance_item,
)
from tests.test_sprint3_support import (
    direct_attachment,
    grant_scope,
    make_department,
    make_employee,
    make_user,
)
from tests.test_sprint10_support import (
    active_internal_loan,
    additional_employee,
    formal_asset,
    offboarding_context,
)


pytestmark = pytest.mark.django_db


def _initiate_disposal(context, asset, key):
    today = timezone.localdate()
    return initiate_disposal(
        actor=context["equipment"],
        asset=asset,
        disposal_type="scrap",
        application_date=today,
        planned_disposal_date=today + timedelta(days=2),
        reason="Sprint 10 清退作废",
        description="清退过程现场核实",
        recipient_name="",
        handled_by=context["equipment"],
        idempotency_key=key,
        expected_status=asset.asset_status,
    )


def _lock_and_evidence(context, disposal, key):
    disposal = record_disposal_actual_details(
        actor=context["equipment"],
        disposal=disposal,
        actual_disposal_date=timezone.localdate(),
        recipient_name="",
        handled_by=context["equipment"],
        idempotency_key=f"{key}-actual",
    )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"],
        disposal=disposal,
        disposal_income="0.00",
        idempotency_key=f"{key}-finance",
    )
    attachment = direct_attachment(
        context["company"],
        context["equipment"],
        key=f"private/disposals/{key}.jpg",
        filename=f"{key}.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"],
        attachment=attachment,
        asset=None,
        asset_disposal=disposal,
        role=AttachmentLink.Role.DISPOSAL,
        security_class=AttachmentLink.SecurityClass.A0,
        created_by=context["equipment"],
    )
    return disposal


def test_responsibility_return_creates_movement_and_resolves_item():
    context = offboarding_context("S10RET")
    receiver = additional_employee(context, "S10RET-R")
    asset, _ = formal_asset(context, "S10RET-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10RET-init",
    )
    item = clearance.items.get()

    returned = return_clearance_item(
        actor=context["equipment"],
        item=item,
        returned_at=timezone.now(),
        received_by_employee=receiver,
        return_department=receiver.department,
        return_responsible_employee=receiver,
        return_location=context["location"],
        return_asset_status="idle",
        idempotency_key="S10RET-return",
        remark="清退归还入库",
    )

    returned.refresh_from_db()
    clearance.refresh_from_db()
    asset.refresh_from_db()
    assert returned.resolution == "returned"
    assert returned.resolved_by_id == context["equipment"].pk
    assert returned.resolved_at is not None
    assert returned.movement is not None
    assert returned.movement.movement_type == "assignment_return"
    assert returned.movement.from_employee_id == context["employee"].pk
    assert returned.movement.to_employee_id == receiver.pk
    assert asset.responsible_employee_id == receiver.pk
    assert asset.asset_status == "idle"
    assert clearance.unresolved_assets == 0
    assert clearance.status == "open"


def test_internal_loan_return_by_warehouse_resolves_with_structured_evidence():
    context = offboarding_context("S10LRET")
    owner = additional_employee(context, "S10LRET-O")
    warehouse_employee = additional_employee(
        context, "S10LRET-W", user=context["warehouse"]
    )
    asset, _ = formal_asset(context, "S10LRET-A", employee=owner)
    loan = active_internal_loan(
        context, asset, context["employee"], "S10LRET"
    )
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10LRET-init",
    )
    item = clearance.items.get()
    assert item.source_type == "internal_loan"

    returned = return_clearance_item(
        actor=context["warehouse"],
        item=item,
        returned_at=timezone.now(),
        received_by_employee=warehouse_employee,
        return_department=owner.department,
        return_responsible_employee=owner,
        return_location=context["location"],
        return_asset_status="in_use",
        idempotency_key="S10LRET-return",
    )

    returned.refresh_from_db()
    loan.refresh_from_db()
    assert loan.status == "returned"
    assert loan.return_movement_id == returned.movement_id
    assert returned.resolution == "returned"
    assert returned.movement.movement_type == "loan_return"
    assert returned.resolved_by_id == context["warehouse"].pk


def test_warehouse_service_can_receive_before_evidence_but_cannot_transfer():
    context = offboarding_context("S10WHSVC")
    receiver = additional_employee(context, "S10WHSVC-R")
    warehouse_employee = additional_employee(
        context, "S10WHSVC-W", user=context["warehouse"]
    )
    asset, _ = formal_asset(context, "S10WHSVC-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10WHSVC-init",
    )
    item = clearance.items.get()

    with pytest.raises(PermissionDenied):
        transfer_clearance_item(
            actor=context["warehouse"],
            item=item,
            to_department=receiver.department,
            to_responsible_employee=receiver,
            to_location=context["location"],
            effective_at=timezone.now(),
            reason="仓库不得执行转交",
            idempotency_key="S10WHSVC-transfer",
        )
    returned = return_clearance_item(
        actor=context["warehouse"],
        item=item,
        returned_at=timezone.now(),
        received_by_employee=warehouse_employee,
        return_department=receiver.department,
        return_responsible_employee=receiver,
        return_location=context["location"],
        return_asset_status="idle",
        idempotency_key="S10WHSVC-return",
    )
    assert returned.resolution == "returned"
    assert returned.movement.operated_by_id == context["warehouse"].pk


def test_both_source_remains_pending_until_responsibility_and_loan_are_both_cleared():
    context = offboarding_context("S10BOTH")
    receiver = additional_employee(context, "S10BOTH-R")
    asset, _ = formal_asset(context, "S10BOTH-A")
    active_internal_loan(context, asset, context["employee"], "S10BOTH")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10BOTH-init",
    )
    item = clearance.items.get()
    assert item.source_type == "both"

    # Simulate a controlled responsibility correction while the structured
    # loan remains active.  A single cleared source must not resolve `both`.
    _base_update(
        Asset,
        asset.pk,
        {
            "responsible_employee_id": receiver.pk,
            "department_id": receiver.department_id,
            "location_id": context["location"].pk,
            "updated_by_id": context["equipment"].pk,
        },
    )
    synced = sync_clearance_item(actor=context["equipment"], item=item)
    clearance.refresh_from_db()
    assert synced.resolution == "pending"
    assert clearance.unresolved_assets == 1

    returned = return_clearance_item(
        actor=context["equipment"],
        item=synced,
        returned_at=timezone.now(),
        received_by_employee=receiver,
        return_department=receiver.department,
        return_responsible_employee=receiver,
        return_location=context["location"],
        return_asset_status="in_use",
        idempotency_key="S10BOTH-return",
    )
    clearance.refresh_from_db()
    assert returned.resolution == "returned"
    assert clearance.unresolved_assets == 0


def test_department_manager_cannot_directly_sync_other_department_item():
    context = offboarding_context("S10SYNCSCOPE")
    manager = make_user("s10syncscope-manager", "department_manager")
    grant_scope(
        manager,
        context["company"],
        context["department"],
        descendants=False,
        assigned_by=context["admin"],
    )
    other_department = make_department(context["company"], "S10SYNCSCOPE-D2")
    other_owner = make_employee(
        context["company"], other_department, "S10SYNCSCOPE-E2"
    )
    asset, _ = formal_asset(
        context, "S10SYNCSCOPE-A", employee=other_owner
    )
    active_internal_loan(
        context, asset, context["employee"], "S10SYNCSCOPE-LOAN"
    )
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10SYNCSCOPE-init",
    )
    item = clearance.items.get()

    with pytest.raises(PermissionDenied, match="没有同步"):
        sync_clearance_item(actor=manager, item=item)
    item.refresh_from_db()
    assert item.resolution == EmployeeAssetClearanceItem.Resolution.PENDING


def test_transfer_resolves_and_preserves_original_snapshot_after_current_state_changes():
    context = offboarding_context("S10TRN")
    receiver = additional_employee(context, "S10TRN-R")
    asset, _ = formal_asset(context, "S10TRN-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10TRN-init",
    )
    item = clearance.items.get()
    original = (
        item.asset_code_snapshot,
        item.asset_name_snapshot,
        item.original_department_snapshot,
        item.original_employee_snapshot,
        item.original_location_path_snapshot,
        item.original_status,
    )

    moved = transfer_clearance_item(
        actor=context["equipment"],
        item=item,
        to_department=receiver.department,
        to_responsible_employee=receiver,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="离职资产转交",
        idempotency_key="S10TRN-transfer",
    )
    moved.refresh_from_db()
    asset.refresh_from_db()
    assert moved.resolution == "transferred"
    assert moved.movement.movement_type == "transfer"
    assert asset.responsible_employee_id == receiver.pk
    assert (
        moved.asset_code_snapshot,
        moved.asset_name_snapshot,
        moved.original_department_snapshot,
        moved.original_employee_snapshot,
        moved.original_location_path_snapshot,
        moved.original_status,
    ) == original

    repeated = transfer_clearance_item
    with pytest.raises(ValidationError):
        repeated(
            actor=context["equipment"],
            item=moved,
            to_department=receiver.department,
            to_responsible_employee=receiver,
            to_location=context["location"],
            effective_at=timezone.now(),
            reason="离职资产转交",
            idempotency_key="S10TRN-transfer",
        )


@pytest.mark.django_db(transaction=True)
def test_disposal_start_cancel_complete_sync_and_reversal_block():
    context = offboarding_context("S10DSP")
    asset, _ = formal_asset(context, "S10DSP-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10DSP-init",
    )
    item = clearance.items.get()

    first = _initiate_disposal(context, asset, "S10DSP-first")
    item.refresh_from_db()
    clearance.refresh_from_db()
    assert item.resolution == "disposal_in_progress"
    assert item.disposal_id == first.pk
    assert item.resolved_at is None
    assert clearance.unresolved_assets == 1
    cancel_disposal(
        actor=context["equipment"],
        disposal=first,
        reason="复核后取消处置",
        idempotency_key="S10DSP-cancel",
    )
    item.refresh_from_db()
    assert item.resolution == "pending"
    assert item.disposal_id is None

    asset.refresh_from_db()
    second = _initiate_disposal(context, asset, "S10DSP-second")
    second = _lock_and_evidence(context, second, "S10DSP-second")
    complete_disposal(
        actor=context["equipment"],
        disposal=second,
        idempotency_key="S10DSP-complete",
    )
    item.refresh_from_db()
    clearance.refresh_from_db()
    asset.refresh_from_db()
    assert item.resolution == "disposed"
    assert item.disposal_id == second.pk
    assert item.movement_id is None
    assert item.resolved_at is not None
    assert asset.asset_status == "disposed"
    # Terminal assets retain their historical responsible field; disposal is
    # nevertheless authoritative closure evidence.
    assert asset.responsible_employee_id == context["employee"].pk
    assert clearance.unresolved_assets == 0
    with pytest.raises(ValidationError):
        reverse_disposal(
            actor=context["finance"],
            disposal=second,
            reason="尝试冲销已用于清退的处置",
            idempotency_key="S10DSP-reverse",
        )


def test_hr_cannot_execute_return_transfer_or_disposal_and_active_loan_blocks_disposal():
    context = offboarding_context("S10ACT")
    owner = additional_employee(context, "S10ACT-O")
    responsibility, _ = formal_asset(context, "S10ACT-R")
    loaned, _ = formal_asset(context, "S10ACT-L", employee=owner)
    active_internal_loan(context, loaned, context["employee"], "S10ACT")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10ACT-init",
    )
    responsibility_item = clearance.items.get(asset=responsibility)
    loan_item = clearance.items.get(asset=loaned)
    with pytest.raises(PermissionDenied):
        transfer_clearance_item(
            actor=context["hr"],
            item=responsibility_item,
            to_department=owner.department,
            to_responsible_employee=owner,
            to_location=context["location"],
            effective_at=timezone.now(),
            reason="HR 不得执行转交",
            idempotency_key="S10ACT-hr-transfer",
        )
    with pytest.raises(PermissionDenied):
        return_clearance_item(
            actor=context["hr"],
            item=responsibility_item,
            returned_at=timezone.now(),
            received_by_employee=owner,
            return_department=owner.department,
            return_responsible_employee=owner,
            return_location=context["location"],
            return_asset_status="in_use",
            idempotency_key="S10ACT-hr-return",
        )
    with pytest.raises(PermissionDenied):
        _initiate_disposal({**context, "equipment": context["hr"]}, responsibility, "S10ACT-hr-dsp")
    with pytest.raises(ValidationError):
        _initiate_disposal(context, loaned, "S10ACT-loan-dsp")
    loan_item.refresh_from_db()
    assert loan_item.resolution == EmployeeAssetClearanceItem.Resolution.PENDING


def test_completed_clearance_is_immutable_to_later_lifecycle_sync():
    context = offboarding_context("S10DONE")
    receiver = additional_employee(context, "S10DONE-R")
    asset, _ = formal_asset(context, "S10DONE-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10DONE-init",
    )
    item = transfer_clearance_item(
        actor=context["equipment"],
        item=clearance.items.get(),
        to_department=receiver.department,
        to_responsible_employee=receiver,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="先转交再完成",
        idempotency_key="S10DONE-transfer",
    )
    completed = complete_clearance(
        actor=context["hr"],
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    assert completed.status == "completed"

    # Supplying unrelated later evidence to the public synchronizer must not
    # rewrite an item belonging to a completed historical clearance.
    movement = AssetMovement.objects.get(pk=item.movement_id)
    synced = sync_clearance_item(
        actor=context["equipment"], item=item, movement=movement
    )
    synced.refresh_from_db()
    completed.refresh_from_db()
    assert synced.resolution == "transferred"
    assert synced.movement_id == movement.pk
    assert completed.status == "completed"
    assert completed.unresolved_assets == 0
