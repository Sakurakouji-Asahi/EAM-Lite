from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.test import Client, override_settings
from django.urls import reverse

from apps.assets.models import AssetLabelPrintBatch
from apps.assets.permissions import can_view_asset
from apps.assets.qr_services import rotate_qr_identity
from apps.inventory.forms import InventoryScanForm
from apps.inventory.services import publish_inventory_task
from tests.test_sprint3_support import make_user
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import add_active_asset, inventory_context


pytestmark = pytest.mark.django_db


def test_scan_form_prefills_the_immutable_task_snapshot():
    context, _asset, _qr = inventory_context("UXSCANDEFAULT")
    assignee = make_user("ux-scan-default-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "UXSCANDEFAULT-T", assignees=[assignee]),
    )
    row = task.task_assets.get()

    form = InventoryScanForm(
        actor=assignee,
        task=task,
        task_asset=row,
    )

    assert str(form["actual_location"].value()) == str(row.expected_location_id)
    assert str(form["actual_employee"].value()) == str(row.expected_employee_id)
    assert form["actual_status"].value() == row.expected_asset_status


def test_system_camera_qr_page_bridges_assigned_task_without_token_or_asset_scope():
    context, asset, qr_identity = inventory_context("UXQRBRIDGE")
    assignee = make_user("ux-qr-bridge-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "UXQRBRIDGE-T", assignees=[assignee]),
    )
    row = task.task_assets.get(asset=asset)
    assert not can_view_asset(assignee, asset)

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(assignee)
    landing = csrf_client.get(
        reverse("assets:qr-scan", args=[qr_identity.public_token])
    )

    assert landing.status_code == 200
    assert landing.context["inventory_only_access"] is True
    assert len(landing.context["inventory_scan_entries"]) == 1
    html = landing.content.decode()
    assert "继续资产盘点" in html
    assert task.task_code in html
    assert qr_identity.public_token not in html
    bridge = landing.context["inventory_scan_entries"][0]["bridge"]
    assert qr_identity.public_token not in bridge

    scan_url = reverse("inventory:task-scan", args=[task.pk])
    missing_csrf = csrf_client.post(scan_url, {"scan_bridge": bridge})
    assert missing_csrf.status_code == 403

    csrf_token = csrf_client.cookies["csrftoken"].value
    started = csrf_client.post(
        scan_url,
        {"scan_bridge": bridge, "csrfmiddlewaretoken": csrf_token},
    )
    assert started.status_code == 302
    assert qr_identity.public_token not in started["Location"]
    assert qr_identity.public_token not in json.dumps(
        dict(csrf_client.session), default=str
    )

    form_page = csrf_client.get(started["Location"])
    assert form_page.status_code == 200
    form = form_page.context["form"]
    assert str(form["actual_location"].value()) == str(row.expected_location_id)
    assert str(form["actual_employee"].value()) == str(row.expected_employee_id)
    assert form["actual_status"].value() == row.expected_asset_status
    assert "inventory-scan-actions" in form_page.content.decode()

    detail = csrf_client.get(reverse("inventory:task-detail", args=[task.pk]))
    assert "inventory-action-column" in detail.content.decode()


def test_qr_inventory_entry_is_hidden_without_scan_permission_and_post_rechecks_scope(
    client,
):
    context, _asset, qr_identity = inventory_context("UXQRHIDDEN")
    assignee = make_user("ux-qr-hidden-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "UXQRHIDDEN-T", assignees=[assignee]),
    )

    client.force_login(assignee)
    allowed = client.get(reverse("assets:qr-scan", args=[qr_identity.public_token]))
    bridge = allowed.context["inventory_scan_entries"][0]["bridge"]

    hr_user = make_user("ux-qr-hidden-hr", "hr")
    client.force_login(hr_user)
    landing = client.get(reverse("assets:qr-scan", args=[qr_identity.public_token]))
    assert landing.status_code == 200
    assert "继续资产盘点" not in landing.content.decode()
    assert not landing.context["inventory_scan_entries"]

    forged_post = client.post(
        reverse("inventory:task-scan", args=[task.pk]),
        {"scan_bridge": bridge},
    )
    assert forged_post.status_code == 404


@override_settings(
    QR_BASE_URL="http://testserver",
    ALLOWED_HOSTS=["testserver", "alternate.test"],
)
def test_opaque_origin_inventory_bridge_keeps_csrf_and_path_scope():
    context, _asset, qr_identity = inventory_context("UXQROPAQUE")
    assignee = make_user("ux-qr-opaque-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "UXQROPAQUE-T", assignees=[assignee]),
    )
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(assignee)
    landing = csrf_client.get(
        reverse("assets:qr-scan", args=[qr_identity.public_token])
    )
    bridge = landing.context["inventory_scan_entries"][0]["bridge"]
    csrf_token = csrf_client.cookies["csrftoken"].value
    scan_url = reverse("inventory:task-scan", args=[task.pk])

    accepted = csrf_client.post(
        scan_url,
        {"scan_bridge": bridge, "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    )
    assert accepted.status_code == 302

    anonymous = Client(enforce_csrf_checks=True)
    anonymous.cookies["csrftoken"] = csrf_token
    assert anonymous.post(
        scan_url,
        {"scan_bridge": bridge, "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    ).status_code == 403

    without_csrf_cookie = Client(enforce_csrf_checks=True)
    without_csrf_cookie.force_login(assignee)
    assert without_csrf_cookie.post(
        scan_url,
        {"scan_bridge": bridge, "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    ).status_code == 403

    assert csrf_client.post(
        scan_url,
        {"scan_bridge": bridge},
        HTTP_ORIGIN="null",
    ).status_code == 403
    assert csrf_client.post(
        scan_url,
        {"csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    ).status_code == 403
    assert csrf_client.post(
        scan_url,
        {"scan_bridge": f"{bridge}tampered", "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    ).status_code == 403

    unauthorized = Client(enforce_csrf_checks=True)
    unauthorized.force_login(make_user("ux-qr-opaque-hr", "hr"))
    unauthorized.cookies["csrftoken"] = csrf_token
    assert unauthorized.post(
        scan_url,
        {"scan_bridge": bridge, "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    ).status_code == 404

    assert csrf_client.post(
        reverse("logout"),
        {"scan_bridge": bridge, "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    ).status_code == 403
    assert "_auth_user_id" in csrf_client.session

    # The compatibility exception is for the signed bridge only. A raw-token
    # scan, another host, or another endpoint still uses normal Origin checks.
    assert csrf_client.post(
        scan_url,
        {"token": qr_identity.public_token, "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
    ).status_code == 403
    assert csrf_client.post(
        scan_url,
        {"scan_bridge": bridge, "csrfmiddlewaretoken": csrf_token},
        HTTP_ORIGIN="null",
        HTTP_HOST="alternate.test",
    ).status_code == 403


def test_scan_entry_distinguishes_invalid_qr_from_valid_non_task_asset(client):
    context, _asset, _qr_identity = inventory_context("UXQRERROR")
    assignee = make_user("ux-qr-error-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "UXQRERROR-T", assignees=[assignee]),
    )
    _other_asset, other_qr = add_active_asset(context, "UXQRERROR-LATE")
    client.force_login(assignee)
    scan_url = reverse("inventory:task-scan", args=[task.pk])

    non_task = client.post(scan_url, {"token": other_qr.public_token})
    non_task_html = non_task.content.decode()
    assert non_task.status_code == 403
    assert "非本任务资产" in non_task_html
    assert "二维码无效" not in non_task_html
    assert "alert-warning" in non_task_html

    invalid = client.post(scan_url, {"token": "x" * 48})
    invalid_html = invalid.content.decode()
    assert invalid.status_code == 403
    assert "二维码无效" in invalid_html
    assert "非本任务资产" not in invalid_html
    assert "alert-danger" in invalid_html


def test_label_queue_search_pagination_and_checked_only_batch(client):
    context, first_asset, _first_qr = inventory_context("UXLABELQUEUE")
    second_asset, _second_qr = add_active_asset(context, "UXLABELQUEUE-SECOND")
    rotate_qr_identity(
        actor=context["finance"],
        asset=first_asset,
        reason="易用性搜索测试",
    )
    rotate_qr_identity(
        actor=context["finance"],
        asset=second_asset,
        reason="易用性批量测试",
    )
    client.force_login(context["equipment"])

    queue_url = reverse("assets:label-queue")
    filtered = client.get(
        queue_url,
        {"status": "ready_to_print", "q": first_asset.asset_code},
    )
    assert filtered.status_code == 200
    assert filtered.context["page_obj"].paginator.per_page == 25
    assert filtered.context["page_obj"].paginator.count == 1
    filtered_html = filtered.content.decode()
    assert first_asset.asset_code in filtered_html
    assert second_asset.asset_code not in filtered_html
    assert 'loading="lazy"' in filtered_html
    assert f"q={first_asset.asset_code}" in filtered.context["pagination_query"]

    all_ready = client.get(queue_url, {"status": "ready_to_print"})
    form_key = all_ready.context["form"]["idempotency_key"].value()
    created = client.post(
        queue_url,
        {
            "status": "ready_to_print",
            "q": "",
            "page": "1",
            "idempotency_key": form_key,
            "asset_ids": [str(first_asset.pk)],
        },
    )
    assert created.status_code == 302
    batch = AssetLabelPrintBatch.objects.order_by("-created_at").first()
    assert batch is not None
    assert list(batch.items.values_list("qr_identity__asset_id", flat=True)) == [
        first_asset.pk
    ]


def test_inventory_task_list_pages_25_and_preserves_filters(client):
    context, _asset, _qr_identity = inventory_context("UXTASKPAGE")
    for index in range(27):
        _draft(context, f"UXTASKPAGE-{index:02d}")
    client.force_login(context["finance"])

    url = reverse("inventory:task-list")
    first_page = client.get(url, {"q": "UXTASKPAGE", "status": "draft"})
    assert first_page.status_code == 200
    page_obj = first_page.context["page_obj"]
    assert page_obj.paginator.per_page == 25
    assert page_obj.paginator.count == 27
    assert len(page_obj.object_list) == 25
    assert "q=UXTASKPAGE" in first_page.context["pagination_query"]
    assert "status=draft" in first_page.context["pagination_query"]

    second_page = client.get(
        url,
        {"q": "UXTASKPAGE", "status": "draft", "page": "2"},
    )
    assert second_page.status_code == 200
    assert len(second_page.context["page_obj"].object_list) == 2
    assert second_page.context["query"] == "UXTASKPAGE"
    assert second_page.context["status"] == "draft"


def test_mobile_touch_targets_sticky_actions_and_reachable_inventory_column():
    css_path = finders.find("css/app.css")
    assert css_path is not None
    css = Path(css_path).read_text(encoding="utf-8")

    assert ".mobile-action-bar" in css
    assert ".inventory-scan-actions" in css
    assert "env(safe-area-inset-bottom)" in css
    assert ".inventory-snapshot-current .inventory-action-column" in css
    assert ".app-mobile-nav .app-nav-link" in css
    assert ".btn-sm" in css and "min-height: 44px" in css
