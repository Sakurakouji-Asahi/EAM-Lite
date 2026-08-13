from __future__ import annotations

import os
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import AttachmentLink
from apps.audit.models import AuditLog
from apps.imports.cleanup import cleanup_unreferenced_private_files
from apps.inventory.permissions import can_view_inventory_attachment
from apps.inventory.services import (
    cancel_inventory_task,
    close_inventory_task,
    correct_inventory_resolution,
    create_inventory_surplus,
    create_inventory_task_draft,
    publish_inventory_task,
    require_inventory_attachment_download,
    resolve_inventory_difference,
    scan_inventory_asset,
    stop_inventory_scanning,
    upload_inventory_attachment,
    void_inventory_attachment,
)
from tests.test_sprint3_support import JPEG_BYTES, PDF_BYTES, direct_attachment, make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _draft, _task_data
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db


def test_task_creation_same_key_different_payload_conflicts():
    context, _asset, _qr = inventory_context("S8IDTASK")
    data = _task_data(context, "S8IDTASK-T")
    first = create_inventory_task_draft(
        actor=context["finance"], company=context["company"], data=data,
        assignee_users=[context["equipment"]],
    )
    replay = create_inventory_task_draft(
        actor=context["finance"], company=context["company"], data=data,
        assignee_users=[context["equipment"]],
    )
    assert replay.pk == first.pk
    changed = dict(data, name="相同幂等键的不同名称")
    with pytest.raises(ValidationError, match="不同盘点任务"):
        create_inventory_task_draft(
            actor=context["finance"], company=context["company"], data=changed,
            assignee_users=[context["equipment"]],
        )


def test_surplus_same_key_different_payload_conflicts():
    context, _asset, _qr = inventory_context("S8IDSUR")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8IDSUR-T")
    )
    first = create_inventory_surplus(
        actor=context["equipment"], task=task,
        temporary_name="未知工装 A", temporary_category_text="工装",
        temporary_location_text="车间 A",
        idempotency_key="S8IDSUR-key", remark="首次提交",
    )
    replay = create_inventory_surplus(
        actor=context["equipment"], task=task,
        temporary_name="未知工装 A", temporary_category_text="工装",
        temporary_location_text="车间 A",
        idempotency_key="S8IDSUR-key", remark="首次提交",
    )
    assert replay.pk == first.pk
    with pytest.raises(ValidationError, match="不同"):
        create_inventory_surplus(
            actor=context["equipment"], task=task,
            temporary_name="未知工装 B", temporary_category_text="模具",
            temporary_location_text="车间 B",
            idempotency_key="S8IDSUR-key", remark="不同载荷",
        )


def test_inventory_attachment_real_fk_and_field_security_scope():
    context, _asset, _qr = inventory_context("S8ATTPERM")
    assignee = make_user("s8-att-assignee", "employee")
    outsider = make_user("s8-att-outsider", "employee")
    management = make_user("s8-att-management", "management")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8ATTPERM-T", assignees=[assignee]),
    )
    surplus = create_inventory_surplus(
        actor=assignee, task=task, temporary_name="现场盘盈",
        temporary_category_text="工装", temporary_location_text="库位",
        idempotency_key="S8ATTPERM-surplus",
    )
    attachment = direct_attachment(
        context["company"], assignee,
        key="private/inventory/S8ATTPERM.jpg", filename="现场照片.jpg",
    )
    link = AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        inventory_surplus=surplus, role="surplus_evidence",
        security_class="A0", created_by=assignee,
    )
    assert link.inventory_surplus_id == surplus.pk
    assert link.asset_id is None and link.asset_disposal_id is None
    assert can_view_inventory_attachment(assignee, link)
    assert can_view_inventory_attachment(context["finance"], link)
    assert can_view_inventory_attachment(management, link)
    assert not can_view_inventory_attachment(outsider, link)

    # A1 business evidence remains finance/management-only even inside an
    # otherwise visible assigned task.
    link.security_class = "A1"
    assert not can_view_inventory_attachment(assignee, link)
    assert can_view_inventory_attachment(context["finance"], link)
    assert can_view_inventory_attachment(management, link)


def test_inventory_attachment_upload_download_void_and_http_scope(client):
    context, asset, qr = inventory_context("S8ATTHTTP")
    assignee = make_user("s8-att-http-assignee", "employee")
    outsider = make_user("s8-att-http-outsider", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8ATTHTTP-T", assignees=[assignee]),
    )
    scan = scan_inventory_asset(
        actor=assignee, task=task, qr_identity=qr,
        actual_location=asset.location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,
        idempotency_key="S8ATTHTTP-scan",
    )
    link = upload_inventory_attachment(
        actor=assignee, target=scan,
        uploaded_file=SimpleUploadedFile(
            "inventory.jpg", JPEG_BYTES, content_type="image/jpeg"
        ),
    )
    assert link.inventory_scan_id == scan.pk
    link.attachment.refresh_from_db()
    assert link.attachment.is_available
    assert link.attachment.storage_key.startswith("private/inventory/")
    assert require_inventory_attachment_download(actor=assignee, link=link).pk == link.pk
    with pytest.raises(PermissionDenied):
        require_inventory_attachment_download(actor=outsider, link=link)

    voided = void_inventory_attachment(
        actor=assignee, link=link, reason="现场照片拍摄错误"
    )
    assert voided.status == "voided"
    with pytest.raises(PermissionDenied):
        require_inventory_attachment_download(actor=assignee, link=link)
    with pytest.raises(ValidationError, match="不同原因"):
        void_inventory_attachment(actor=assignee, link=link, reason="另一原因")

    # Use a second active link for the HTTP download. Keep this last because
    # FileResponse completion emits request_finished, which closes PostgreSQL
    # connections under Django's default connection lifetime.
    active_link = upload_inventory_attachment(
        actor=assignee, target=scan,
        uploaded_file=SimpleUploadedFile(
            "inventory-active.jpg", JPEG_BYTES, content_type="image/jpeg"
        ),
    )

    url = reverse("inventory:attachment-download", args=[task.pk, active_link.pk])
    client.force_login(outsider)
    assert client.get(url).status_code == 404
    client.force_login(assignee)
    response = client.get(url)
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    for closer in response._resource_closers:
        closer()
    response._resource_closers.clear()


def test_surplus_evidence_requires_image_but_general_inventory_evidence_allows_pdf():
    context, asset, qr = inventory_context("S8ATTMIME")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8ATTMIME-T")
    )
    scan = scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=asset.location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,
        idempotency_key="S8ATTMIME-scan",
    )
    general = upload_inventory_attachment(
        actor=context["equipment"], target=scan,
        uploaded_file=SimpleUploadedFile(
            "inventory.pdf", PDF_BYTES, content_type="application/pdf"
        ),
    )
    assert general.role == AttachmentLink.Role.INVENTORY_EVIDENCE
    assert general.attachment.mime_type == "application/pdf"

    surplus = create_inventory_surplus(
        actor=context["equipment"], task=task,
        temporary_name="待确认盘盈物品", temporary_category_text="其他",
        temporary_location_text="现场", idempotency_key="S8ATTMIME-surplus",
    )
    with pytest.raises(ValidationError, match="必须上传图片"):
        upload_inventory_attachment(
            actor=context["equipment"], target=surplus,
            uploaded_file=SimpleUploadedFile(
                "surplus.pdf", PDF_BYTES, content_type="application/pdf"
            ),
        )
    assert not AttachmentLink.objects.filter(inventory_surplus=surplus).exists()


def test_same_key_different_payload_conflicts_for_scan_stop_cancel_and_resolution():
    context, asset, qr = inventory_context("S8IDOPS")
    _department, _employee, other_location = add_target_assignment(
        context, "S8IDOPS-N"
    )
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8IDOPS-T")
    )
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=other_location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,
        idempotency_key="S8IDOPS-scan", note="首次扫码",
    )
    with pytest.raises(ValidationError, match="不同请求"):
        scan_inventory_asset(
            actor=context["equipment"], task=task, qr_identity=qr,
            actual_location=other_location,
            actual_employee=asset.responsible_employee,
            actual_status=asset.asset_status,
            idempotency_key="S8IDOPS-scan", note="冲突载荷",
        )

    stop_inventory_scanning(
        actor=context["finance"], task=task, reason="进入差异处理",
        idempotency_key="S8IDOPS-stop",
    )
    with pytest.raises(ValidationError, match="不同请求参数"):
        stop_inventory_scanning(
            actor=context["finance"], task=task, reason="冲突停止原因",
            idempotency_key="S8IDOPS-stop",
        )
    row = task.task_assets.get()
    original = resolve_inventory_difference(
        actor=context["finance"], task_asset=row,
        resolution_type="master_confirmed", conclusion="主档无误",
        idempotency_key="S8IDOPS-resolution",
    )
    with pytest.raises(ValidationError, match="不同请求"):
        resolve_inventory_difference(
            actor=context["finance"], task_asset=row,
            resolution_type="master_confirmed", conclusion="不同结论",
            idempotency_key="S8IDOPS-resolution",
        )
    close_inventory_task(
        actor=context["finance"], task=task,
        idempotency_key="S8IDOPS-close",
    )
    corrected = correct_inventory_resolution(
        actor=context["finance"], resolution=original,
        resolution_type="other", conclusion="更正结论一",
        correction_reason="复核原因一", idempotency_key="S8IDOPS-correct",
    )
    assert corrected.status == "active"
    replay = correct_inventory_resolution(
        actor=context["finance"], resolution=original,
        resolution_type="other", conclusion="更正结论一",
        correction_reason="复核原因一", idempotency_key="S8IDOPS-correct",
    )
    assert replay.pk == corrected.pk
    with pytest.raises(ValidationError, match="不同"):
        correct_inventory_resolution(
            actor=context["finance"], resolution=original,
            resolution_type="other", conclusion="更正结论二",
            correction_reason="复核原因二", idempotency_key="S8IDOPS-correct",
        )
    with pytest.raises(ValidationError, match="不同"):
        correct_inventory_resolution(
            actor=context["finance"], resolution=original,
            resolution_type="master_confirmed", conclusion="更正结论一",
            correction_reason="复核原因一", idempotency_key="S8IDOPS-correct",
        )
    outsider = make_user("s8-idops-outsider", "employee")
    with pytest.raises(PermissionDenied):
        correct_inventory_resolution(
            actor=outsider, resolution=original,
            resolution_type="other", conclusion="更正结论一",
            correction_reason="复核原因一", idempotency_key="S8IDOPS-correct",
        )

    cancel_target = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8IDOPS-CANCEL")
    )
    cancel_inventory_task(
        actor=context["finance"], task=cancel_target, reason="业务取消",
        idempotency_key="S8IDOPS-cancel",
    )
    with pytest.raises(ValidationError, match="不同"):
        cancel_inventory_task(
            actor=context["finance"], task=cancel_target, reason="冲突取消原因",
            idempotency_key="S8IDOPS-cancel",
        )


def test_denied_retry_does_not_leak_or_return_existing_task():
    context, _asset, _qr = inventory_context("S8IDLEAK")
    data = _task_data(context, "S8IDLEAK-T")
    created = create_inventory_task_draft(
        actor=context["finance"], company=context["company"], data=data,
        assignee_users=[context["equipment"]],
    )
    attacker = make_user("s8-id-leak-attacker", "employee")
    with pytest.raises(PermissionDenied):
        create_inventory_task_draft(
            actor=attacker, company=context["company"], data=data,
            assignee_users=[attacker],
        )
    assert created.name not in str(PermissionDenied)


def test_inventory_private_orphan_file_cleanup_is_audited_and_idempotent(tmp_path):
    context, _asset, _qr = inventory_context("S8ATORPHAN")
    admin = make_user("s8-inventory-orphan-admin", "system_admin")
    with override_settings(MEDIA_ROOT=tmp_path):
        orphan_key = default_storage.save(
            f"private/inventory/{context['company'].pk}/orphan.jpg",
            ContentFile(JPEG_BYTES),
        )
        referenced = direct_attachment(
            context["company"], context["equipment"],
            key=f"private/inventory/{context['company'].pk}/referenced.jpg",
            filename="referenced.jpg",
        )
        default_storage.save(
            referenced.storage_key, ContentFile(JPEG_BYTES)
        )
        old_time = (timezone.now() - timedelta(days=8)).timestamp()
        os.utime(default_storage.path(orphan_key), (old_time, old_time))
        os.utime(
            default_storage.path(referenced.storage_key),
            (old_time, old_time),
        )

        first = cleanup_unreferenced_private_files(
            actor=admin, older_than_days=7, dry_run=False,
            task_id="S8ATORPHAN-cleanup",
            private_prefixes=("private/inventory",),
        )
        repeated = cleanup_unreferenced_private_files(
            actor=admin, older_than_days=7, dry_run=False,
            task_id="S8ATORPHAN-cleanup-repeat",
            private_prefixes=("private/inventory",),
        )

        assert orphan_key in first.legacy_files_deleted
        assert not default_storage.exists(orphan_key)
        assert repeated.legacy_files_deleted == []
        assert default_storage.exists(referenced.storage_key)
        assert AuditLog.objects.filter(
            action="inventory_private_file_cleanup",
            company=context["company"],
        ).count() == 1
