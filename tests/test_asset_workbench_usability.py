"""Verify daily follow-up priorities and continuous lifecycle work in scope."""

from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import (
    cancel_disposal,
    record_disposal_actual_details,
    return_loan,
)
from tests.test_sprint3_support import make_user
from tests.test_sprint7_disposal_services import _initiate, _record_and_lock
from tests.test_sprint7_lifecycle_services import _loan
from tests.test_sprint7_support import active_asset_context
from tests.test_sprint8_support import add_active_asset


pytestmark = pytest.mark.django_db


class _Links(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.hrefs = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def test_loan_priority_search_and_stage_summary_keep_open_work_visible(client, monkeypatch):
    context, returned_asset, _ = active_asset_context("WORKLOAN")
    today = timezone.localdate()
    returned = _loan(context, returned_asset, "work-returned", expected_return_date=today)
    returned = return_loan(
        actor=context["equipment"], loan=returned, returned_at=today,
        received_by_employee=context["employee"],
        return_department=context["department"],
        return_responsible_employee=context["employee"],
        return_location=context["location"], return_asset_status="in_use",
        idempotency_key="work-returned-complete",
    )
    overdue_asset, _ = add_active_asset(context, "WORKDUE")
    overdue = _loan(
        context, overdue_asset, "work-overdue", expected_return_date=today + timedelta(days=1),
        borrower_type="external", borrower_employee=None,
        borrower_name="现场联络人", borrower_organization="现场服务团队",
    )
    due_asset, _ = add_active_asset(context, "WORKTODAY")
    due = _loan(
        context, due_asset, "work-today", expected_return_date=today + timedelta(days=2),
        borrower_type="external", borrower_employee=None,
        borrower_name="驻场工程师", borrower_organization="现场服务团队",
    )
    monkeypatch.setattr("apps.assets.workbenches.timezone.localdate", lambda: today + timedelta(days=2))
    client.force_login(context["equipment"])
    url = reverse("assets:loan-workbench")

    page = client.get(url, {"state": ""})
    assert page.status_code == 200
    assert [row.pk for row in page.context["page_obj"]] == [overdue.pk, due.pk, returned.pk]
    text = page.content.decode()
    assert "逾期 1 天" in text and "今日到期" in text
    assert "实际：" in text and f"接收人：{context['employee'].name}" in text
    by_date = client.get(url, {"state": "", "order": "date"})
    assert by_date.context["page_obj"][0].pk == returned.pk

    page = client.get(url, {"q": "现场服务团队", "state": "returned", "order": "recent", "page": "2"})
    assert page.context["page_obj"].paginator.count == 0
    assert page.context["work_counts"] == {"active": 2, "overdue": 1, "due": 1, "returned": 0}
    assert parse_qs(urlsplit(page.context["work_links"]["active"]).query) == {
        "q": ["现场服务团队"], "state": ["active"], "order": ["recent"],
    }
    state_chip = next(row for row in page.context["active_filters"] if row["label"] == "办理阶段")
    all_matching = client.get(url + state_chip["url"])
    assert all_matching.context["page_obj"].paginator.count == 2
    assert all_matching.context["filter_form"].cleaned_data["state"] == ""
    by_person = client.get(url, {"q": "驻场工程师"})
    assert [row.pk for row in by_person.context["page_obj"]] == [due.pk]
    by_snapshot = client.get(url, {"q": returned.borrower_name_snapshot, "state": ""})
    assert [row.pk for row in by_snapshot.context["page_obj"]] == [returned.pk]


def test_disposal_priority_and_stage_actions_follow_existing_roles(client):
    context, cancelled_asset, _ = active_asset_context("WORKDISP")
    today = timezone.localdate()
    cancelled = _initiate(context, cancelled_asset, "work-cancelled", planned_disposal_date=today)
    cancel_disposal(
        actor=context["equipment"], disposal=cancelled,
        reason="计划调整", idempotency_key="work-cancelled-close",
    )
    actual_asset, _ = add_active_asset(context, "WORKACTUAL")
    actual = _initiate(context, actual_asset, "work-actual", planned_disposal_date=today + timedelta(days=2))
    finance_asset, _ = add_active_asset(context, "WORKFINANCE")
    finance = _initiate(context, finance_asset, "work-finance", planned_disposal_date=today + timedelta(days=1))
    record_disposal_actual_details(
        actor=context["equipment"], disposal=finance, actual_disposal_date=today,
        handled_by=context["equipment"], idempotency_key="work-finance-actual",
    )
    locked_asset, _ = add_active_asset(context, "WORKLOCKED")
    locked = _initiate(context, locked_asset, "work-locked", planned_disposal_date=today + timedelta(days=3))
    _record_and_lock(context, locked, "work-locked-stage")
    url = reverse("assets:disposal-workbench")

    client.force_login(context["equipment"])
    page = client.get(url, {"state": ""})
    assert page.status_code == 200
    assert [row.pk for row in page.context["page_obj"]] == [finance.pk, actual.pk, locked.pk, cancelled.pk]
    rows = {row.pk: row for row in page.context["page_obj"]}
    assert rows[actual.pk].work_action_url == reverse("assets:disposal-actual", args=[actual.pk])
    assert rows[finance.pk].work_action_url is None
    assert rows[locked.pk].work_action_url == reverse("assets:disposal-complete", args=[locked.pk])
    assert rows[cancelled.pk].work_action_url is None
    assert page.context["work_counts"] == {"actual": 1, "finance": 1, "complete": 1, "closed": 1}
    by_date = client.get(url, {"state": "", "order": "date"})
    assert by_date.context["page_obj"][0].pk == cancelled.pk

    client.force_login(context["finance"])
    page = client.get(url, {"state": "finance"})
    assert page.context["page_obj"][0].work_action_url == reverse("assets:disposal-finance-lock", args=[finance.pk])
    client.force_login(make_user("workbench-management", "management"))
    page = client.get(url, {"state": ""})
    assert page.context["page_obj"].paginator.count == 4
    assert all(row.work_action_url is None for row in page.context["page_obj"])
    client.force_login(make_user("workbench-disposal-outsider", "employee"))
    page = client.get(url, {"state": ""})
    assert page.context["page_obj"].paginator.count == 0
    assert not any(page.context["work_counts"].values())


def test_direct_disposal_action_preserves_query_and_returns_after_save(client):
    context, asset, _ = active_asset_context("WORKRETURN")
    disposal = _initiate(context, asset, "work-return-query")
    client.force_login(context["equipment"])
    workbench = reverse("assets:disposal-workbench")
    query = urlencode({"q": asset.asset_code, "state": "actual", "order": "recent", "page": "1"})
    return_to = workbench + "?" + query
    page = client.get(return_to)
    action_path = reverse("assets:disposal-actual", args=[disposal.pk])
    action_url = next(href for href in _Links(page.content.decode()).hrefs if urlsplit(href).path == action_path)
    assert parse_qs(urlsplit(action_url).query) == {"return_to": [return_to]}

    form_page = client.get(action_url)
    assert form_page.status_code == 200
    assert form_page.context["cancel_url"] == return_to
    form = form_page.context["form"]
    saved = client.post(action_url, {
        "actual_disposal_date": timezone.localdate().isoformat(),
        "recipient_name": "", "expected_status": form["expected_status"].value(),
        "idempotency_key": form["idempotency_key"].value(), "return_to": return_to,
    })
    assert saved.status_code == 302, saved.context["form"].errors
    assert saved.url == return_to
    disposal.refresh_from_db()
    assert disposal.actual_disposal_date == timezone.localdate()
    following = client.get(saved.url)
    assert following.context["page_obj"].paginator.count == 0
    assert following.context["work_counts"]["finance"] == 1
    assert parse_qs(urlsplit(following.context["work_links"]["finance"]).query) == {
        "q": [asset.asset_code], "state": ["finance"], "order": ["recent"],
    }
