"""Inventory downloads follow visible task scope and effective evidence."""

import csv
import io

import pytest
from django.urls import reverse

from apps.assets.permissions import can_view_asset
from apps.inventory.result_export import HEADERS, _safe_cell
from apps.inventory.services import (
    close_inventory_task,
    correct_inventory_resolution,
    publish_inventory_task,
    resolve_inventory_difference,
    stop_inventory_scanning,
)
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _draft, _published, _scan
from tests.test_sprint8_support import add_active_asset, inventory_context


pytestmark = pytest.mark.django_db


def _download_rows(response):
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Cache-Control"] == "private, no-store"
    content = b"".join(response.streaming_content)
    assert content.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def test_export_downloads_all_matching_rows_and_current_evidence(client):
    context, asset, qr = inventory_context("INVEXPORT")
    others = [add_active_asset(context, f"INVEXPORT-{index:02d}") for index in range(25)]
    asset.equipment_number = "EXPORT-EQ-ONLY"
    asset.save(update_fields=["equipment_number"])
    _department, _employee, location = add_target_assignment(context, "INVEXPORT-OTHER")
    task = _published(context, "INVEXPORT-T")
    _scan(context, task, qr, "export-old-scan", actual_location=location, note="历史异常说明")
    _scan(context, task, qr, "export-current-scan", note="当前现场说明，包含逗号\n和换行")
    client.force_login(context["equipment"])

    detail_url = reverse("inventory:task-detail", args=[task.pk])
    export_url = reverse("inventory:task-result-export", args=[task.pk])
    page = client.get(detail_url, {"page": 2, "page_size": 25})
    assert len(page.context["row_items"]) == 1
    assert "导出全部 26 条匹配明细" in page.content.decode()
    assert export_url in page.content.decode()
    rows = _download_rows(client.get(export_url, {"page": 2, "page_size": 25}))
    assert len(rows) == 26
    assert list(rows[0]) == list(HEADERS)
    first = next(row for row in rows if row["设备编号（当前）"] == "EXPORT-EQ-ONLY")
    assert first["资产编号（快照）"] == asset.asset_code
    assert first["有效现场结果"] == "正常"
    assert first["现场位置"] == context["location"].name
    assert first["现场说明"] == "当前现场说明，包含逗号\n和换行"
    assert "历史异常说明" not in repr(rows)
    assert not {"原值", "净值", "折旧", "二维码", "Token"}.intersection(rows[0])
    filtered = _download_rows(client.get(export_url, {"q": "EXPORT-EQ-ONLY", "row_view": "normal"}))
    assert filtered == [first]
    missing = _download_rows(client.get(export_url, {"row_view": "missing"}))
    assert {row["资产编号（快照）"] for row in missing} == {item[0].asset_code for item in others}
    assert all(row["有效现场结果"] == "未盘" for row in missing)


def test_export_uses_current_corrected_resolution_and_escapes_csv_formula_text(client):
    context, _asset, _qr = inventory_context("INVEXPORTRES")
    task = _published(context, "INVEXPORTRES-T")
    task = stop_inventory_scanning(actor=context["finance"], task=task, reason="现场完成")
    old = resolve_inventory_difference(
        actor=context["finance"], task_asset=task.task_assets.get(), resolution_type="loss_confirmed",
        conclusion="旧结论", idempotency_key="export-resolution-old",
    )
    task = close_inventory_task(actor=context["finance"], task=task)
    current = correct_inventory_resolution(
        actor=context["finance"], resolution=old, resolution_type="other",
        conclusion="=危险公式文字\n应保留为文字", correction_reason="补充现场结论",
        idempotency_key="export-resolution-current",
    )
    client.force_login(context["equipment"])
    rows = _download_rows(client.get(reverse("inventory:task-result-export", args=[task.pk]), {"row_view": "resolved"}))
    assert len(rows) == 1
    assert rows[0]["当前处理结论"] == "'" + current.conclusion
    assert rows[0]["处理结论类型"] == "其他结论"
    assert "旧结论" not in repr(rows)


def test_export_rechecks_assignment_without_granting_general_asset_scope(client):
    context, asset, _qr = inventory_context("INVEXPORTSCOPE")
    assignee = make_user("inventory-export-assignee", "employee")
    task = publish_inventory_task(actor=context["finance"], task=_draft(context, "INVEXPORTSCOPE-T", assignees=[assignee]))
    assert not can_view_asset(assignee, asset)
    client.force_login(assignee)
    url = reverse("inventory:task-result-export", args=[task.pk])
    assert len(_download_rows(client.get(url))) == 1
    assignee.groups.clear()
    assert client.get(url).status_code == 404
    client.force_login(make_user("inventory-export-hr", "hr"))
    assert client.get(url).status_code == 404


def test_export_rejects_drafts_invalid_filters_and_mutating_requests(client):
    context, _asset, _qr = inventory_context("INVEXPORTVALID")
    task = _draft(context, "INVEXPORTVALID-T")
    client.force_login(context["equipment"])
    url = reverse("inventory:task-result-export", args=[task.pk])
    assert client.get(url).status_code == 400
    assert "导出筛选结果 CSV" not in client.get(reverse("inventory:task-detail", args=[task.pk])).content.decode()
    task = publish_inventory_task(actor=context["finance"], task=task)
    for query in ({"row_view": "invalid"}, {"page_size": 999999}, {"q": "x" * 201}):
        response = client.get(url, query)
        assert response.status_code == 400
        assert not response.streaming
        assert "Content-Disposition" not in response
    assert client.post(url).status_code == 405
    client.logout()
    assert client.get(url).status_code == 302


@pytest.mark.parametrize("text", ("=1+1", "+1", "-1", "@SUM(A1)", " \t=1+1", "\ufeff=1", "\tordinary", "\nordinary"))
def test_csv_user_text_cannot_be_interpreted_as_formula(text):
    assert _safe_cell(text) == "'" + text


def test_csv_plain_text_and_empty_values_are_preserved():
    assert _safe_cell("正常的中文，含逗号") == "正常的中文，含逗号"
    assert _safe_cell(None) == ""
