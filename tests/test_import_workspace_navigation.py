from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.imports.presentation import created_results_context
from apps.imports.services import confirm_import_batch, upload_and_validate_import
from apps.imports.workspace import history_navigation
from apps.masterdata.models import Department
from tests.test_sprint1_imports import setup_data, workbook_file
from tests.test_bulk_finance_confirmation import imported_assets
from tests.test_sprint3_support import make_company, make_department, make_user
from tests.test_unified_asset_identity import context


pytestmark = pytest.mark.django_db(transaction=True)


def query_of(url):
    return {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "imports"


def department_batch(company, actor, key, rows, *, filename="workspace-departments.xlsx"):
    uploaded = workbook_file("department", rows)
    uploaded.name = filename
    return upload_and_validate_import(actor=actor, company=company, import_type="department",
        uploaded_file=uploaded, idempotency_key=key)


def test_history_state_counts_and_filter_removal_keep_filename_and_type(client):
    company, actor = setup_data("system_admin")
    confirmed = department_batch(company, actor, "confirmed", [["DONE", "已导入部门", "", "", "是"]])
    confirm_import_batch(actor=actor, batch=confirmed)
    invalid = department_batch(company, actor, "invalid", [["BAD", "需修正部门", "MISSING", "", "是"]])
    department_batch(company, actor, "validated", [["READY", "待确认部门", "", "", "是"]])
    client.force_login(actor)
    history = client.get(reverse("imports:home"), {"q": "workspace-", "import_type": "department", "status": "invalid", "page": "8"})
    assert history.status_code == 200 and [batch.pk for batch in history.context["recent_batches"]] == [invalid.pk]
    cards = history.context["history_status_cards"]
    assert [card["count"] for card in cards] == [3, 0, 1, 1, 1, 0, 0, 0]
    for card in cards:
        assert query_of(card["url"])["q"] == "workspace-"
        assert query_of(card["url"])["import_type"] == "department" and "page" not in query_of(card["url"])
    chip = history.context["import_filter_summary"][-1]
    assert "status" not in query_of(chip["remove_url"])
    assert query_of(chip["remove_url"])["q"] == "workspace-"
    detail = client.get(history.context["recent_batches"][0].workspace_errors_url)
    assert detail.status_code == 200 and detail.context["rows"][0].errors_json
    returned = query_of(detail.context["history_return_url"])
    assert returned == {"q": "workspace-", "import_type": "department", "status": "invalid", "page": "1"}


def test_error_navigation_locates_neighbour_rows_and_keeps_history(client):
    company, actor = setup_data("system_admin")
    batch = department_batch(company, actor, "error-navigation", [
        ["OK1", "第一部门", "", "", "是"], ["BAD1", "错误一", "MISSING", "", "是"],
        ["OK2", "第二部门", "", "", "是"], ["BAD2", "错误二", "MISSING", "", "是"],
        ["BAD3", "错误三", "MISSING", "", "是"],
    ])
    client.force_login(actor)
    history_query = "q=workspace-&import_type=department&status=invalid&page=2"
    url = reverse("imports:batch_detail", args=[batch.pk])
    page = client.get(url, {"row_view": "warnings", "row_number": "4", "history_query": history_query})
    assert page.status_code == 200 and not page.context["rows"]
    assert [card["count"] for card in page.context["row_status_cards"]] == [1, 0, 0, 1]
    targets = page.context["error_navigation"]
    assert [(target["label"], target["row_number"]) for target in targets] == [("首条错误", 3), ("上一错误", 3), ("下一错误", 5)]
    next_error = client.get(targets[-1]["url"])
    assert [row.row_number for row in next_error.context["rows"]] == [5]
    assert next_error.context["rows"][0].raw_data_json["部门名称"] == "错误二"
    assert query_of(targets[-1]["url"])["row_view"] == "errors"
    assert next_error.context["history_return_url"] == reverse("imports:home") + "?" + history_query
    assert [card["count"] for card in next_error.context["row_status_cards"]] == [1, 1, 0, 0]
    assert next_error.context["can_download_error_rows"]
    assert not Department.objects.filter(company=company).exists()
    assert client.get(url, {"unsupported": "value"}).status_code == 400


def test_confirm_and_cancel_return_to_same_preview_and_preserve_whole_batch(client):
    company, actor = setup_data("system_admin")
    batch = department_batch(company, actor, "confirm-navigation", [
        ["NAV1", "办理部门一", "", "", "是"], ["NAV2", "办理部门二", "", "", "是"]])
    client.force_login(actor)
    history_query = "q=workspace-&status=validated&page=3"
    detail = client.get(reverse("imports:batch_detail", args=[batch.pk]),
        {"row_number": "2", "row_view": "clean", "history_query": history_query})
    missing_ack = client.post(detail.context["import_confirm_url"], {"history_query": history_query})
    assert missing_ack.status_code == 302 and query_of(missing_ack.url)["row_number"] == "2"
    result = client.post(detail.context["import_confirm_url"], {"confirm": "1", "history_query": history_query})
    assert result.status_code == 302 and query_of(result.url)["history_query"] == history_query
    assert Department.objects.filter(company=company).count() == 2
    completed = client.get(result.url)
    row = completed.context["rows"][0]
    assert row.created_result["url"] == reverse("masterdata:department-detail", args=[int(row.created_object_id)])
    assert completed.context["created_results_directory"]["url"] == reverse("masterdata:department-list")
    assert completed.context["history_return_url"] == reverse("imports:home") + "?" + history_query
    pending = department_batch(company, actor, "cancel-navigation", [["NO-CREATE", "取消测试部门", "", "", "是"]])
    cancel_page = client.get(reverse("imports:batch_detail", args=[pending.pk]), {"history_query": history_query})
    cancelled = client.post(cancel_page.context["import_cancel_url"], {"reason": "取消本次测试", "history_query": history_query})
    assert cancelled.status_code == 302 and query_of(cancelled.url)["history_query"] == history_query
    pending.refresh_from_db()
    assert pending.status == "cancelled" and not Department.objects.filter(code="NO-CREATE").exists()


def test_history_state_counts_exclude_private_asset_imports(context, client, settings, tmp_path):
    batch, _ = imported_assets(context, settings, tmp_path)
    outsider = make_user("history-counter-outsider", "equipment")
    client.force_login(outsider)
    history = client.get(reverse("imports:home"), {"q": str(batch.pk), "import_type": "asset_initialization"})
    assert history.status_code == 200 and not history.context["recent_batches"]
    assert all(card["count"] == 0 for card in history.context["history_status_cards"])
    assert client.get(reverse("imports:batch_detail", args=[batch.pk]), {"history_query": "status=confirmed"}).status_code == 403


def test_created_result_links_accept_only_objects_in_existing_scope(context):
    other_company = make_company("FOREIGN-IMPORT-RESULT", active=False)
    foreign = make_department(other_company, "FOREIGN")
    rows = [SimpleNamespace(validation_status="created", created_object_type="Department", created_object_id=str(obj.pk))
            for obj in (context["department"], foreign)]
    batch = SimpleNamespace(company=context["company"], import_type="department", status="confirmed")
    result = created_results_context(actor=context["admin"], batch=batch, rows=rows)
    assert result["created_results_directory"]["url"] == reverse("masterdata:department-list")
    assert rows[0].created_result["url"] == reverse("masterdata:department-detail", args=[context["department"].pk])
    assert not hasattr(rows[1], "created_result")


def test_return_metadata_accepts_only_history_fields():
    request = RequestFactory().get("/imports/batch/", {"history_query":
        "next=https%3A%2F%2Fexample.com&q=keep&status=invalid&import_type=department&page=2&token=secret"})
    navigation = history_navigation(request)
    assert navigation["history_query"] == "q=keep&import_type=department&status=invalid&page=2"
    assert navigation["history_return_url"].startswith(reverse("imports:home") + "?")
