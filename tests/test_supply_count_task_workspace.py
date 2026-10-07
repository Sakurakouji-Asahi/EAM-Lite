from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse

from apps.supplies.services import (cancel_supply_count_task, publish_supply_count_task,
    record_supply_count, stop_supply_count_entry)
from tests.test_count_role_scope import count_role_scope
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_supply_item, make_user, seed_supply_stock
from tests.test_sprint17_services import make_count


pytestmark = pytest.mark.django_db


@pytest.fixture
def count_workspace(client):
    company, _, department, employee, source, target, first, second = supply_context()
    actor = make_user("task-workspace-operator", "warehouse", "equipment")
    for index, item in enumerate((first, second)):
        seed_supply_stock(actor=actor, company=company, warehouse=source, item=item,
            quantity="5.1234", unit_cost="10", key=f"task-workspace-seed-{index}")
    active = make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=source, key="workspace-active")
    publish_supply_count_task(actor=actor, task=active)
    draft = make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=target, key="workspace-draft")
    cancelled = make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=target, key="workspace-cancelled")
    cancel_supply_count_task(actor=actor, task=cancelled, reason="已安排另一任务")
    custody = make_count(actor=actor, company=company, domain="custody", department=department,
        employee=employee, key="workspace-custody")
    client.force_login(actor)
    return company, actor, department, employee, source, target, first, second, active, draft, custody


def test_task_search_finds_warehouse_department_and_employee_identifiers_and_names(client, count_workspace):
    _, _, department, employee, warehouse, _, _, _, active, _, custody = count_workspace
    for query, expected in ((warehouse.code, active), (warehouse.name, active),
            (department.code, custody), (department.name, custody),
            (employee.employee_no, custody), (employee.name, custody)):
        page = client.get(reverse("supplies:count-task-list"), {"q": query})
        assert page.status_code == 200
        assert {task.pk for task in page.context["page_obj"]} == {expected.pk}


def test_query_status_summary_and_removable_filters_keep_other_conditions(client, count_workspace):
    _, _, _, _, _, _, _, _, _, draft, _ = count_workspace
    page = client.get(reverse("supplies:count-task-list"), {"q": "workspace", "status": "draft",
        "count_domain": "warehouse_stock", "date_from": "2026-08-01", "page": 4})
    assert {task.pk for task in page.context["page_obj"]} == {draft.pk}
    assert page.context["task_summary"] == {"total": 3, "draft": 1, "in_progress": 1,
        "reconciliation": 0, "closed": 0, "cancelled": 1, "open": 2}
    for link in page.context["count_status_links"]:
        params = parse_qs(urlsplit(link["url"]).query)
        assert params["q"] == ["workspace"] and params["count_domain"] == ["warehouse_stock"]
        assert "page" not in params
    chip = next(chip for chip in page.context["count_filter_chips"] if chip["label"] == "状态")
    params = parse_qs(urlsplit(chip["url"]).query)
    assert "status" not in params and "page" not in params and params["date_from"] == ["2026-08-01"]
    empty = client.get(reverse("supplies:count-task-list"), {"q": "NO-MATCH-TASK"})
    assert empty.status_code == 200 and empty.context["task_summary"]["total"] == 0
    assert "清除条件重新查看" in empty.content.decode()
    assert client.get(reverse("supplies:count-task-list"), {"date_from": "invalid"}).status_code == 400


def test_status_counts_use_authorized_distinct_tasks_for_mixed_roles(client, count_role_scope):
    client.force_login(count_role_scope.actor)
    page = client.get(reverse("supplies:count-task-list"), {"status": "in_progress"})
    assert page.status_code == 200
    assert page.context["task_summary"]["total"] == 2
    assert page.context["task_summary"]["in_progress"] == 2
    assert {task.pk for task in page.context["page_obj"]} == {count_role_scope.task_a.pk, count_role_scope.task_b.pk}


def test_action_chain_preserves_detail_filters_and_original_task_list(client, count_workspace):
    company, actor, _, _, _, _, _, _, task, _, _ = count_workspace
    item = make_supply_item(company, task.lines.first().item.category, "WORKSPACE-NEW")
    list_url = reverse("supplies:count-task-list") + "?q=SOURCE&status=open&page=2"
    detail_params = {"q": item.item_code, "row_view": "needs_cost", "page_size": 50, "page": 2}
    action_data = {"return_to": list_url, "return_query": urlencode(detail_params)}
    destination = reverse("supplies:count-task-detail", args=[task.pk]) + "?" + urlencode({**detail_params, "return_to": list_url})
    added = client.post(reverse("supplies:count-task-add-item", args=[task.pk]), {"item": str(item.pk), **action_data})
    assert added.status_code == 302 and added.url == destination
    line = task.lines.get(item=item)
    for row in task.lines.exclude(pk=line.pk):
        record_supply_count(actor=actor, line=row, counted_quantity=row.expected_quantity, remark="")
    recorded = client.post(reverse("supplies:count-line-record", args=[task.pk, line.pk]), {
        "counted_quantity": "1.2345", "remark": "现场新增盘盈", "return_query": urlencode({**detail_params, "return_to": list_url})})
    assert recorded.status_code == 302 and recorded.url == destination
    stopped = client.post(reverse("supplies:count-task-stop", args=[task.pk]), {"confirm": "on", **action_data})
    assert stopped.status_code == 302 and stopped.url == destination
    task.refresh_from_db()
    assert task.status == "reconciliation"
    cost_page = client.get(reverse("supplies:count-line-adjustment-cost", args=[task.pk, line.pk]), action_data)
    assert cost_page.context["count_detail_url"] == destination
    assert task.task_no in cost_page.content.decode() and f"1.2345 {line.item.unit}" in cost_page.content.decode()
    saved = client.post(reverse("supplies:count-line-adjustment-cost", args=[task.pk, line.pk]), {"unit_cost": "1.234567", **action_data})
    assert saved.status_code == 302 and saved.url == destination
    closed = client.post(reverse("supplies:count-task-close", args=[task.pk]), {"confirm": "on", **action_data})
    assert closed.status_code == 302 and closed.url == destination
    detail = client.get(destination)
    assert detail.context["count_list_url"] == list_url
    assert 'name="return_to"' in detail.content.decode()
    assert "当前筛选没有符合条件的明细" in detail.content.decode()
    assert detail.context["count_query_reset_url"].endswith(urlencode({"return_to": list_url}))
    task.refresh_from_db()
    assert task.status == "closed"


def test_create_publish_and_cancel_preserve_back_query_and_invalid_inputs(client, count_workspace):
    _, _, _, _, _, _, _, _, _, draft, _ = count_workspace
    list_url = reverse("supplies:count-task-list") + "?q=workspace&status=draft&page=2"
    fields = {"return_to": list_url, "return_query": "q=PAPER&row_view=unrecorded&page_size=50&page=2"}
    destination = reverse("supplies:count-task-detail", args=[draft.pk]) + "?" + fields["return_query"] + "&" + urlencode({"return_to": list_url})
    publish_page = client.get(reverse("supplies:count-task-publish", args=[draft.pk]), fields)
    assert publish_page.status_code == 200 and publish_page.context["count_detail_url"] == destination
    assert draft.task_no in publish_page.content.decode()
    error = client.post(reverse("supplies:count-task-cancel", args=[draft.pk]), {"reason": "", **fields})
    assert error.status_code == 200 and error.context["count_detail_url"] == destination
    assert 'data-unsaved-guard="true"' in error.content.decode()
    cancelled = client.post(reverse("supplies:count-task-cancel", args=[draft.pk]), {"reason": "办理上下文验收", **fields})
    assert cancelled.status_code == 302 and cancelled.url == destination
    create_error = client.post(reverse("supplies:count-task-create"), {"return_to": list_url})
    assert create_error.status_code == 200 and create_error.context["count_list_url"] == list_url


@pytest.mark.parametrize("return_to", ("https://example.org/supplies/counts/", "//example.org/supplies/counts/",
    "/supplies/items/", "/supplies/counts/../items/", "/supplies/counts/\\unsafe"))
def test_return_target_only_accepts_local_task_list(client, count_workspace, return_to):
    task = count_workspace[-3]
    page = client.get(reverse("supplies:count-task-detail", args=[task.pk]), {"return_to": return_to})
    assert page.status_code == 200
    assert page.context["count_list_url"] == reverse("supplies:count-task-list")
    assert page.context["count_list_return"] == ""
