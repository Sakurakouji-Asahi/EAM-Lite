"""Record lookup keeps its origin and rechecks the existing read scope."""
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.maintenance.models import MaintenanceRecord
from apps.maintenance.record_navigation import record_navigation_context
from apps.maintenance.services import _enable_capability
from tests.test_asset_list_return_navigation import Links
from tests.test_sprint3_support import make_user
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db


def history(ctx, count=1):
    rows = [MaintenanceRecord(
        company=ctx["company"], maintenance_plan=ctx["plan"], asset=ctx["asset"],
        scheduled_date=ctx["plan"].first_due_date - timedelta(days=index + 10),
        completed_date=ctx["plan"].first_due_date - timedelta(days=index + 10),
        completed_by=ctx["responsible"], content_snapshot="清洁风扇并紧固螺丝",
        result="normal", remark="复查通过", idempotency_key=f"record-navigation-{index}",
    ) for index in range(count)]
    # PostgreSQL record triggers accept one controlled insert per enabled flag.
    for row in rows:
        _enable_capability("controlled_maintenance_record_insert")
        row.save(force_insert=True)
    return rows


def detail_link(response):
    return next(url for url, label in Links(response).links if label.strip() == "详情")


def test_record_list_second_page_returns_to_exact_query_without_writes(client):
    ctx = maintenance_context("RECORDRETURN")
    history(ctx, 26)
    client.force_login(ctx["equipment"])
    origin = reverse("maintenance:record-list") + "?" + urlencode({
        "q": "RECORDRETURN", "status": "confirmed", "page_size": 25, "page": 2,
    })
    listing = client.get(origin)
    assert listing.status_code == 200 and listing.context["page_obj"].number == 2
    assert len(listing.context["records"]) == 1
    before = list(MaintenanceRecord.objects.values().order_by("pk"))
    link = detail_link(listing)
    assert parse_qs(urlsplit(link).query)["return_to"] == [origin]
    detail = client.get(link)
    assert detail.status_code == 200 and detail.context["record_return_url"] == origin
    returned = client.get(detail.context["record_return_url"])
    assert [r.pk for r in returned.context["records"]] == [r.pk for r in listing.context["records"]]
    assert list(MaintenanceRecord.objects.values().order_by("pk")) == before


def test_plan_content_search_and_record_return_preserve_nested_plan_origin(client):
    ctx = maintenance_context("HISTORYSEARCH")
    record = history(ctx)[0]
    client.force_login(ctx["equipment"])
    plan_list = reverse("maintenance:plan-list") + "?q=HISTORYSEARCH&status=active"
    origin = reverse("maintenance:plan-detail", args=[ctx["plan"].pk]) + "?" + urlencode({
        "q": "风扇", "result": "normal", "status": "confirmed", "page_size": 50,
        "return_to": plan_list,
    })
    page = client.get(origin)
    assert page.status_code == 200 and list(page.context["records"]) == [record]
    detail = client.get(detail_link(page))
    assert detail.context["record_return_url"] == origin
    assert detail.context["record_return_label"] == "返回计划时间线"
    returned = client.get(detail.context["record_return_url"])
    assert returned.context["plan_list_url"] == plan_list
    assert returned.context["filter_form"]["q"].value() == "风扇"
    for query, expected in (("复查", 1), ("不存在的内容", 0)):
        result = client.get(reverse("maintenance:plan-detail", args=[ctx["plan"].pk]), {"q": query})
        assert result.status_code == 200 and result.context["page_obj"].paginator.count == expected


def test_record_navigation_only_returns_to_record_list_or_its_own_plan(client):
    ctx = maintenance_context("RECORDSAFETY")
    record = history(ctx)[0]
    fallback = reverse("maintenance:record-list")
    request = RequestFactory()
    for value in (
        "https://example.test/maintenance/records/", "//example.test/maintenance/records/",
        fallback + "#fragment", fallback + "\n", fallback + "\\elsewhere",
        reverse("maintenance:plan-edit", args=[ctx["plan"].pk]),
        "/maintenance/plans/00000000-0000-0000-0000-000000000001/",
    ):
        context = record_navigation_context(request.get("/", {"return_to": value}), record)
        assert context["record_return_url"] == fallback
    unrelated = make_user("record-unrelated", "employee")
    client.force_login(unrelated)
    denied = client.get(reverse("maintenance:record-detail", args=[record.pk]), {"return_to": fallback})
    assert denied.status_code == 403
    listing = client.get(fallback)
    assert listing.status_code == 200 and not listing.context["records"]
