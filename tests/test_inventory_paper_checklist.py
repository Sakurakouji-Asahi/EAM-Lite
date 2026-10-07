"""Paper inventory checklists retain export scope, filters and effective evidence."""
from html import escape

import pytest
from django.urls import reverse

from apps.assets.permissions import can_view_asset
from apps.inventory.models import InventoryTask, InventoryTaskAsset, InventoryScan, InventoryResolution
from apps.inventory.services import (close_inventory_task, correct_inventory_resolution,
    publish_inventory_task, resolve_inventory_difference, stop_inventory_scanning)
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _draft, _published, _scan
from tests.test_sprint8_support import add_active_asset, inventory_context

pytestmark = pytest.mark.django_db


def _state(task):
    return (InventoryTask._base_manager.filter(pk=task.pk).values().get(),
        list(InventoryTaskAsset._base_manager.filter(inventory_task=task).values()),
        list(InventoryScan._base_manager.filter(inventory_task=task).values()),
        list(InventoryResolution._base_manager.filter(inventory_task_asset__inventory_task=task).values()))


def test_paper_contains_all_matching_rows_and_only_current_allowed_evidence(client):
    context, asset, qr = inventory_context("INVPAPER")
    others = [add_active_asset(context, f"INVPAPER-{index:02d}") for index in range(25)]
    _department, _employee, other_location = add_target_assignment(context, "INVPAPER-OTHER")
    task = _published(context, "INVPAPER-T")
    _scan(context, task, qr, "paper-old-scan", actual_location=other_location, note="不应打印的历史说明")
    note = "当前有效说明 <检验>\nPRINT-LATEST-MARKER"
    _scan(context, task, qr, "paper-effective-scan", note=note)
    before = _state(task)
    client.force_login(context["equipment"])
    detail_url = reverse("inventory:task-detail", args=[task.pk])
    url = reverse("inventory:task-paper-checklist", args=[task.pk])
    detail = client.get(detail_url, {"page": 2, "page_size": 25})
    assert len(detail.context["row_items"]) == 1 and "打印现场清单" in detail.content.decode()
    paper = client.get(url, {"page": 2, "page_size": 25})
    assert paper.status_code == 200 and paper.context["paper_count"] == 26
    assert len(paper.context["paper_items"]) == 26
    assert paper.context["back_url"] == detail_url + "?page=2&page_size=25#inventory-results"
    assert "no-store" in paper["Cache-Control"] and paper["Referrer-Policy"] == "no-referrer"
    body = paper.content.decode()
    assert escape(note) in body and "不应打印的历史说明" not in body
    assert qr.public_token not in body and "1234.56" not in body
    assert "现场手写核对（待录入）" in body and "不受屏幕分页限制" in body
    normal = client.get(url, {"q": asset.asset_code, "row_view": "normal"})
    assert normal.context["paper_count"] == 1 and normal.context["paper_items"][0]["row"].asset_id == asset.pk
    missing = client.get(url, {"row_view": "missing", "page": 2})
    assert {item["row"].asset_id for item in missing.context["paper_items"]} == {item[0].pk for item in others}
    empty = client.get(url, {"q": "没有这种设备"})
    assert empty.context["paper_count"] == 0 and "没有匹配的应盘记录" in empty.content.decode()
    assert _state(task) == before


def test_assignee_print_scope_rechecks_roles_and_keeps_only_corrected_conclusion(client):
    context, asset, qr = inventory_context("INVPAPERSCOPE")
    assignee = make_user("inventory-paper-assignee", "employee")
    task = publish_inventory_task(actor=context["finance"], task=_draft(context, "INVPAPERSCOPE-T", assignees=[assignee]))
    task = stop_inventory_scanning(actor=context["finance"], task=task, reason="核对完成")
    old = resolve_inventory_difference(actor=context["finance"], task_asset=task.task_assets.get(),
        resolution_type="loss_confirmed", conclusion="历史结论不打印", idempotency_key="paper-old-conclusion")
    task = close_inventory_task(actor=context["finance"], task=task)
    current = correct_inventory_resolution(actor=context["finance"], resolution=old,
        resolution_type="other", conclusion="PRINT-CURRENT-CONCLUSION", correction_reason="纸面核对复核", idempotency_key="paper-current-conclusion")
    assert not can_view_asset(assignee, asset)
    url = reverse("inventory:task-paper-checklist", args=[task.pk])
    client.force_login(assignee)
    paper = client.get(url, {"row_view": "resolved"})
    assert paper.status_code == 200 and paper.context["paper_count"] == 1
    assert paper.context["paper_items"][0]["resolution"].pk == current.pk
    assert "PRINT-CURRENT-CONCLUSION" in paper.content.decode() and "历史结论不打印" not in paper.content.decode()
    assert qr.public_token not in paper.content.decode()
    assignee.groups.clear()
    assert client.get(url).status_code == 404
    client.force_login(make_user("inventory-paper-hr", "hr"))
    assert client.get(url).status_code == 404
    client.logout()
    assert client.get(url).status_code == 302


def test_paper_rejects_draft_invalid_filters_and_post(client):
    context, _asset, _qr = inventory_context("INVPAPERVALID")
    task = _draft(context, "INVPAPERVALID-T")
    client.force_login(context["equipment"])
    url = reverse("inventory:task-paper-checklist", args=[task.pk])
    assert client.get(url).status_code == 400
    assert "打印现场清单" not in client.get(reverse("inventory:task-detail", args=[task.pk])).content.decode()
    task = publish_inventory_task(actor=context["finance"], task=task)
    before = _state(task)
    for query in ({"row_view": "invalid"}, {"page_size": 1}, {"q": "x" * 201}):
        response = client.get(url, query)
        assert response.status_code == 400 and "paper-checklist" not in response.content.decode()
    assert client.post(url).status_code == 405
    assert _state(task) == before
