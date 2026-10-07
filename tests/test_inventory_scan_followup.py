"""Field scanning stays continuous without hiding repeat-scan consequences."""

import json

import pytest
from django.urls import reverse

from apps.inventory.models import InventoryScan
from apps.inventory.services import publish_inventory_task
from apps.inventory.views import RECENT_SCAN_SESSION_PREFIX
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _draft, _scan
from tests.test_sprint8_support import add_active_asset, inventory_context


pytestmark = pytest.mark.django_db


def _start(client, task, qr):
    response = client.post(reverse("inventory:task-scan", args=[task.pk]), {"token": qr.public_token})
    assert response.status_code == 302
    assert qr.public_token not in response.url
    return response.url, client.get(response.url)


def _data(form):
    return {
        name: form[name].value()
        for name in ("idempotency_key", "actual_location", "actual_employee", "actual_status")
    }


def test_scan_save_shows_recent_asset_and_remaining_then_guides_completed_task(client):
    context, first_asset, first_qr = inventory_context("SCANFOLLOW")
    second_asset, second_qr = add_active_asset(context, "SCANFOLLOW-SECOND")
    assignee = make_user("scan-follow-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "SCANFOLLOW-T", assignees=[assignee]),
    )
    client.force_login(assignee)
    entry_url = reverse("inventory:task-scan", args=[task.pk])
    form_url, form_page = _start(client, task, first_qr)
    text = form_page.content.decode()
    assert task.name in text and task.task_code in text
    assert "查看剩余未盘 2 件" in text
    assert form_page.context["previous_scan"] is None
    assert "跳过此资产，继续扫码" in text
    assert client.post(form_url, _data(form_page.context["form"])).url == entry_url

    entry = client.get(entry_url)
    recent = entry.context["recent_scan"]
    assert recent.asset_id == first_asset.pk and recent.scanned_by_id == assignee.pk
    text = entry.content.decode()
    assert "最近保存" in text and "还剩 1 件未盘" in text
    assert first_asset.asset_code in text and first_asset.asset_name in text
    assert "row_view=missing" in entry.context["summary_links"]["missing"]
    assert first_qr.public_token not in text
    assert first_qr.public_token not in json.dumps(dict(client.session), default=str)
    assert entry["Cache-Control"] == "private, no-store"

    form_url, form_page = _start(client, task, second_qr)
    assert client.post(form_url, _data(form_page.context["form"])).url == entry_url
    complete = client.get(entry_url)
    assert complete.context["recent_scan"].asset_id == second_asset.pk
    assert complete.context["summary"]["missing"] == 0
    assert "现场记录已齐" in complete.content.decode()
    stop_url = reverse("inventory:task-stop", args=[task.pk])
    assert stop_url not in complete.content.decode()
    assert "请联系任务负责人" in complete.content.decode()
    task.refresh_from_db()
    assert task.status == "in_progress"
    client.force_login(context["equipment"])
    manager_entry = client.get(entry_url)
    assert manager_entry.context["recent_scan"] is None
    assert stop_url in manager_entry.content.decode()


def test_rescan_explains_effective_replacement_and_validation_preserves_observations(client):
    context, asset, qr = inventory_context("SCANRECHECK")
    _department, _employee, changed_location = add_target_assignment(context, "SCANRECHECK-N")
    task = publish_inventory_task(actor=context["finance"], task=_draft(context, "SCANRECHECK-T"))
    previous = _scan(context, task, qr, "scan-recheck-first", actual_location=changed_location)
    client.force_login(context["equipment"])
    form_url, form_page = _start(client, task, qr)
    text = form_page.content.decode()
    assert form_page.context["previous_scan"].pk == previous.pk
    assert "此资产已盘过，本次为复核" in text
    assert "历史记录保留" in text and "已盘数量不会增加" in text
    assert changed_location.name in text and "保存本次复核结果" in text
    # Snapshot prefills remain unchanged; the earlier observation is read-only.
    assert str(form_page.context["form"]["actual_location"].value()) == str(asset.location_id)
    assert InventoryScan.objects.filter(inventory_task=task).count() == 1
    payload = _data(form_page.context["form"])
    payload.update(actual_location=str(changed_location.pk), other_mismatch="on", note="")
    invalid = client.post(form_url, payload)
    assert invalid.status_code == 200
    assert "note" in invalid.context["form"].errors
    assert str(invalid.context["form"]["actual_location"].value()) == str(changed_location.pk)
    assert invalid.context["previous_scan"].pk == previous.pk
    assert InventoryScan.objects.filter(inventory_task=task).count() == 1

    payload["note"] = "实物标签破损，位置已核对"
    conflicting = client.post(form_url, payload)
    assert conflicting.status_code == 200
    assert "其他异常不得覆盖" in conflicting.content.decode()
    assert conflicting.context["form"]["note"].value() == payload["note"]
    assert InventoryScan.objects.filter(inventory_task=task).count() == 1
    payload["other_mismatch"] = ""
    saved = client.post(form_url, payload)
    assert saved.status_code == 302, saved.context["form"].errors
    previous.refresh_from_db()
    assert not previous.is_effective
    current = InventoryScan.objects.get(inventory_task=task, is_effective=True)
    assert current.supersedes_scan_id == previous.pk and current.note == payload["note"]
    following = client.get(saved.url)
    assert following.context["recent_scan"].pk == current.pk
    assert following.context["summary"]["scanned"] == 1
    assert InventoryScan.objects.filter(inventory_task=task).count() == 2


def test_invalid_scan_and_stale_recent_pointer_do_not_echo_token_or_cross_tasks(client):
    context, _asset, qr = inventory_context("SCANSAFE")
    first = publish_inventory_task(actor=context["finance"], task=_draft(context, "SCANSAFE-FIRST"))
    second = publish_inventory_task(actor=context["finance"], task=_draft(context, "SCANSAFE-SECOND"))
    scan = _scan(context, first, qr, "scan-safe-first")
    client.force_login(context["equipment"])
    entry_url = reverse("inventory:task-scan", args=[second.pk])
    session = client.session
    key = f"{RECENT_SCAN_SESSION_PREFIX}{second.pk}"
    session[key] = str(scan.pk)
    session.save()
    page = client.get(entry_url)
    assert page.context["recent_scan"] is None
    assert key not in client.session
    session = client.session
    session[key] = "invalid-uuid"
    session.save()
    assert client.get(entry_url).status_code == 200
    invalid_token = "private-invalid-token-never-render"
    page = client.post(entry_url, {"token": invalid_token})
    assert page.status_code == 403 and invalid_token not in page.content.decode()
    assert page.context["summary"]["scanned"] == 0
    assert page["Cache-Control"] == "private, no-store"
    assert page["Referrer-Policy"] == "no-referrer"
    assert 'maxlength="512"' in page.content.decode()
    assert "不能用资产编号或设备编号代替" in page.content.decode()
    client.force_login(make_user("scan-follow-outsider", "employee"))
    assert client.get(entry_url).status_code == 404
