from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import AttachmentLink
from apps.audit.models import AuditLog
from apps.maintenance.services import (
    complete_maintenance,
    require_maintenance_attachment_download,
    upload_maintenance_attachment,
    void_maintenance_attachment,
)
from apps.masterdata.services import set_system_setting
from tests.test_sprint3_support import JPEG_BYTES, PDF_BYTES, make_user
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def _problem_record(ctx, key):
    return complete_maintenance(
        actor=ctx["responsible_user"],
        plan=ctx["plan"],
        scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="手机完成并记录现场实际内容",
        result="problem_found",
        problem_description="发现防护罩固定点松动",
        remark="已采取临时防护",
        idempotency_key=key,
    )


@override_settings(MEDIA_ROOT="var/test-media-sprint9-service")
def test_attachment_targets_a0_a1_download_scope_validation_and_void_history():
    ctx = maintenance_context("S9ATTSVC")
    management = make_user("s9attsvc-management", "management")
    record = _problem_record(ctx, "S9ATTSVC-complete")
    a0_record = upload_maintenance_attachment(
        actor=ctx["responsible_user"],
        target=record,
        uploaded_file=SimpleUploadedFile(
            "completion.jpg", JPEG_BYTES, content_type="image/jpeg"
        ),
        security_class="A0",
    )
    a0_problem = upload_maintenance_attachment(
        actor=ctx["equipment"],
        target=record.problem,
        uploaded_file=SimpleUploadedFile(
            "followup.pdf", PDF_BYTES, content_type="application/pdf"
        ),
        security_class="A0",
    )
    a1_record = upload_maintenance_attachment(
        actor=ctx["finance"],
        target=record,
        uploaded_file=SimpleUploadedFile(
            "financial.pdf", PDF_BYTES, content_type="application/pdf"
        ),
        security_class="A1",
    )

    assert a0_record.maintenance_record_id == record.pk
    assert a0_record.maintenance_problem_id is None and a0_record.asset_id is None
    assert a0_problem.maintenance_problem_id == record.problem.pk
    assert a0_problem.maintenance_record_id is None and a0_problem.asset_id is None
    assert a1_record.security_class == "A1"
    assert require_maintenance_attachment_download(
        actor=ctx["responsible_user"], link=a0_record
    ).pk == a0_record.pk
    assert require_maintenance_attachment_download(
        actor=ctx["finance"], link=a1_record
    ).pk == a1_record.pk
    assert require_maintenance_attachment_download(
        actor=management, link=a1_record
    ).pk == a1_record.pk
    with pytest.raises(PermissionDenied):
        require_maintenance_attachment_download(
            actor=ctx["responsible_user"], link=a1_record
        )
    with pytest.raises(PermissionDenied):
        upload_maintenance_attachment(
            actor=management,
            target=record,
            uploaded_file=SimpleUploadedFile(
                "readonly.jpg", JPEG_BYTES, content_type="image/jpeg"
            ),
        )

    for name, content, mime in (
        ("double.jpg.exe", JPEG_BYTES, "image/jpeg"),
        ("fake.jpg", PDF_BYTES, "image/jpeg"),
        ("mime.jpg", JPEG_BYTES, "application/pdf"),
    ):
        with pytest.raises(ValidationError):
            upload_maintenance_attachment(
                actor=ctx["equipment"],
                target=record,
                uploaded_file=SimpleUploadedFile(name, content, content_type=mime),
            )

    set_system_setting(
        actor=ctx["admin"],
        company=ctx["company"],
        key="attachment_max_size_bytes",
        value=max(1, len(JPEG_BYTES) - 1),
    )
    with pytest.raises(ValidationError, match="超过当前上限"):
        upload_maintenance_attachment(
            actor=ctx["equipment"],
            target=record,
            uploaded_file=SimpleUploadedFile(
                "oversize.jpg", JPEG_BYTES, content_type="image/jpeg"
            ),
        )

    voided = void_maintenance_attachment(
        actor=ctx["equipment"],
        link=a0_record,
        reason="现场照片选错",
    )
    assert voided.status == "voided" and voided.void_reason == "现场照片选错"
    assert voided.attachment.is_available
    with pytest.raises(PermissionDenied):
        require_maintenance_attachment_download(
            actor=ctx["equipment"], link=voided
        )
    assert AuditLog.objects.filter(
        action="maintenance.attachment_uploaded"
    ).count() == 3
    assert AuditLog.objects.filter(
        action="maintenance.attachment_voided"
    ).count() == 1


def test_home_due_detail_qr_mobile_completion_and_multipart_upload_http(tmp_path):
    ctx = maintenance_context("S9HTTP")
    client = Client()
    client.force_login(ctx["equipment"])

    home = client.get(reverse("home"))
    due = client.get(reverse("maintenance:due-list"))
    assert home.status_code == due.status_code == 200
    assert home.context["maintenance_counts"] == due.context["counts"]
    assert home.context["maintenance_counts"] == {
        "upcoming": 1,
        "due_today": 0,
        "overdue": 0,
    }

    detail = client.get(reverse("assets:asset-detail", args=[ctx["asset"].pk]))
    qr = client.get(
        reverse("assets:qr-scan", args=[ctx["qr_identity"].public_token])
    )
    complete_url = reverse("maintenance:plan-complete", args=[ctx["plan"].pk])
    assert detail.status_code == qr.status_code == 200
    assert "预防性保养" in detail.content.decode()
    assert complete_url in qr.content.decode()
    assert "原值" not in qr.content.decode()

    mobile = client.get(complete_url, HTTP_USER_AGENT="Mozilla/5.0 Mobile")
    html = mobile.content.decode()
    assert mobile.status_code == 200
    assert 'enctype="multipart/form-data"' in html
    assert "maintenance-mobile-action" in html
    assert "计划日期" in html and "实际完成日期" in html
    assert "runtime_hour" not in html and "严重级别" not in html
    assert 'name="scheduled_date"' in html and "disabled" in html

    record = _problem_record(ctx, "S9HTTP-complete")
    upload_url = reverse(
        "maintenance:attachment-upload", args=["record", record.pk]
    )
    with override_settings(MEDIA_ROOT=tmp_path):
        uploaded = client.post(
            upload_url,
            {
                "uploaded_file": SimpleUploadedFile(
                    "mobile.jpg", JPEG_BYTES, content_type="image/jpeg"
                )
            },
        )
        assert uploaded.status_code == 302
        link = AttachmentLink.objects.get(
            maintenance_record=record,
            attachment__original_filename="mobile.jpg",
        )
        download = client.get(
            reverse("maintenance:attachment-download", args=[link.pk])
        )
        assert download.status_code == 200
        assert download["Content-Disposition"] == "attachment"
        assert download["Cache-Control"] == "private, no-store"
        assert download["X-Content-Type-Options"] == "nosniff"

        outsider = make_user("s9http-outsider", "employee")
        client.force_login(outsider)
        assert client.get(
            reverse("maintenance:attachment-download", args=[link.pk])
        ).status_code == 403


def test_assigned_employee_qr_scope_can_complete_via_mobile_http():
    ctx = maintenance_context("S9ASSIGNEDQR")
    client = Client()
    client.force_login(ctx["responsible_user"])
    qr_url = reverse(
        "assets:qr-scan", args=[ctx["qr_identity"].public_token]
    )
    complete_url = reverse("maintenance:plan-complete", args=[ctx["plan"].pk])

    scanned = client.get(qr_url, HTTP_USER_AGENT="Mozilla/5.0 Mobile")
    body = scanned.content.decode()
    assert scanned.status_code == 200
    assert ctx["plan"].name in body
    assert complete_url in body
    assert "原值" not in body and "账面净值" not in body

    completed = client.post(
        complete_url,
        {
            "idempotency_key": "S9ASSIGNEDQR-complete",
            "scheduled_date": ctx["plan"].next_maintenance_date.isoformat(),
            "completed_date": timezone.localdate().isoformat(),
            "actual_content": "责任人扫码后完成现场保养",
            "result": "normal",
            "problem_description": "",
            "remark": "手机端提交",
        },
        HTTP_USER_AGENT="Mozilla/5.0 Mobile",
    )
    assert completed.status_code == 302
    assert ctx["plan"].records.filter(status="confirmed").count() == 1
