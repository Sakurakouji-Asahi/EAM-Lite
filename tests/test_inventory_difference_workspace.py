"""Difference forms retain evidence, native inputs and a scoped continuation."""
from urllib.parse import urlencode

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import set_asset_idle, transfer_asset
from apps.assets.models import Asset
from apps.inventory.difference_workspace import difference_return_url
from apps.inventory.models import InventoryResolution, InventoryTaskAsset
from apps.inventory.services import close_inventory_task, resolve_inventory_difference, stop_inventory_scanning
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _published, _scan
from tests.test_sprint8_support import add_active_asset, inventory_context

pytestmark = pytest.mark.django_db


def stop(ctx, task, key):
    return stop_inventory_scanning(actor=ctx["finance"], task=task,
                                   reason="现场完成，核实差异", idempotency_key=key)


def native_data(page):
    return {field.html_name: "" if field.value() is None else str(field.value())
            for field in page.context["form"] if not field.field.disabled}


def test_difference_evidence_bound_error_and_native_save_keep_snapshot_and_master(client):
    ctx, asset, qr = inventory_context("DIFFEVIDENCE")
    _department, observed_employee, observed_location = add_target_assignment(ctx, "DIFF-OBSERVED")
    target_department, target_employee, target_location = add_target_assignment(ctx, "DIFF-CURRENT")
    task = _published(ctx, "DIFFEVIDENCE-T")
    scan = _scan(ctx, task, qr, "diff-evidence-scan", actual_location=observed_location,
                 actual_employee=observed_employee, note="临时移到现场检查区域")
    task = stop(ctx, task, "diff-evidence-stop")
    transfer_asset(actor=ctx["equipment"], asset=asset, to_department=target_department,
        to_responsible_employee=target_employee, to_location=target_location, effective_at=timezone.now(),
        reason="发布后的合法调拨", idempotency_key="diff-evidence-transfer", expected_status=asset.asset_status)
    asset.refresh_from_db()
    set_asset_idle(actor=ctx["equipment"], asset=asset, effective_at=timezone.now(),
                           reason="暂时闲置", idempotency_key="diff-evidence-idle")
    asset.refresh_from_db()
    row = task.task_assets.get(asset=asset)
    snapshot = InventoryTaskAsset._base_manager.filter(pk=row.pk).values().get()
    current = Asset._base_manager.filter(pk=asset.pk).values().get()
    client.force_login(ctx["equipment"])
    detail = reverse("inventory:task-detail", args=[task.pk])
    source = detail + "?" + urlencode({"q": row.expected_code_snapshot, "row_view": "unresolved", "page_size": 50, "page": 2}) + "#inventory-results"
    url = reverse("inventory:task-resolve", args=[task.pk, row.pk])
    page = client.get(url, {"return_to": source})
    assert page.status_code == 200 and page.context["difference_scan"].pk == scan.pk
    comparison = {item["label"]: item for item in page.context["difference_comparison"]}
    assert comparison["责任人"]["expected"] == row.expected_employee_snapshot
    assert comparison["责任人"]["actual"] == observed_employee.name
    assert comparison["责任人"]["current"] == target_employee.name
    assert comparison["责任人"]["actual_differs"] and comparison["责任人"]["current_differs"]
    assert comparison["状态"]["actual"] == "在用" and comparison["状态"]["current"] == "闲置"
    assert scan.note in page.content.decode() and "original_cost" not in page.content.decode()
    assert page.context["form"]["to_status"].value() is None
    data = native_data(page)
    data.update(return_to=source, resolution_type="master_updated", conclusion="先保留这段尚未保存的输入",
                to_location=str(observed_location.pk), effective_at="")
    rejected = client.post(url, data)
    assert rejected.status_code == 200 and "effective_at" in rejected.context["form"].errors
    assert rejected.context["form"]["conclusion"].value() == data["conclusion"]
    assert rejected.context["form"]["idempotency_key"].value() == data["idempotency_key"]
    assert rejected.context["difference_return_to"] == source
    assert InventoryTaskAsset._base_manager.filter(pk=row.pk).values().get() == snapshot
    assert Asset._base_manager.filter(pk=asset.pk).values().get() == current
    data.update(resolution_type="master_confirmed", conclusion="核实为临时现场变化，保留当前已调拨的主档", to_location="")
    saved = client.post(url, data)
    assert saved.status_code == 302 and saved.url == source
    assert client.post(url, data).url == source  # unchanged idempotent retry
    assert row.resolutions.filter(status="active").count() == 1
    assert Asset._base_manager.filter(pk=asset.pk).values().get() == current
    after = InventoryTaskAsset._base_manager.filter(pk=row.pk).values().get()
    assert after.pop("inventory_status") == "resolved"
    snapshot.pop("inventory_status")
    assert after == snapshot


def test_continue_stays_in_the_source_search_and_returns_after_last_match(client):
    ctx, first, _qr = inventory_context("DIFFCONTINUE")
    first.asset_name = "筛选内 第一件"
    first.save(update_fields=["asset_name"])
    second, _ = add_active_asset(ctx, "筛选内 第二件")
    outside, _ = add_active_asset(ctx, "查询外资产")
    task = stop(ctx, _published(ctx, "DIFFCONTINUE-T"), "diff-continue-stop")
    first_row, second_row, outside_row = [task.task_assets.get(asset=asset) for asset in (first, second, outside)]
    client.force_login(ctx["equipment"])
    detail = reverse("inventory:task-detail", args=[task.pk])
    source = detail + "?" + urlencode({"q": "筛选内", "row_view": "unresolved", "page_size": 50, "page": 8}) + "#inventory-results"
    first_url = reverse("inventory:task-resolve", args=[task.pk, first_row.pk])
    page = client.get(first_url, {"return_to": source})
    assert page.context["difference_other_pending"] == 1
    data = native_data(page)
    data.update(return_to=source, action="continue", resolution_type="loss_confirmed", conclusion="核实后确认盘亏，另行处理")
    next_response = client.post(first_url, data)
    second_url = reverse("inventory:task-resolve", args=[task.pk, second_row.pk])
    assert next_response.url == second_url + "?" + urlencode({"return_to": source})
    next_page = client.get(next_response.url)
    assert next_page.context["row"].pk == second_row.pk and next_page.context["difference_other_pending"] == 0
    data = native_data(next_page)
    data.update(return_to=source, action="continue", resolution_type="other", conclusion="核实完成，后续单独安排")
    final = client.post(second_url, data)
    assert final.status_code == 302 and final.url == source
    assert task.task_assets.filter(resolutions__status="active").count() == 2
    outside_row.refresh_from_db()
    task.refresh_from_db()
    assert outside_row.inventory_status == "missing" and not outside_row.resolutions.exists()
    assert task.status == "reconciliation"


def test_closed_correction_keeps_original_conclusion_and_returns_query(client):
    ctx, asset, _qr = inventory_context("DIFFCORRECT")
    task = stop(ctx, _published(ctx, "DIFFCORRECT-T"), "diff-correct-stop")
    row = task.task_assets.get(asset=asset)
    original = resolve_inventory_difference(actor=ctx["finance"], task_asset=row,
        resolution_type="loss_confirmed", conclusion="最初核实为盘亏", idempotency_key="diff-correct-original")
    task = close_inventory_task(actor=ctx["finance"], task=task, idempotency_key="diff-correct-close")
    client.force_login(ctx["finance"])
    source = reverse("inventory:task-detail", args=[task.pk]) + "?row_view=resolved#inventory-results"
    url = reverse("inventory:resolution-correct", args=[task.pk, original.pk])
    page = client.get(url, {"return_to": source})
    assert original.conclusion in page.content.decode() and "保存并继续下一条" not in page.content.decode()
    data = native_data(page)
    data.update(return_to=source, resolution_type="other", conclusion="追加复核的新结论", correction_reason="")
    rejected = client.post(url, data)
    assert "correction_reason" in rejected.context["form"].errors
    assert rejected.context["form"]["conclusion"].value() == data["conclusion"]
    assert InventoryResolution.objects.filter(inventory_task_asset=row).count() == 1
    data["correction_reason"] = "发现新的核实依据"
    saved = client.post(url, data)
    assert saved.status_code == 302 and saved.url == source
    original.refresh_from_db()
    task.refresh_from_db()
    corrected = row.resolutions.get(status="active")
    assert original.status == "superseded" and original.conclusion == "最初核实为盘亏"
    assert corrected.supersedes_resolution_id == original.pk and task.status == "closed"


def test_difference_rejects_execution_only_actor_and_cross_task_return(client):
    ctx, asset, _qr = inventory_context("DIFFAUTH")
    assignee = make_user("diff-auth-assignee", "employee")
    task = stop(ctx, _published(ctx, "DIFFAUTH-T", assignees=[assignee]), "diff-auth-stop")
    row = task.task_assets.get(asset=asset)
    url = reverse("inventory:task-resolve", args=[task.pk, row.pk])
    client.force_login(assignee)
    assert client.get(url).status_code == 403
    assert client.post(url, {"resolution_type": "other", "conclusion": "越权处理", "idempotency_key": "diff-auth-invalid"}).status_code == 403
    assert not row.resolutions.exists()
    factory = RequestFactory()
    fallback = reverse("inventory:task-detail", args=[task.pk])
    for value in ("https://example.test/", "//example.test/", reverse("inventory:task-list"),
                  fallback.replace(str(task.pk), "00000000-0000-0000-0000-000000000000"),
                  fallback + "#unrelated", fallback + "?q=test\n"):
        assert difference_return_url(factory.post(url, {"return_to": value}), task) == fallback
