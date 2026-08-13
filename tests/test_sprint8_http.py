from __future__ import annotations

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.assets.models import AttachmentLink
from apps.assets.qr_services import build_qr_payload, rotate_qr_identity
from apps.audit.models import AuditLog
from apps.inventory.models import InventoryScan, InventorySurplus
from apps.inventory.services import publish_inventory_task
from tests.test_sprint3_support import JPEG_BYTES, make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db


def test_mobile_scan_and_progress_pages_are_responsive_and_finance_isolated(client):
    context, asset, qr = inventory_context("S8HTTPMOBILE")
    assignee = make_user("s8-http-mobile-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8HTTPMOBILE-T", assignees=[assignee]),
    )
    client.force_login(assignee)

    progress = client.get(reverse("inventory:task-detail", args=[task.pk]))
    assert progress.status_code == 200
    html = progress.content.decode()
    assert 'name="viewport"' in html
    assert "inventory-summary-grid" in html
    assert "table-responsive" in html
    assert task.task_code in html and asset.asset_code in html
    assert "1234.56" not in html
    assert "原值" not in html and "账面净值" not in html

    entry = client.get(reverse("inventory:task-scan", args=[task.pk]))
    assert entry.status_code == 200
    scan_html = entry.content.decode()
    assert "inventory-mobile-action" in scan_html
    assert "应盘" in scan_html and "已盘" in scan_html
    assert "结果由后端" not in scan_html

    scan_redirect = client.post(
        reverse("inventory:task-scan", args=[task.pk]),
        {"token": build_qr_payload(qr)},
    )
    assert scan_redirect.status_code == 302
    assert qr.public_token not in scan_redirect["Location"]
    assert scan_redirect["Referrer-Policy"] == "no-referrer"
    assert scan_redirect["Cache-Control"] == "private, no-store"

    form_page = client.get(scan_redirect["Location"])
    assert form_page.status_code == 200
    form_html = form_page.content.decode()
    assert qr.public_token not in form_html
    assert form_page["Referrer-Policy"] == "no-referrer"
    assert form_page["Cache-Control"] == "private, no-store"
    assert "inventory-mobile-action" in form_html
    assert "结果由后端逐维比较" in form_html
    assert 'name="result"' not in form_html
    assert "1234.56" not in form_html


def test_http_scope_token_and_control_actions_deny_unauthorized_users(client):
    context, _asset, qr = inventory_context("S8HTTPDENY")
    assignee = make_user("s8-http-deny-assignee", "employee")
    outsider = make_user("s8-http-deny-outsider", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8HTTPDENY-T", assignees=[assignee]),
    )

    client.force_login(outsider)
    assert client.get(reverse("inventory:task-detail", args=[task.pk])).status_code == 404
    assert client.get(reverse("inventory:task-scan", args=[task.pk])).status_code == 404

    client.force_login(assignee)
    invalid_token = "invalid-token-must-never-be-echoed"
    invalid = client.post(
        reverse("inventory:task-scan", args=[task.pk]),
        {"token": invalid_token},
    )
    assert invalid.status_code == 403
    assert invalid_token not in invalid.content.decode()
    assert invalid["Referrer-Policy"] == "no-referrer"
    assert invalid["Cache-Control"] == "private, no-store"
    for name in ("task-stop", "task-close", "task-cancel"):
        response = client.get(reverse(f"inventory:{name}", args=[task.pk]))
        assert response.status_code == 403

    valid = client.post(
        reverse("inventory:task-scan", args=[task.pk]),
        {"token": qr.public_token},
    )
    assert valid.status_code == 302
    assert qr.public_token not in valid["Location"]
    assert client.get(valid["Location"]).status_code == 200

    not_attached = rotate_qr_identity(
        actor=context["finance"],
        asset=qr.asset,
        reason="HTTP 验证未贴标 Token 不得盘点",
    )
    rejected_unattached = client.post(
        reverse("inventory:task-scan", args=[task.pk]),
        {"token": not_attached.public_token},
    )
    assert rejected_unattached.status_code == 403
    assert not_attached.public_token not in rejected_unattached.content.decode()


def test_inventory_pages_require_login(client):
    context, _asset, _qr = inventory_context("S8HTTPLOGIN")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8HTTPLOGIN-T")
    )
    for url in (
        reverse("inventory:task-list"),
        reverse("inventory:task-detail", args=[task.pk]),
        reverse("inventory:task-scan", args=[task.pk]),
    ):
        response = client.get(url)
        assert response.status_code == 302
        assert reverse("login") in response.url


def test_hr_and_unscoped_department_manager_are_denied_inventory_root(client):
    context, _asset, _qr = inventory_context("S8HTTPROOTDENY")
    hr = make_user("s8-http-root-hr", "hr")
    client.force_login(hr)
    assert client.get(reverse("inventory:task-list")).status_code == 403
    assert client.get(reverse("inventory:task-create")).status_code == 403

    manager = make_user("s8-http-root-manager", "department_manager")
    client.force_login(manager)
    assert client.get(reverse("inventory:task-list")).status_code == 200
    assert client.get(reverse("inventory:task-create")).status_code == 403


def test_mobile_http_difference_surplus_attachment_close_and_same_key_retry(client):
    context, asset, qr = inventory_context("S8HTTPFLOW")
    assignee = make_user("s8-http-flow-assignee", "employee")
    _department, _employee, other_location = add_target_assignment(
        context, "S8HTTPFLOW-N"
    )
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8HTTPFLOW-T", assignees=[assignee]),
    )

    client.force_login(assignee)
    scan_entry_url = reverse("inventory:task-scan", args=[task.pk])
    first_context = client.post(scan_entry_url, {"token": qr.public_token})
    assert first_context.status_code == 302
    assert qr.public_token not in first_context["Location"]
    assert qr.public_token not in json.dumps(dict(client.session), default=str)
    scan_page = client.get(first_context["Location"])
    scan_key = scan_page.context["form"]["idempotency_key"].value()
    scan_data = {
        "idempotency_key": scan_key,
        "actual_location": str(other_location.pk),
        "actual_employee": str(asset.responsible_employee_id),
        "actual_status": asset.asset_status,
        "note": "现场位置与发布快照不同",
    }
    saved = client.post(first_context["Location"], scan_data)
    assert saved.status_code == 302
    assert qr.public_token not in saved["Location"]

    retry_context = client.post(scan_entry_url, {"token": qr.public_token})
    assert retry_context.status_code == 302
    replay = client.post(retry_context["Location"], scan_data)
    assert replay.status_code == 302
    assert InventoryScan.objects.filter(inventory_task=task).count() == 1
    scan = InventoryScan.objects.get(inventory_task=task)
    assert scan.result == "location_mismatch" and scan.is_effective

    surplus_url = reverse("inventory:surplus-create", args=[task.pk])
    surplus_page = client.get(surplus_url)
    surplus_key = surplus_page.context["form"]["idempotency_key"].value()
    surplus_data = {
        "idempotency_key": surplus_key,
        "temporary_name": "现场未建账工装",
        "temporary_category_text": "工装",
        "temporary_location_text": "一号车间角落",
        "remark": "等待财务确认",
    }
    created = client.post(surplus_url, surplus_data)
    assert created.status_code == 302
    replayed_create = client.post(surplus_url, surplus_data)
    assert replayed_create.status_code == 302
    assert InventorySurplus.objects.filter(inventory_task=task).count() == 1
    surplus = InventorySurplus.objects.get(inventory_task=task)

    upload_url = reverse(
        "inventory:attachment-upload",
        args=[task.pk, "surplus", surplus.pk],
    )
    uploaded = client.post(
        upload_url,
        {
            "uploaded_file": SimpleUploadedFile(
                "inventory.jpg", JPEG_BYTES, content_type="image/jpeg"
            )
        },
    )
    assert uploaded.status_code == 302
    link = AttachmentLink.objects.get(inventory_surplus=surplus)
    assert link.attachment.storage_key.startswith("private/inventory/")
    assert link.attachment.is_available

    outsider = make_user("s8-http-flow-outsider", "employee")
    client.force_login(outsider)
    assert client.get(upload_url).status_code == 404
    assert client.get(
        reverse("inventory:surplus-detail", args=[task.pk, surplus.pk])
    ).status_code == 404

    client.force_login(context["finance"])
    stop_url = reverse("inventory:task-stop", args=[task.pk])
    stop_page = client.get(stop_url)
    stop_key = stop_page.context["form"]["idempotency_key"].value()
    stop_data = {
        "idempotency_key": stop_key,
        "reason": "进入差异处理",
        "confirm": "on",
    }
    assert client.post(stop_url, stop_data).status_code == 302
    assert client.post(stop_url, stop_data).status_code == 302

    task.refresh_from_db()
    row = task.task_assets.get(asset=asset)
    detail = client.get(reverse("inventory:task-detail", args=[task.pk]))
    detail_html = detail.content.decode()
    assert detail.status_code == 200
    assert "位置异常" in detail_html
    assert "现场未建账工装" in detail_html
    assert "未解决：2" in detail_html

    resolution_url = reverse(
        "inventory:task-resolve", args=[task.pk, row.pk]
    )
    resolution_page = client.get(resolution_url)
    resolution_key = resolution_page.context["form"]["idempotency_key"].value()
    resolution_data = {
        "idempotency_key": resolution_key,
        "resolution_type": "master_confirmed",
        "conclusion": "现场临时摆放，主档无误",
    }
    assert client.post(resolution_url, resolution_data).status_code == 302
    assert client.post(resolution_url, resolution_data).status_code == 302

    surplus_resolution_url = reverse(
        "inventory:surplus-resolve", args=[task.pk, surplus.pk]
    )
    surplus_resolution_page = client.get(surplus_resolution_url)
    surplus_resolution_key = surplus_resolution_page.context["form"][
        "idempotency_key"
    ].value()
    assert surplus_resolution_key
    surplus_resolution_data = {
        "idempotency_key": surplus_resolution_key,
        "resolution_status": "not_company",
        "remark": "核查后确认不是公司资产",
    }
    assert client.post(
        surplus_resolution_url, surplus_resolution_data
    ).status_code == 302
    assert client.post(
        surplus_resolution_url, surplus_resolution_data
    ).status_code == 302

    close_url = reverse("inventory:task-close", args=[task.pk])
    close_page = client.get(close_url)
    close_key = close_page.context["form"]["idempotency_key"].value()
    close_data = {"idempotency_key": close_key, "confirm": "on"}
    assert client.post(close_url, close_data).status_code == 302
    assert client.post(close_url, close_data).status_code == 302
    task.refresh_from_db()
    assert task.status == "closed"

    final_page = client.get(reverse("inventory:task-detail", args=[task.pk]))
    final_html = final_page.content.decode()
    assert final_page.status_code == 200
    assert "已关闭" in final_html and "未解决：0" in final_html
    assert qr.public_token not in final_html
    audit_evidence = json.dumps(
        list(
            AuditLog.objects.filter(company=context["company"]).values(
                "old_data_json", "new_data_json", "user_agent"
            )
        ),
        ensure_ascii=False,
        default=str,
    )
    assert qr.public_token not in audit_evidence
