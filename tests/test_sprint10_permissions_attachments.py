from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from apps.assets.models import AttachmentLink
from apps.offboarding.models import EmployeeAssetClearance
from apps.offboarding.permissions import (
    can_manage_clearance_attachment,
    can_view_clearance,
    can_view_clearance_attachment,
    scoped_clearance_items,
    scoped_clearances,
)
from apps.offboarding.services import (
    authorize_clearance_attachment_download,
    complete_clearance,
    initiate_clearance,
    transfer_clearance_item,
    upload_clearance_attachment,
    void_clearance_attachment,
)
from tests.test_sprint3_support import (
    JPEG_BYTES,
    PDF_BYTES,
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


def _jpeg(name="clearance.jpg", content_type="image/jpeg"):
    return SimpleUploadedFile(name, JPEG_BYTES, content_type=content_type)


def _pdf(name="clearance.pdf", content_type="application/pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type=content_type)


def test_clearance_read_scope_matrix_and_system_admin_default_deny():
    context = offboarding_context("S10SCOPE")
    asset, _ = formal_asset(context, "S10SCOPE-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10SCOPE-init",
    )
    item = clearance.items.get(asset=asset)
    manager = make_user("s10scope-manager", "department_manager")
    grant_scope(
        manager,
        context["company"],
        context["department"],
        descendants=False,
        assigned_by=context["admin"],
    )
    outsider = make_user("s10scope-outsider", "employee")

    for actor in (
        context["finance"],
        context["equipment"],
        context["hr"],
        context["management"],
        manager,
        context["employee_user"],
    ):
        assert can_view_clearance(actor, clearance)
        assert scoped_clearances(actor, context["company"]).filter(
            pk=clearance.pk
        ).exists()
        assert scoped_clearance_items(actor, context["company"]).filter(
            pk=item.pk
        ).exists()

    assert can_view_clearance(context["warehouse"], clearance)
    assert scoped_clearance_items(
        context["warehouse"], context["company"]
    ).filter(pk=item.pk).exists()

    for actor in (context["admin"], outsider):
        assert not can_view_clearance(actor, clearance)
        assert not scoped_clearances(actor, context["company"]).filter(
            pk=clearance.pk
        ).exists()
        assert not scoped_clearance_items(actor, context["company"]).filter(
            pk=item.pk
        ).exists()


def test_warehouse_scope_is_limited_to_unresolved_company_items():
    context = offboarding_context("S10WH")
    warehouse_employee = additional_employee(
        context, "S10WH-W", user=context["warehouse"]
    )
    asset, _ = formal_asset(context, "S10WH-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10WH-init",
    )
    item = clearance.items.get()
    assert can_view_clearance(context["warehouse"], clearance)

    item = transfer_clearance_item(
        actor=context["equipment"],
        item=item,
        to_department=warehouse_employee.department,
        to_responsible_employee=warehouse_employee,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="转交给仓库接收人",
        idempotency_key="S10WH-transfer",
    )
    assert item.movement.to_employee_id == warehouse_employee.pk
    assert can_view_clearance(context["warehouse"], clearance)
    assert scoped_clearance_items(
        context["warehouse"], context["company"]
    ).filter(pk=item.pk).exists()


@override_settings(MEDIA_ROOT="var/test-sprint10-item-attachment-scope")
def test_item_attachment_uses_item_scope_not_sibling_clearance_scope():
    context = offboarding_context("S10ATTSCOPE")
    formal_asset(context, "S10ATTSCOPE-IN")
    other_department = make_department(context["company"], "S10ATTSCOPE-D2")
    other_owner = make_employee(
        context["company"], other_department, "S10ATTSCOPE-E2"
    )
    sibling, _ = formal_asset(
        context, "S10ATTSCOPE-OUT", employee=other_owner
    )
    active_internal_loan(
        context, sibling, context["employee"], "S10ATTSCOPE-LOAN"
    )
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10ATTSCOPE-init",
    )
    manager = make_user("s10attscope-manager", "department_manager")
    grant_scope(
        manager,
        context["company"],
        context["department"],
        descendants=False,
        assigned_by=context["admin"],
    )
    sibling_item = clearance.items.get(asset=sibling)
    sibling_link = upload_clearance_attachment(
        actor=context["equipment"],
        target=sibling_item,
        uploaded_file=_jpeg("sibling.jpg"),
    )

    assert can_view_clearance(manager, clearance)
    assert not scoped_clearance_items(
        manager, context["company"]
    ).filter(pk=sibling_item.pk).exists()
    assert not can_view_clearance_attachment(manager, sibling_link)
    with pytest.raises(PermissionDenied):
        authorize_clearance_attachment_download(actor=manager, link=sibling_link)


@override_settings(MEDIA_ROOT="var/test-sprint10-attachments")
def test_a0_a1_attachment_permissions_private_storage_and_void_history():
    context = offboarding_context("S10ATT")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10ATT-init",
    )
    a0 = upload_clearance_attachment(
        actor=context["hr"],
        target=clearance,
        uploaded_file=_jpeg("ordinary-evidence.jpg"),
        security_class="A0",
    )
    a1 = upload_clearance_attachment(
        actor=context["finance"],
        target=clearance,
        uploaded_file=_pdf("finance-evidence.pdf"),
        security_class="A1",
    )
    assert a0.role == AttachmentLink.Role.CLEARANCE
    assert a0.clearance_id == clearance.pk and a0.clearance_item_id is None
    assert a1.clearance_id == clearance.pk and a1.clearance_item_id is None
    for link in (a0, a1):
        assert link.attachment.storage_key.startswith(
            f"private/assets/{context['company'].pk}/clearance/"
        )
        assert link.attachment.is_available is True
        assert link.attachment.malware_scan_status == "policy_limited"
        assert link.attachment.original_filename not in link.attachment.storage_key

    for actor in (
        context["hr"],
        context["finance"],
        context["equipment"],
        context["management"],
        context["employee_user"],
    ):
        assert can_view_clearance_attachment(actor, a0)
        assert authorize_clearance_attachment_download(
            actor=actor, link=a0
        ).pk == a0.pk
    for actor in (context["finance"], context["management"]):
        assert can_view_clearance_attachment(actor, a1)
        assert authorize_clearance_attachment_download(
            actor=actor, link=a1
        ).pk == a1.pk
    for actor in (
        context["hr"],
        context["equipment"],
        context["employee_user"],
        context["admin"],
    ):
        assert not can_view_clearance_attachment(actor, a1)
        with pytest.raises(PermissionDenied):
            authorize_clearance_attachment_download(actor=actor, link=a1)

    assert can_manage_clearance_attachment(
        context["finance"], clearance, security_class="A1"
    )
    assert not can_manage_clearance_attachment(
        context["management"], clearance, security_class="A1"
    )
    with pytest.raises(PermissionDenied):
        upload_clearance_attachment(
            actor=context["hr"],
            target=clearance,
            uploaded_file=_pdf("forged-a1.pdf"),
            security_class="A1",
        )
    with pytest.raises(PermissionDenied):
        void_clearance_attachment(
            actor=context["management"], link=a1, reason="无权作废"
        )
    voided = void_clearance_attachment(
        actor=context["finance"], link=a1, reason="证据更新需换版"
    )
    assert voided.status == "voided"
    assert voided.void_reason == "证据更新需换版"
    assert voided.voided_by_id == context["finance"].pk
    assert voided.attachment.is_available is True
    with pytest.raises(PermissionDenied):
        authorize_clearance_attachment_download(actor=context["finance"], link=voided)


@override_settings(MEDIA_ROOT="var/test-sprint10-attachment-validation")
def test_attachment_rejects_spoofed_mime_unsafe_extension_and_closed_target():
    context = offboarding_context("S10AVAL")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10AVAL-init",
    )
    before = AttachmentLink._base_manager.count()
    with pytest.raises(ValidationError):
        upload_clearance_attachment(
            actor=context["hr"],
            target=clearance,
            uploaded_file=_jpeg("spoof.jpg", content_type="application/pdf"),
        )
    with pytest.raises(ValidationError):
        upload_clearance_attachment(
            actor=context["hr"],
            target=clearance,
            uploaded_file=SimpleUploadedFile(
                "payload.exe", b"MZ-not-an-image", content_type="application/octet-stream"
            ),
        )
    assert AttachmentLink._base_manager.count() == before

    completed = complete_clearance(
        actor=context["hr"],
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    with pytest.raises(PermissionDenied):
        upload_clearance_attachment(
            actor=context["finance"],
            target=completed,
            uploaded_file=_jpeg("late.jpg"),
        )


def test_actor_set_null_keeps_clearance_and_resolution_evidence():
    context = offboarding_context("S10NULL")
    actor_hr = make_user("s10null-history-hr", "hr")
    actor_equipment = make_user("s10null-history-equipment", "equipment")
    receiver = additional_employee(context, "S10NULL-R")
    asset, _ = formal_asset(context, "S10NULL-A")
    clearance = initiate_clearance(
        actor=actor_hr,
        employee=context["employee"],
        idempotency_key="S10NULL-init",
    )
    item = transfer_clearance_item(
        actor=actor_equipment,
        item=clearance.items.get(),
        to_department=receiver.department,
        to_responsible_employee=receiver,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="SET_NULL 历史证据",
        idempotency_key="S10NULL-transfer",
    )
    clearance = complete_clearance(
        actor=actor_hr,
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    movement_id = item.movement_id
    clearance_id, item_id = clearance.pk, item.pk

    actor_hr.delete()
    actor_equipment.delete()

    clearance = EmployeeAssetClearance.objects.get(pk=clearance_id)
    item = clearance.items.get(pk=item_id)
    assert clearance.status == "completed"
    assert clearance.initiated_by_id is None
    assert clearance.completed_by_id is None
    assert item.resolution == "transferred"
    assert item.resolved_by_id is None
    assert item.movement_id == movement_id
