from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils.html import escape

from apps.maintenance.assignment import assign_problem, assignment_version
from apps.maintenance.domain import business_date
from apps.maintenance.problem_workspace import problem_return_url
from apps.maintenance.services import (
    close_maintenance_problem,
    create_maintenance_plan,
    void_maintenance_record,
)
from tests.test_asset_list_return_navigation import Links
from tests.test_sprint3_support import make_user
from tests.test_sprint9_services import _complete
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def issue(context, key, *, target_days=None, description=None):
    completed = business_date() - timedelta(days=10)
    plan = create_maintenance_plan(
        actor=context["equipment"], company=context["company"], asset=context["asset"],
        name=f"{key} 检查计划", cycle_value=1, cycle_unit="month",
        responsible_employee=context["responsible"], advance_notice_days=3,
        standard_content="检查并记录异常", first_due_date=completed,
    )
    record = _complete(
        {**context, "plan": plan}, f"{key}-complete", completed_date=completed,
        result="problem_found", problem_description=description or f"{key} 防护罩松动",
    )
    if target_days is not None:
        assign_problem(
            actor=context["equipment"], problem=record.problem,
            owner_employee=context["responsible"],
            target_date=business_date() + timedelta(days=target_days), reason="安排复查",
            idempotency_key=f"{key}-assign", expected_assignment=assignment_version(record.problem),
        )
        record.problem.refresh_from_db()
    return record


def test_deadline_summary_covers_all_matching_pages_and_week_boundaries(client):
    context = maintenance_context("PROBLEMATTENTION")
    overdue = issue(context, "ATTENTION-OVERDUE", target_days=-3)
    today = issue(context, "ATTENTION-TODAY", target_days=0)
    next_day = issue(context, "ATTENTION-NEXT", target_days=1)
    last_day = issue(context, "ATTENTION-LAST", target_days=7)
    issue(context, "ATTENTION-LATER", target_days=8)
    issue(context, "ATTENTION-UNASSIGNED")
    closed = issue(context, "ATTENTION-CLOSED", target_days=-1)
    close_maintenance_problem(
        actor=context["equipment"], problem=closed.problem, closure_note="复查通过",
        idempotency_key="attention-close",
    )
    voided = issue(context, "ATTENTION-VOID", target_days=-2)
    void_maintenance_record(
        actor=context["equipment"], record=voided, reason="重复登记",
        idempotency_key="attention-void",
    )
    client.force_login(context["equipment"])
    url = reverse("maintenance:problem-list")
    filters = {"q": context["asset"].asset_code, "status": "closed", "due_scope": "overdue",
               "page_size": "50", "page": "2"}
    page = client.get(url, filters)
    assert page.status_code == 200
    assert not page.context["items"]
    assert page.context["attention_counts"] == {"overdue": 1, "today": 1, "week": 2, "unassigned": 1}
    assert page.context["problem_counts"] == {"open": 1, "closed": 0}
    for scope, link in page.context["attention_links"].items():
        query = parse_qs(urlsplit(link).query)
        assert query["q"] == [context["asset"].asset_code]
        assert query["status"] == ["open"] and query["due_scope"] == [scope]
        assert query["page_size"] == ["50"] and "page" not in query
    week = client.get(url + page.context["attention_links"]["week"])
    assert [row["problem"].pk for row in week.context["items"]] == [next_day.problem.pk, last_day.problem.pk]
    assert [row["timing"]["label"] for row in week.context["items"]] == ["距到期 1 天", "距到期 7 天"]
    assert client.get(url, {"due_scope": "today"}).context["items"][0]["problem"].pk == today.problem.pk
    assert client.get(url, {"due_scope": "overdue"}).context["items"][0]["timing"]["label"] == "逾期 3 天"
    owner = client.get(url, {"owner_employee": context["responsible"].pk})
    assert owner.context["attention_counts"] == {"overdue": 1, "today": 1, "week": 2, "unassigned": 0}
    assert overdue.problem.pk in {row["problem"].pk for row in owner.context["items"]}


def test_problem_actions_keep_context_errors_and_original_query_through_record_and_save(client):
    context = maintenance_context("PROBLEMRETURN")
    query = '跟进 & + / ? = % # "检查"'
    record = issue(context, "RETURN-ISSUE", description=query)
    problem = record.problem
    client.force_login(context["equipment"])
    list_url = reverse("maintenance:problem-list") + "?" + urlencode({
        "q": query, "status": "open", "due_scope": "unassigned", "page": 2, "page_size": 50,
    })
    listing = client.get(list_url)
    assign_url = reverse("maintenance:problem-assign", args=[problem.pk])
    close_url = reverse("maintenance:problem-close", args=[problem.pk])
    record_url = reverse("maintenance:record-detail", args=[record.pk])
    for path in (assign_url, close_url, record_url):
        href = Links(listing).hrefs_for_path(path)[0]
        assert parse_qs(urlsplit(href).query)["return_to"] == [list_url]
    detail = client.get(Links(listing).hrefs_for_path(record_url)[0])
    assert detail.context["problem_list_url"] == list_url
    assert parse_qs(urlsplit(Links(detail).hrefs_for_path(assign_url)[0]).query)["return_to"] == [list_url]
    assignment = client.get(Links(detail).hrefs_for_path(assign_url)[0])
    assert assignment.context["problem"].pk == problem.pk
    assert assignment.context["cancel_url"] == list_url
    assert assignment.context["problem_timing"]["label"] == "未设期限"
    assert escape(query) in assignment.content.decode()
    assert context["asset"].asset_code in assignment.content.decode()
    invalid = client.post(assign_url, {"return_to": list_url})
    assert invalid.status_code == 200 and invalid.context["form"].errors
    assert invalid.context["problem_return_to"] == list_url
    assert 'data-unsaved-guard="true"' in invalid.content.decode()
    data = {
        "owner_employee": context["responsible"].pk,
        "target_date": business_date().isoformat(), "reason": "安排当天复查",
        "idempotency_key": assignment.context["form"]["idempotency_key"].value(),
        "expected_assignment": assignment.context["form"]["expected_assignment"].value(),
        "return_to": list_url,
    }
    assert client.post(assign_url, data).url == list_url
    assert client.post(assign_url, data).url == list_url
    close = client.get(close_url, {"return_to": list_url})
    assert close.context["problem_timing"]["label"] == "今日到期"
    assert context["responsible"].employee_no in close.content.decode()
    incomplete = client.post(close_url, {"return_to": list_url, "closure_note": "复查通过"})
    assert incomplete.status_code == 200 and incomplete.context["form"].errors
    assert incomplete.context["cancel_url"] == list_url
    closing = {"return_to": list_url, "closure_note": "复查通过", "confirm": "on",
               "idempotency_key": close.context["form"]["idempotency_key"].value()}
    assert client.post(close_url, closing).url == list_url
    assert client.post(close_url, closing).url == list_url
    problem.refresh_from_db()
    assert problem.status == "closed" and problem.closure_note == "复查通过"


def test_problem_summary_respects_scope_invalid_filters_and_safe_action_fallback(client):
    context = maintenance_context("PROBLEMSCOPE")
    record = issue(context, "SCOPE-ISSUE", target_days=-1)
    list_url = reverse("maintenance:problem-list")
    client.force_login(context["equipment"])
    invalid = client.get(list_url, {"due_scope": "invented"})
    assert invalid.status_code == 400 and not invalid.context["items"]
    assert invalid.context["attention_counts"] == {"overdue": 0, "today": 0, "week": 0, "unassigned": 0}
    close_url = reverse("maintenance:problem-close", args=[record.problem.pk])
    record_url = reverse("maintenance:record-detail", args=[record.pk])
    close = client.get(close_url, {"return_to": "https://example.invalid/"})
    assert close.context["cancel_url"] == record_url
    assert not close.context["problem_return_to"]
    saved = client.post(close_url, {"return_to": "/accounts/logout/", "closure_note": "核验通过",
        "confirm": "on", "idempotency_key": close.context["form"]["idempotency_key"].value()})
    assert saved.url == record_url
    client.force_login(make_user("problem-workspace-outsider", "employee"))
    denied = client.get(list_url)
    assert not denied.context["items"]
    assert denied.context["attention_counts"] == {"overdue": 0, "today": 0, "week": 0, "unassigned": 0}
    assert client.get(close_url).status_code in (403, 404)


@pytest.mark.parametrize("value", [
    "https://example.invalid/maintenance/problems/", "//example.invalid/maintenance/problems/",
    "/accounts/logout/", "/maintenance/problems/\\evil", "/maintenance/problems/?q=bad\n",
    "/maintenance/problems/?q=" + "a" * 3000,
])
def test_return_navigation_only_accepts_local_problem_list(value):
    request = RequestFactory().get("/", {"return_to": value})
    assert problem_return_url(request, "fallback") == "fallback"
