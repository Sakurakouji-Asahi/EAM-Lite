"""Find historical files without losing list context or authorized scope."""
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet
from django.http import QueryDict
from django.test import RequestFactory
from django.urls import reverse

from apps.masterdata.models import Attachment
from apps.reports.export_history import export_file_context, export_history_return_url
from apps.reports.models import ExportLog
from apps.reports.services import generate_report_export
from tests.test_sprint7_support import active_asset_context
from tests.test_sprint11_services_database import isolated_export_storage


def _pending(context, key, actor=None, report_key="asset_ledger"):
    return ExportLog.objects.create(company=context["company"], export_type=report_key,
        requested_by=actor or context["finance"], idempotency_key=key, request_hash="a" * 64)


@pytest.mark.django_db
def test_export_search_and_status_counts_use_only_authorized_history(client, monkeypatch, isolated_export_storage):
    context, asset, _identity = active_asset_context("HISTORYFIND")
    completed = generate_report_export(actor=context["finance"], company=context["company"], report_key="asset_ledger",
        filters={"q": asset.asset_code}, idempotency_key="history-find-completed")
    with monkeypatch.context() as patch:
        def fail_dataset(**_kwargs):
            raise ValidationError("请核对日期 <现场复核>")
        patch.setattr("apps.reports.services.build_report_dataset", fail_dataset)
        with pytest.raises(ValidationError):
            generate_report_export(actor=context["finance"], company=context["company"], report_key="asset_ledger",
                filters={}, idempotency_key="history-find-failed")
    failed = ExportLog.objects.get(idempotency_key="history-find-failed")
    _pending(context, "history-find-pending")
    own = _pending(context, "history-find-own", actor=context["equipment"])
    hidden = _pending(context, "history-find-financial", report_key="monthly_depreciation")
    client.force_login(context["equipment"])
    url = reverse("reports:export-history")
    params = {"q": context["finance"].username, "status": "failed", "page": "2"}
    page = client.get(url, params)
    assert page.status_code == 200
    assert [row.pk for row in page.context["page_obj"]] == [failed.pk]
    counts = {row["label"]: row["count"] for row in page.context["status_summary"]}
    assert counts == {"全部状态": 3, "生成中": 1, "已完成": 1, "失败": 1, "已过期": 0}
    completed_card = next(item for item in page.context["status_summary"] if item["label"] == "已完成")
    assert parse_qs(urlsplit(completed_card["url"]).query) == {"q": [context["finance"].username], "status": ["completed"]}
    html = page.content.decode()
    assert "请核对日期 &lt;现场复核&gt;" in html and str(hidden.pk) not in html and "月度折旧" not in html
    assert "no-store" in page.headers["Cache-Control"]
    by_filename = client.get(url, {"q": completed.output_attachment.safe_filename})
    row, = by_filename.context["page_obj"]
    assert row.pk == completed.pk and row.file_context["can_download"] is True
    assert parse_qs(urlsplit(row.source_url).query)["q"] == [asset.asset_code]
    assert reverse("reports:export-download", args=[completed.pk]) in by_filename.content.decode()
    assert [row.pk for row in client.get(url, {"q": str(failed.pk)}).context["page_obj"]] == [failed.pk]
    assert client.get(url, {"q": str(hidden.pk)}).context["page_obj"].paginator.count == 0
    assert [row.pk for row in client.get(url, {"mine": "on"}).context["page_obj"]] == [own.pk]


@pytest.mark.django_db
def test_export_list_and_detail_do_not_offer_unavailable_files(client, isolated_export_storage):
    context, _asset, _identity = active_asset_context("HISTORYUNAVAILABLE")
    export = generate_report_export(actor=context["finance"], company=context["company"], report_key="asset_ledger",
        filters={}, idempotency_key="history-unavailable-completed")
    # Manufacture an unavailable historical attachment; presentation must respect it.
    QuerySet.update(Attachment._base_manager.filter(pk=export.output_attachment_id), is_available=False)
    client.force_login(context["finance"])
    page = client.get(reverse("reports:export-history"), {"q": str(export.pk)})
    row, = page.context["page_obj"]
    assert row.file_context["can_download"] is False
    assert "文件暂不可用" in page.content.decode()
    download_url = reverse("reports:export-download", args=[export.pk])
    assert download_url not in page.content.decode()
    detail = client.get(reverse("reports:export-detail", args=[export.pk]))
    assert detail.context["can_download"] is False and "文件暂不可用" in detail.content.decode()
    assert download_url not in detail.content.decode()
    assert client.get(download_url).status_code == 404


@pytest.mark.django_db
def test_failed_export_with_invalid_original_filter_can_be_found_and_requeried(client, isolated_export_storage):
    context, _asset, _identity = active_asset_context("HISTORYFAILEDINPUT")
    with pytest.raises(ValidationError):
        generate_report_export(actor=context["finance"], company=context["company"], report_key="asset_ledger",
            filters={"department": "not-a-valid-department"}, idempotency_key="history-invalid-original-filter")
    export = ExportLog.objects.get(idempotency_key="history-invalid-original-filter")
    assert export.status == "failed"
    client.force_login(context["finance"])
    page = client.get(reverse("reports:export-history"), {"q": str(export.pk)})
    assert page.status_code == 200
    row, = page.context["page_obj"]
    assert parse_qs(urlsplit(row.source_url).query)["department"] == ["not-a-valid-department"]
    detail = client.get(reverse("reports:export-detail", args=[export.pk]))
    assert detail.status_code == 200 and detail.context["display_filters"]["部门"] == "not-a-valid-department"
    original_query = client.get(row.source_url)
    assert original_query.status_code == 400


@pytest.mark.django_db
def test_history_detail_restores_page_and_filters_and_invalid_queries_show_no_records(client):
    context, _asset, _identity = active_asset_context("HISTORYRETURN")
    for index in range(27):
        _pending(context, f"history-return-{index}")
    client.force_login(context["finance"])
    url = reverse("reports:export-history")
    params = {"q": context["finance"].display_name, "status": "pending", "mine": "on", "page": "2"}
    page = client.get(url, params)
    assert page.status_code == 200 and len(page.context["page_obj"]) == 2
    record = page.context["page_obj"][0]
    detail = client.get(reverse("reports:export-detail", args=[record.pk]), {"history_query": page.context["history_query"]})
    assert urlsplit(detail.context["history_return_url"]).path == url
    assert QueryDict(urlsplit(detail.context["history_return_url"]).query).dict() == params
    restored = client.get(detail.context["history_return_url"])
    assert restored.context["page_obj"].number == 2 and len(restored.context["page_obj"]) == 2
    invalid = client.get(url, {"date_from": "2026-09-30", "date_to": "2026-09-01"})
    assert invalid.status_code == 400 and invalid.context["page_obj"].paginator.count == 0
    assert "请修正查询条件后重试" in invalid.content.decode()


@pytest.mark.parametrize("status,note", [("pending", "文件正在生成"), ("failed", "失败原因"), ("expired", "文件已过期")])
def test_pending_failed_and_expired_exports_have_actionable_notes(status, note):
    export = ExportLog(status=status, error_summary="失败原因")
    result = export_file_context(None, export)
    assert result["can_download"] is False and note in result["file_note"]


def test_history_return_url_is_fixed_to_history_and_ignores_other_parameters():
    request = RequestFactory().get("/", {"history_query": "q=https%3A%2F%2Fexample.invalid%2F&return_to=%2Fadmin%2F&page=2"})
    result = urlsplit(export_history_return_url(request))
    assert not result.scheme and not result.netloc
    assert result.path == reverse("reports:export-history")
    assert parse_qs(result.query) == {"q": ["https://example.invalid/"], "page": ["2"]}
