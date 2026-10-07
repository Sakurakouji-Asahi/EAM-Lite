from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from apps.reports.export_history import history_date_ranges
from apps.reports.models import ExportLog
from tests.test_sprint7_support import active_asset_context


@pytest.mark.parametrize("today,previous_start,previous_end", [
    (date(2027,1,1), date(2026,12,1), date(2026,12,31)),
    (date(2028,3,1), date(2028,2,1), date(2028,2,29)),
])
def test_history_shortcut_month_boundaries_and_seven_inclusive_dates(today, previous_start, previous_end):
    ranges = {label:(start,end) for label,start,end in history_date_ranges(today)}
    assert ranges["今天"] == (today,today)
    assert ranges["近 7 天"] == (today-timedelta(days=6),today)
    assert ranges["本月"] == (today.replace(day=1),today)
    assert ranges["上月"] == (previous_start,previous_end)


@pytest.mark.django_db
def test_history_date_shortcuts_keep_filters_and_include_end_of_day(client, monkeypatch):
    context, _asset, _identity = active_asset_context("EXPORTDATES")
    today = date(2028,3,1)
    stamps = [datetime.combine(today, time.min), datetime.combine(today,time(23,59,59)),
        datetime.combine(today-timedelta(days=1),time(23,59,59)), datetime.combine(today+timedelta(days=1),time.min)]
    records = []
    for index, stamp in enumerate(stamps):
        with monkeypatch.context() as patch:
            patch.setattr("django.utils.timezone.now", lambda value=stamp: value.replace(tzinfo=ZoneInfo("Asia/Shanghai")))
            records.append(ExportLog.objects.create(company=context["company"], export_type="asset_ledger",
                requested_by=context["finance"], request_hash="a"*64, idempotency_key=f"export-date-{index}"))
    client.force_login(context["finance"])
    monkeypatch.setattr("apps.reports.export_history.timezone.localdate", lambda:today)
    url = reverse("reports:export-history")
    params = {"q":context["finance"].username, "report_type":"asset_ledger", "status":"pending", "mine":"on", "page":"3"}
    response = client.get(url,params)
    assert response.status_code == 200
    shortcut = next(item for item in response.context["history_date_shortcuts"] if item["label"] == "今天")
    shortcut_params = parse_qs(urlsplit(shortcut["url"]).query)
    for name in ("q","report_type","status","mine"):
        assert shortcut_params[name] == [params[name]]
    assert "page" not in shortcut_params
    today_page = client.get(shortcut["url"])
    assert today_page.status_code == 200
    assert {record.pk for record in today_page.context["page_obj"]} == {record.pk for record in records[:2]}
    assert next(item for item in today_page.context["history_date_shortcuts"] if item["label"] == "今天")["selected"]
    chips = {item["label"]:item for item in today_page.context["history_filter_chips"]}
    removed_status = parse_qs(urlsplit(chips["导出状态"]["url"]).query)
    assert "status" not in removed_status
    assert removed_status["date_from"] == [today.isoformat()] and removed_status["mine"] == ["on"]
    removed_dates = parse_qs(urlsplit(chips["请求日期"]["url"]).query)
    assert "date_from" not in removed_dates and "date_to" not in removed_dates
    assert removed_dates["status"] == ["pending"] and removed_dates["q"] == [params["q"]]
    record = today_page.context["page_obj"][0]
    detail = client.get(reverse("reports:export-detail",args=[record.pk]), {"history_query":today_page.context["history_query"]})
    assert parse_qs(urlsplit(detail.context["history_return_url"]).query) == shortcut_params
    assert ExportLog.objects.count() == 4


@pytest.mark.django_db
def test_invalid_date_range_has_no_applied_summary_and_shortcut_repairs_only_dates(client, monkeypatch):
    context, _asset, _identity = active_asset_context("EXPORTBADDATE")
    client.force_login(context["finance"])
    monkeypatch.setattr("apps.reports.export_history.timezone.localdate", lambda:date(2028,3,1))
    response = client.get(reverse("reports:export-history"), {"q":"<条件提示>", "report_type":"asset_ledger",
        "mine":"on", "date_from":"2028-03-05", "date_to":"2028-03-01", "page":"7"})
    assert response.status_code == 400
    assert response.context["history_filter_chips"] == []
    assert response.context["page_obj"].paginator.count == 0
    today = next(item for item in response.context["history_date_shortcuts"] if item["label"] == "今天")
    repaired = client.get(today["url"])
    assert repaired.status_code == 200
    params = parse_qs(urlsplit(today["url"]).query)
    assert params["q"] == ["<条件提示>"] and params["mine"] == ["on"]
    assert "page" not in params
    assert "&lt;条件提示&gt;" in repaired.content.decode()
    assert "结束日包含在内" in repaired.content.decode()
