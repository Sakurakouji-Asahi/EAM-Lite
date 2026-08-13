from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.models import Asset, AssetMovement, AttachmentLink
from apps.audit.models import AuditLog
from apps.inventory.models import InventoryResolution, InventorySurplus
from apps.inventory.services import (
    cancel_inventory_task,
    close_inventory_task,
    convert_surplus_to_asset_draft,
    correct_inventory_resolution,
    create_inventory_surplus,
    inventory_task_summary,
    publish_inventory_task,
    resolve_inventory_difference,
    resolve_inventory_surplus,
    scan_inventory_asset,
    stop_inventory_scanning,
    supplemental_scan,
    void_inventory_attachment,
)
from tests.test_sprint3_support import (
    complete_asset_data,
    direct_attachment,
    make_user,
)
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint7_support import add_department_manager
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import add_active_asset, inventory_context


pytestmark = pytest.mark.django_db


def _published(context, key, **kwargs):
    return publish_inventory_task(
        actor=context["finance"], task=_draft(context, key, **kwargs)
    )


def _stop(context, task, key):
    return stop_inventory_scanning(
        actor=context["finance"], task=task,
        reason="进入差异处理", idempotency_key=f"{key}-stop",
    )


def test_abnormal_and_missing_need_one_active_resolution_but_normal_does_not():
    context, first_asset, first_qr = inventory_context("S8RES")
    second_asset, _second_qr = add_active_asset(context, "S8RES-SECOND")
    _department, _employee, other_location = add_target_assignment(
        context, "S8RES-N"
    )
    task = _published(context, "S8RES-T")
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=first_qr,
        actual_location=other_location,
        actual_employee=first_asset.responsible_employee,
        actual_status=first_asset.asset_status,
        idempotency_key="S8RES-scan",
    )
    task = _stop(context, task, "S8RES")
    abnormal = task.task_assets.get(asset=first_asset)
    missing = task.task_assets.get(asset=second_asset)
    first = resolve_inventory_difference(
        actor=context["finance"], task_asset=abnormal,
        resolution_type="master_confirmed",
        conclusion="现场摆放临时变化，主档无误",
        idempotency_key="S8RES-abnormal",
    )
    replay = resolve_inventory_difference(
        actor=context["finance"], task_asset=abnormal,
        resolution_type="master_confirmed",
        conclusion="现场摆放临时变化，主档无误",
        idempotency_key="S8RES-abnormal",
    )
    assert replay.pk == first.pk
    with pytest.raises(ValidationError, match="已有当前有效结论"):
        resolve_inventory_difference(
            actor=context["finance"], task_asset=abnormal,
            resolution_type="other", conclusion="重复结论",
            idempotency_key="S8RES-duplicate-active",
        )
    resolve_inventory_difference(
        actor=context["finance"], task_asset=missing,
        resolution_type="loss_confirmed", conclusion="现场确认盘亏，后续另行处置",
        idempotency_key="S8RES-missing",
    )
    assert Asset.objects.filter(pk=second_asset.pk).exists()
    assert InventoryResolution.objects.filter(status="active").count() == 2
    assert inventory_task_summary(task)["unresolved"] == 0
    assert close_inventory_task(
        actor=context["finance"], task=task, idempotency_key="S8RES-close"
    ).status == "closed"


def test_normal_scan_is_direct_completion_evidence_and_forbids_fake_resolution():
    context, asset, qr = inventory_context("S8NORMAL")
    task = _published(context, "S8NORMAL-T")
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=asset.location, actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status, idempotency_key="S8NORMAL-scan",
    )
    task = _stop(context, task, "S8NORMAL")
    row = task.task_assets.get()
    with pytest.raises(ValidationError, match="正常扫码"):
        resolve_inventory_difference(
            actor=context["finance"], task_asset=row,
            resolution_type="master_confirmed", conclusion="不应伪造",
            idempotency_key="S8NORMAL-fake",
        )
    assert close_inventory_task(
        actor=context["finance"], task=task, idempotency_key="S8NORMAL-close"
    ).status == "closed"
    assert not row.resolutions.exists()


def test_master_updated_resolution_reuses_lifecycle_movement_service():
    context, asset, qr = inventory_context("S8MASTER")
    department, employee, location = add_target_assignment(context, "S8MASTER-N")
    task = _published(context, "S8MASTER-T")
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=location, actual_employee=employee,
        actual_status="in_use", idempotency_key="S8MASTER-scan",
    )
    task = _stop(context, task, "S8MASTER")
    resolution = resolve_inventory_difference(
        actor=context["equipment"], task_asset=task.task_assets.get(),
        resolution_type="master_updated", conclusion="按现场执行正式调拨",
        idempotency_key="S8MASTER-resolve", to_department=department,
        to_responsible_employee=employee, to_location=location,
        effective_at=timezone.now() - timedelta(seconds=1),
    )
    asset.refresh_from_db()
    assert resolution.movement_id is not None
    assert resolution.movement.movement_type == "transfer"
    assert AssetMovement.objects.filter(pk=resolution.movement_id).exists()
    assert (asset.department_id, asset.responsible_employee_id, asset.location_id) == (
        department.pk, employee.pk, location.pk,
    )


def test_surplus_has_no_asset_id_converts_once_to_unissued_draft_and_keeps_evidence():
    context, _asset, _qr = inventory_context("S8SURPLUS")
    task = _published(context, "S8SURPLUS-T")
    surplus = create_inventory_surplus(
        actor=context["equipment"], task=task,
        temporary_name="现场未知工装", temporary_category_text="工装",
        temporary_location_text="一号车间角落",
        idempotency_key="S8SURPLUS-create", remark="等待财务确认",
    )
    assert not hasattr(surplus, "asset_id")
    assert surplus.linked_asset_id is None
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/inventory/S8SURPLUS.jpg", filename="盘盈照片.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        inventory_surplus=surplus, role="surplus_evidence",
        security_class="A0", created_by=context["equipment"],
    )
    task = _stop(context, task, "S8SURPLUS")
    data = complete_asset_data(
        context["category"], context["department"], context["employee"],
        context["location"], asset_name="盘盈转建草稿", serial_number="S8-SURPLUS",
    )
    draft = convert_surplus_to_asset_draft(
        actor=context["finance"], surplus=surplus, asset_data=data,
        idempotency_key="S8SURPLUS-convert", remark="财务确认转资产草稿",
    )
    replay = convert_surplus_to_asset_draft(
        actor=context["finance"], surplus=surplus, asset_data=data,
        idempotency_key="S8SURPLUS-convert", remark="财务确认转资产草稿",
    )
    surplus.refresh_from_db()
    assert replay.pk == draft.pk == surplus.linked_asset_id
    assert draft.asset_status == "draft"
    assert draft.asset_code is None and draft.current_issued_code_id is None
    assert Asset.objects.filter(asset_name="盘盈转建草稿").count() == 1
    assert AttachmentLink.objects.filter(
        inventory_surplus=surplus, status="active"
    ).count() == 1


def test_unresolved_surplus_blocks_close_then_resolution_allows_close():
    context, asset, qr = inventory_context("S8SURBLOCK")
    task = _published(context, "S8SURBLOCK-T")
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=asset.location, actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status, idempotency_key="S8SURBLOCK-scan",
    )
    surplus = create_inventory_surplus(
        actor=context["equipment"], task=task, temporary_name="非公司物品",
        temporary_category_text="未知", temporary_location_text="库房",
        idempotency_key="S8SURBLOCK-create",
    )
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/inventory/S8SURBLOCK.jpg", filename="非公司物品现场照片.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        inventory_surplus=surplus, role="surplus_evidence",
        security_class="A0", created_by=context["equipment"],
    )
    task = _stop(context, task, "S8SURBLOCK")
    with pytest.raises(ValidationError, match="仍有 1 项"):
        close_inventory_task(actor=context["finance"], task=task)
    resolve_inventory_surplus(
        actor=context["finance"], surplus=surplus,
        resolution_status="not_company", remark="核查后确认非公司资产",
    )
    assert close_inventory_task(actor=context["finance"], task=task).status == "closed"


def test_surplus_without_photo_cannot_be_resolved_converted_or_closed():
    context, asset, qr = inventory_context("S8SURPHOTO")
    task = _published(context, "S8SURPHOTO-T")
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=asset.location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,
        idempotency_key="S8SURPHOTO-scan",
    )
    surplus = create_inventory_surplus(
        actor=context["equipment"], task=task, temporary_name="缺少照片的盘盈",
        temporary_category_text="工装", temporary_location_text="现场",
        idempotency_key="S8SURPHOTO-create",
    )
    task = _stop(context, task, "S8SURPHOTO")
    with pytest.raises(ValidationError, match="至少一张有效照片"):
        resolve_inventory_surplus(
            actor=context["finance"], surplus=surplus,
            resolution_status="other", remark="暂不处理",
            idempotency_key="S8SURPHOTO-resolve",
        )
    data = complete_asset_data(
        context["category"], context["department"], context["employee"],
        context["location"], asset_name="不应创建的盘盈草稿",
        serial_number="S8-SURPHOTO",
    )
    with pytest.raises(ValidationError, match="至少一张有效照片"):
        convert_surplus_to_asset_draft(
            actor=context["finance"], surplus=surplus, asset_data=data,
            idempotency_key="S8SURPHOTO-convert", remark="无照片不得转换",
        )
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/inventory/S8SURPHOTO.jpg", filename="盘盈补充照片.jpg",
    )
    link = AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        inventory_surplus=surplus, role="surplus_evidence",
        security_class="A0", created_by=context["equipment"],
    )
    resolve_inventory_surplus(
        actor=context["finance"], surplus=surplus,
        resolution_status="other", remark="有证据后确认其他处理",
        idempotency_key="S8SURPHOTO-resolve-after-photo",
    )
    void_inventory_attachment(
        actor=context["finance"], link=link, reason="照片复核无效"
    )
    # Closing rechecks that the evidence is still active; a once-present but
    # voided photograph is insufficient.
    with pytest.raises(ValidationError, match="至少一张有效照片"):
        close_inventory_task(
            actor=context["finance"], task=task,
            idempotency_key="S8SURPHOTO-close",
        )
    task.refresh_from_db()
    assert task.status == "reconciliation"
    assert not Asset.objects.filter(asset_name="不应创建的盘盈草稿").exists()


def test_only_finance_can_confirm_any_surplus_resolution():
    context, _asset, _qr = inventory_context("S8SURFIN")
    task = _published(context, "S8SURFIN-T")
    surplus = create_inventory_surplus(
        actor=context["equipment"], task=task,
        temporary_name="待财务确认盘盈",
        temporary_category_text="工装", temporary_location_text="现场",
        idempotency_key="S8SURFIN-create",
    )
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/inventory/S8SURFIN.jpg", filename="盘盈照片.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        inventory_surplus=surplus, role="surplus_evidence",
        security_class="A0", created_by=context["equipment"],
    )
    task = _stop(context, task, "S8SURFIN")
    manager = add_department_manager(
        context, "S8SURFIN", context["department"]
    )
    for actor in (context["equipment"], manager):
        with pytest.raises(PermissionDenied):
            resolve_inventory_surplus(
                actor=actor, surplus=surplus,
                resolution_status="not_company", remark="越权确认",
                idempotency_key=f"S8SURFIN-{actor.pk}",
            )
    resolved = resolve_inventory_surplus(
        actor=context["finance"], surplus=surplus,
        resolution_status="not_company", remark="财务确认非公司资产",
        idempotency_key="S8SURFIN-finance",
    )
    assert resolved.resolution_status == "not_company"


def test_supplemental_scan_rejects_row_with_active_resolution():
    context, asset, qr = inventory_context("S8SUPRES")
    task = _stop(context, _published(context, "S8SUPRES-T"), "S8SUPRES")
    row = task.task_assets.get(asset=asset)
    resolve_inventory_difference(
        actor=context["finance"], task_asset=row,
        resolution_type="master_confirmed", conclusion="确认未盘但保留主档",
        idempotency_key="S8SUPRES-resolution",
    )
    with pytest.raises(ValidationError, match="已有有效处理结论"):
        supplemental_scan(
            actor=context["finance"], task_asset=row, qr_identity=qr,
            actual_location=asset.location,
            actual_employee=asset.responsible_employee,
            actual_status=asset.asset_status,
            supplement_reason="结论后不得补盘",
            idempotency_key="S8SUPRES-supplement",
        )


def test_cancel_keeps_all_evidence_and_assignee_cannot_cancel():
    context, asset, qr = inventory_context("S8CANCEL")
    assignee = make_user("s8-cancel-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8CANCEL-T", assignees=[assignee]),
    )
    scan = scan_inventory_asset(
        actor=assignee, task=task, qr_identity=qr,
        actual_location=asset.location, actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status, idempotency_key="S8CANCEL-scan",
    )
    with pytest.raises(PermissionDenied):
        cancel_inventory_task(actor=assignee, task=task, reason="越权取消")
    cancelled = cancel_inventory_task(
        actor=context["finance"], task=task, reason="业务计划调整",
        idempotency_key="S8CANCEL-cancel",
    )
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_by_id == context["finance"].pk
    assert cancelled.cancelled_at is not None
    assert cancelled.cancellation_reason == "业务计划调整"
    assert cancelled.assignees.filter(user=assignee).exists()
    assert cancelled.task_assets.exists()
    assert cancelled.scans.filter(pk=scan.pk).exists()


def test_closed_resolution_correction_is_append_only_and_does_not_reopen_scanning():
    context, asset, qr = inventory_context("S8CORRECT")
    _department, _employee, other_location = add_target_assignment(
        context, "S8CORRECT-N"
    )
    task = _published(context, "S8CORRECT-T")
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=other_location, actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status, idempotency_key="S8CORRECT-scan",
    )
    task = _stop(context, task, "S8CORRECT")
    original = resolve_inventory_difference(
        actor=context["finance"], task_asset=task.task_assets.get(),
        resolution_type="master_confirmed", conclusion="初次核查结论",
        idempotency_key="S8CORRECT-original",
    )
    task = close_inventory_task(actor=context["finance"], task=task)
    corrected = correct_inventory_resolution(
        actor=context["finance"], resolution=original,
        resolution_type="other", conclusion="更正后的结论",
        correction_reason="复核发现结论描述有误",
        idempotency_key="S8CORRECT-new",
    )
    original.refresh_from_db()
    task.refresh_from_db()
    assert original.status == "superseded"
    assert corrected.status == "active"
    assert corrected.supersedes_resolution_id == original.pk
    assert corrected.correction_reason == "复核发现结论描述有误"
    assert InventoryResolution.objects.filter(inventory_task_asset=original.inventory_task_asset).count() == 2
    assert task.status == "closed"
    with pytest.raises(PermissionDenied):
        scan_inventory_asset(
            actor=context["equipment"], task=task, qr_identity=qr,
            actual_location=asset.location, actual_employee=asset.responsible_employee,
            actual_status=asset.asset_status, idempotency_key="S8CORRECT-late-scan",
        )
    assert AuditLog.objects.filter(action="inventory.resolution_corrected").count() == 1


def test_critical_inventory_operations_write_append_only_audit_events():
    context, asset, qr = inventory_context("S8AUDIT")
    task = _published(context, "S8AUDIT-T")
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=asset.location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,
        idempotency_key="S8AUDIT-scan",
    )
    task = _stop(context, task, "S8AUDIT")
    close_inventory_task(
        actor=context["finance"], task=task,
        idempotency_key="S8AUDIT-close",
    )
    assert set(
        AuditLog.objects.filter(
            action__in={
                "inventory.task_created",
                "inventory.task_published",
                "inventory.asset_scanned",
                "inventory.scanning_stopped",
                "inventory.task_closed",
            }
        ).values_list("action", flat=True)
    ) == {
        "inventory.task_created",
        "inventory.task_published",
        "inventory.asset_scanned",
        "inventory.scanning_stopped",
        "inventory.task_closed",
    }
