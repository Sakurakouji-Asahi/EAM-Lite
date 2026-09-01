from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import AttachmentLink
from apps.maintenance.models import MaintenancePlan, MaintenanceProblem, MaintenanceRecord
from apps.maintenance.services import (
    complete_maintenance,
    upload_maintenance_attachment,
    void_maintenance_record,
)
from tests.test_sprint3_support import (
    JPEG_BYTES,
    PDF_BYTES,
    complete_initialization,
    make_company,
    make_user,
)
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def _complete(ctx, key, *, result="normal"):
    return complete_maintenance(
        actor=ctx["responsible_user"],
        plan=ctx["plan"],
        scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="按标准完成检查、清洁和紧固",
        result=result,
        problem_description="发现防护罩螺栓松动" if result == "problem_found" else "",
        remark="手机端 HTTP 验收",
        idempotency_key=key,
    )


def test_equipment_opens_and_submits_plan_form_without_runtime_hour(client):
    ctx = maintenance_context("S9HTTPPLAN")
    create_url = reverse("maintenance:plan-create")
    client.force_login(ctx["equipment"])

    page = client.get(create_url, {"asset": ctx["asset"].pk})
    html = page.content.decode()
    assert page.status_code == 200
    assert str(page.context["form"]["asset"].value()) == str(ctx["asset"].pk)
    assert all(f'value="{unit}"' in html for unit in ("day", "week", "month", "year"))
    assert "runtime_hour" not in html

    first_due = timezone.localdate() + timedelta(days=5)
    response = client.post(
        create_url,
        {
            "asset": str(ctx["asset"].pk),
            "name": "每两周安全检查",
            "cycle_value": "2",
            "cycle_unit": "week",
            "responsible_employee": str(ctx["responsible"].pk),
            "advance_notice_days": "2",
            "standard_content": "检查防护、润滑和紧固状态",
            "first_due_date": first_due.isoformat(),
        },
    )

    plan = MaintenancePlan.objects.get(asset=ctx["asset"], name="每两周安全检查")
    assert response.status_code == 302
    assert response.url == reverse("maintenance:plan-detail", args=[plan.pk])
    assert plan.next_maintenance_date == first_due
    assert plan.cycle_unit == "week" and plan.cycle_value == 2


def test_empty_maintenance_asset_selector_explains_prerequisites(client):
    company = make_company("S9HTTPEMPTY")
    equipment = make_user("s9-http-empty-equipment", "equipment")
    complete_initialization(company, equipment)
    client.force_login(equipment)

    response = client.get(reverse("maintenance:plan-create"))

    assert response.status_code == 200
    assert response.context["has_eligible_assets"] is False
    html = response.content.decode()
    assert "当前没有可建立保养计划的资产" in html
    assert reverse("assets:label-queue") in html
    assert reverse("assets:asset-list") in html
    assert '<button class="btn btn-primary" type="submit" disabled>' in html


@pytest.mark.django_db(transaction=True)
def test_home_and_due_list_share_the_same_due_query_and_surface(client):
    ctx = maintenance_context("S9HTTPDUE")
    client.force_login(ctx["responsible_user"])

    home = client.get(reverse("home"))
    due = client.get(reverse("maintenance:due-list"))

    assert home.status_code == due.status_code == 200
    assert home.context["maintenance_counts"] == due.context["counts"]
    assert home.context["maintenance_counts"] == {
        "upcoming": 1,
        "due_today": 0,
        "overdue": 0,
    }
    assert [item["plan"].pk for item in home.context["maintenance_items"]] == [
        item["plan"].pk for item in due.context["items"]
    ]
    for response in (home, due):
        html = response.content.decode()
        assert ctx["plan"].name in html
        assert "即将到期：1" in html
        assert "runtime_hour" not in html


def test_assignee_scans_and_completes_on_mobile_without_f1_or_hr_leak(client):
    ctx = maintenance_context("S9HTTPQR")
    scan_url = reverse("assets:qr-scan", args=[ctx["qr_identity"].public_token])
    complete_url = reverse("maintenance:plan-complete", args=[ctx["plan"].pk])
    client.force_login(ctx["responsible_user"])

    scan = client.get(scan_url, HTTP_USER_AGENT="Mozilla/5.0 Mobile")
    scan_html = scan.content.decode()
    assert scan.status_code == 200
    assert "保养摘要" in scan_html and ctx["plan"].name in scan_html
    assert complete_url in scan_html
    assert "原值" not in scan_html and "账面净值" not in scan_html
    assert "1234.56" not in scan_html
    assert "runtime_hour" not in scan_html

    form_page = client.get(complete_url, HTTP_USER_AGENT="Mozilla/5.0 Mobile")
    form_html = form_page.content.decode()
    assert form_page.status_code == 200
    assert "maintenance-mobile-action" in form_html
    assert "计划日期" in form_html and "实际完成日期" in form_html
    assert 'name="scheduled_date"' in form_html and "disabled" in form_html
    idempotency_key = form_page.context["form"]["idempotency_key"].value()

    completed = client.post(
        complete_url,
        {
            "idempotency_key": idempotency_key,
            # A disabled field is intentionally absent from a browser POST.
            "completed_date": timezone.localdate().isoformat(),
            "actual_content": "扫码后完成检查、清洁、润滑和紧固",
            "result": "normal",
            "problem_description": "",
            "remark": "责任人手机完成",
        },
    )
    record = MaintenanceRecord.objects.get(
        maintenance_plan=ctx["plan"], status="confirmed"
    )
    assert completed.status_code == 302
    assert completed.url == reverse("maintenance:record-detail", args=[record.pk])
    assert record.scheduled_date == ctx["plan"].first_due_date
    assert record.completed_by_id == ctx["responsible"].pk

    hr = make_user("s9-http-qr-hr", "hr")
    client.force_login(hr)
    hr_scan = client.get(scan_url)
    hr_html = hr_scan.content.decode()
    assert hr_scan.status_code == 200
    assert "保养摘要" not in hr_html
    assert ctx["plan"].name not in hr_html and complete_url not in hr_html
    assert "原值" not in hr_html and "1234.56" not in hr_html


def test_mobile_completion_post_uploads_photo_to_new_record_in_same_page(
    client, tmp_path
):
    ctx = maintenance_context("S9HTTPCOMPLETEPHOTO")
    complete_url = reverse("maintenance:plan-complete", args=[ctx["plan"].pk])
    client.force_login(ctx["responsible_user"])
    form_page = client.get(complete_url, HTTP_USER_AGENT="Mozilla/5.0 Mobile")
    idempotency_key = form_page.context["form"]["idempotency_key"].value()
    assert 'name="uploaded_file"' in form_page.content.decode()

    with override_settings(MEDIA_ROOT=tmp_path):
        response = client.post(
            complete_url,
            {
                "idempotency_key": idempotency_key,
                "completed_date": timezone.localdate().isoformat(),
                "actual_content": "手机同页完成并提交现场照片",
                "result": "normal",
                "problem_description": "",
                "remark": "同页附件事务验收",
                "security_class": "A0",
                "uploaded_file": SimpleUploadedFile(
                    "completion-mobile.jpg",
                    JPEG_BYTES,
                    content_type="image/jpeg",
                ),
            },
            HTTP_USER_AGENT="Mozilla/5.0 Mobile",
        )

    record = MaintenanceRecord.objects.get(
        maintenance_plan=ctx["plan"], status="confirmed"
    )
    link = AttachmentLink.objects.get(
        maintenance_record=record,
        attachment__original_filename="completion-mobile.jpg",
    )
    assert response.status_code == 302
    assert response.url == reverse("maintenance:record-detail", args=[record.pk])
    assert link.maintenance_record_id == record.pk
    assert link.maintenance_problem_id is None and link.asset_id is None
    assert link.role == "maintenance" and link.security_class == "A0"


def test_multipart_attachment_upload_targets_record_and_uses_private_storage(
    client, tmp_path
):
    ctx = maintenance_context("S9HTTPUPLOAD")
    record = _complete(ctx, "S9HTTPUPLOAD-complete")
    upload_url = reverse(
        "maintenance:attachment-upload", args=["record", record.pk]
    )
    client.force_login(ctx["responsible_user"])

    page = client.get(upload_url)
    assert page.status_code == 200
    assert 'enctype="multipart/form-data"' in page.content.decode()

    with override_settings(MEDIA_ROOT=tmp_path):
        response = client.post(
            upload_url,
            {
                "uploaded_file": SimpleUploadedFile(
                    "maintenance-proof.jpg", JPEG_BYTES, content_type="image/jpeg"
                )
            },
        )
        link = AttachmentLink.objects.get(
            maintenance_record=record,
            attachment__original_filename="maintenance-proof.jpg",
        )
        assert (tmp_path / link.attachment.storage_key).is_file()

    assert response.status_code == 302
    assert response.url == reverse("maintenance:record-detail", args=[record.pk])
    assert link.maintenance_problem_id is None and link.asset_id is None
    assert link.role == "maintenance" and link.security_class == "A0"
    assert link.attachment.storage_key.startswith(
        f"private/assets/{ctx['company'].pk}/maintenance/"
    )
    assert link.attachment.is_available


def test_finance_upload_page_selects_a1_nonfinance_cannot_see_or_forge_it(
    client, tmp_path
):
    ctx = maintenance_context("S9HTTPA1")
    record = _complete(ctx, "S9HTTPA1-complete")
    upload_url = reverse(
        "maintenance:attachment-upload", args=["record", record.pk]
    )

    client.force_login(ctx["finance"])
    finance_page = client.get(upload_url)
    finance_html = finance_page.content.decode()
    assert finance_page.status_code == 200
    assert 'name="security_class"' in finance_html
    assert 'value="A1"' in finance_html
    with override_settings(MEDIA_ROOT=tmp_path):
        created = client.post(
            upload_url,
            {
                "security_class": "A1",
                "uploaded_file": SimpleUploadedFile(
                    "finance-maintenance.pdf",
                    PDF_BYTES,
                    content_type="application/pdf",
                ),
            },
        )
    a1 = AttachmentLink.objects.get(
        maintenance_record=record,
        attachment__original_filename="finance-maintenance.pdf",
    )
    assert created.status_code == 302
    assert a1.security_class == "A1" and a1.role == "maintenance"

    client.force_login(ctx["responsible_user"])
    assignee_page = client.get(upload_url)
    assignee_html = assignee_page.content.decode()
    assert assignee_page.status_code == 200
    assert 'value="A1"' not in assignee_html
    assert "财务敏感" not in assignee_html
    with pytest.raises(PermissionDenied):
        upload_maintenance_attachment(
            actor=ctx["responsible_user"],
            target=record,
            security_class="A1",
            uploaded_file=SimpleUploadedFile(
                "forged-a1.pdf", PDF_BYTES, content_type="application/pdf"
            ),
        )


def test_plan_assignee_record_detail_never_offers_problem_attachment_upload(client):
    ctx = maintenance_context("S9HTTPPROBLEMUPLOAD")
    record = _complete(ctx, "S9HTTPPROBLEMUPLOAD-complete", result="problem_found")
    record_upload = reverse(
        "maintenance:attachment-upload", args=["record", record.pk]
    )
    problem_upload = reverse(
        "maintenance:attachment-upload", args=["problem", record.problem.pk]
    )
    client.force_login(ctx["responsible_user"])

    page = client.get(reverse("maintenance:record-detail", args=[record.pk]))
    html = page.content.decode()
    assert page.status_code == 200
    assert record_upload in html
    assert problem_upload not in html
    assert "上传处理证据" not in html
    assert client.get(problem_upload).status_code == 403


def test_voided_problem_is_history_only_and_direct_urls_enforce_scope(client):
    ctx = maintenance_context("S9HTTPVOID")
    record = _complete(ctx, "S9HTTPVOID-complete", result="problem_found")
    problem = record.problem
    void_maintenance_record(
        actor=ctx["equipment"],
        record=record,
        reason="完成日期录入错误",
        idempotency_key="S9HTTPVOID-void",
    )
    record.refresh_from_db()
    problem.refresh_from_db()
    detail_url = reverse("maintenance:record-detail", args=[record.pk])
    close_url = reverse("maintenance:problem-close", args=[problem.pk])

    client.force_login(ctx["equipment"])
    detail = client.get(detail_url)
    detail_html = detail.content.decode()
    assert detail.status_code == 200
    assert "来源保养记录已作废，仅保留历史证据" in detail_html
    assert "状态：来源记录已作废（仅历史）" in detail_html
    assert problem.description in detail_html
    assert close_url not in detail_html

    close_page = client.get(close_url)
    close_key = close_page.context["form"]["idempotency_key"].value()
    rejected = client.post(
        close_url,
        {
            "idempotency_key": close_key,
            "closure_note": "不应写入的处理说明",
            "confirm": "on",
        },
    )
    problem.refresh_from_db()
    assert rejected.status_code == 200
    assert "来源保养记录已作废" in rejected.content.decode()
    assert problem.status == MaintenanceProblem.Status.OPEN
    assert problem.closed_at is None and problem.closure_note == ""
    current_problems = client.get(reverse("maintenance:problem-list"))
    assert problem.description not in current_problems.content.decode()

    outsider = make_user("s9-http-void-outsider", "employee")
    client.force_login(outsider)
    for url in (
        reverse("maintenance:plan-detail", args=[ctx["plan"].pk]),
        reverse("maintenance:plan-complete", args=[ctx["plan"].pk]),
        detail_url,
        close_url,
        reverse("maintenance:attachment-upload", args=["record", record.pk]),
    ):
        assert client.get(url).status_code in {403, 404}

    denied_scan = client.get(
        reverse("assets:qr-scan", args=[ctx["qr_identity"].public_token])
    )
    assert denied_scan.status_code == 403
    assert ctx["plan"].name not in denied_scan.content.decode()
