"""Constructed business cases for finding and following up operational work."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inventory.services import resolve_inventory_difference, stop_inventory_scanning
from apps.maintenance.services import (
    close_maintenance_problem,
    create_maintenance_plan,
    void_maintenance_record,
)
from apps.maintenance.models import MaintenanceProblem
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _published, _scan
from tests.test_sprint8_support import add_active_asset, inventory_context
from tests.test_sprint9_services import _complete
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db(transaction=True)


def test_inventory_results_page_and_filter_effective_evidence_without_shrinking_totals(client):
    context, normal_asset, normal_qr = inventory_context("RESULTFILTER")
    others = [add_active_asset(context, f"RESULTFILTER-{index:02d}") for index in range(26)]
    exception_asset, exception_qr = others[0]
    resolved_asset, _ = others[1]
    normal_asset.equipment_number = "EQ-FILTER-ONLY"
    normal_asset.save(update_fields=["equipment_number"])
    _department, _employee, other_location = add_target_assignment(context, "RESULTFILTER-OTHER")
    task = _published(context, "RESULTFILTER-TASK")
    _scan(context, task, normal_qr, "result-old-scan", actual_location=other_location)
    _scan(context, task, normal_qr, "result-current-scan")
    _scan(context, task, exception_qr, "result-exception", actual_location=other_location)
    task = stop_inventory_scanning(
        actor=context["finance"], task=task, reason="现场完成", idempotency_key="result-stop"
    )
    resolve_inventory_difference(
        actor=context["finance"], task_asset=task.task_assets.get(asset=resolved_asset),
        resolution_type="loss_confirmed", conclusion="确认盘亏，另行办理处置", idempotency_key="result-resolve",
    )
    client.force_login(context["equipment"])
    url = reverse("inventory:task-detail", args=[task.pk])
    first = client.get(url)
    assert first.status_code == 200
    assert len(first.context["row_items"]) == 25
    assert first.context["page_obj"].paginator.count == 27
    second = client.get(url, {"page": 2})
    assert len(second.context["row_items"]) == 2
    ids = {item["row"].pk for page in (first, second) for item in page.context["row_items"]}
    assert len(ids) == 27
    summary = first.context["summary"]
    assert summary == {"expected": 27, "scanned": 2, "normal": 1, "exception": 1,
                       "missing": 25, "surplus": 0, "unresolved": 25}
    normal = client.get(url, {"q": "EQ-FILTER-ONLY", "row_view": "normal", "page_size": 50})
    assert [item["row"].asset_id for item in normal.context["row_items"]] == [normal_asset.pk]
    assert len(normal.context["row_items"][0]["scan_history"]) == 2
    assert normal.context["summary"] == summary
    assert "q=EQ-FILTER-ONLY" in normal.context["pagination_query"]
    assert "page_size=50" in normal.context["pagination_query"]
    assert "关闭任务</a>" in normal.content.decode() and "btn-danger disabled" in normal.content.decode()
    abnormal = client.get(url, {"row_view": "exception"})
    assert [item["row"].asset_id for item in abnormal.context["row_items"]] == [exception_asset.pk]
    missing = client.get(url, {"row_view": "missing"})
    assert missing.context["page_obj"].paginator.count == 25
    resolved = client.get(url, {"row_view": "resolved"})
    assert [item["row"].asset_id for item in resolved.context["row_items"]] == [resolved_asset.pk]
    pending = client.get(url, {"row_view": "unresolved"})
    assert pending.context["page_obj"].paginator.count == 25
    assert resolved_asset.pk not in {item["row"].asset_id for item in pending.context["row_items"]}
    assert client.get(url, {"page_size": 999999}).status_code == 400
    client.force_login(make_user("result-filter-outsider", "employee"))
    assert client.get(url, {"row_view": "normal", "q": "EQ-FILTER-ONLY"}).status_code in (403, 404)


def _problem(context, key, days_ago):
    completed_date = timezone.localdate() - timedelta(days=days_ago)
    plan = create_maintenance_plan(
        actor=context["equipment"], company=context["company"], asset=context["asset"],
        name=f"{key} 检查计划", cycle_value=1, cycle_unit="month",
        responsible_employee=context["responsible"], advance_notice_days=3,
        standard_content="检查并记录异常", first_due_date=completed_date,
    )
    return _complete(
        {**context, "plan": plan}, f"{key}-complete", completed_date=completed_date,
        result="problem_found", problem_description=f"{key} 防护罩松动",
    )


def test_maintenance_followup_prioritizes_open_and_filters_without_losing_history_or_scope(client):
    context = maintenance_context("FOLLOWUP")
    context["asset"].equipment_number = "EQ-FOLLOWUP"
    context["asset"].save(update_fields=["equipment_number"])
    closed = _problem(context, "OLD-CLOSED", 3)
    close_maintenance_problem(
        actor=context["equipment"], problem=closed.problem, closure_note="已更换紧固件并复查",
        idempotency_key="followup-close",
    )
    old_open = _problem(context, "OLDER-OPEN", 2)
    new_open = _problem(context, "NEWER-OPEN", 1)
    client.force_login(context["equipment"])
    url = reverse("maintenance:problem-list")
    response = client.get(url)
    assert [item["problem"].pk for item in response.context["items"]] == [
        old_open.problem.pk, new_open.problem.pk, closed.problem.pk,
    ]
    assert response.context["problem_counts"] == {"open": 2, "closed": 1}
    filtered = client.get(url, {"q": "EQ-FOLLOWUP", "status": "closed"})
    assert [item["problem"].pk for item in filtered.context["items"]] == [closed.problem.pk]
    assert filtered.context["problem_counts"] == {"open": 2, "closed": 1}
    assert "status=closed" in filtered.context["pagination_query"]
    assert "已更换紧固件并复查" in filtered.content.decode()
    dated = client.get(url, {"date_from": old_open.completed_date.isoformat(), "date_to": old_open.completed_date.isoformat()})
    assert [item["problem"].pk for item in dated.context["items"]] == [old_open.problem.pk]
    searched = client.get(url, {"q": "紧固件"})
    assert [item["problem"].pk for item in searched.context["items"]] == [closed.problem.pk]
    assert client.get(url, {"date_from": "2026-02-30"}).status_code == 400
    assert client.get(url, {"status": "invented"}).status_code == 400
    void_maintenance_record(actor=context["equipment"], record=new_open, reason="录入错误", idempotency_key="followup-void")
    refreshed = client.get(url)
    assert refreshed.context["problem_counts"] == {"open": 1, "closed": 1}
    assert new_open.problem.description not in refreshed.content.decode()
    client.force_login(make_user("followup-unassigned", "employee"))
    denied = client.get(url, {"q": "EQ-FOLLOWUP"})
    assert not denied.context["items"]
    assert denied.context["problem_counts"] == {"open": 0, "closed": 0}


def test_closed_problem_detail_keeps_the_actual_handler_time_and_conclusion_visible(client):
    context = maintenance_context("CLOSUREDETAIL")
    record = _problem(context, "CLOSUREDETAIL-P", 1)
    closed = close_maintenance_problem(
        actor=context["equipment"], problem=record.problem, closure_note="更换防护罩螺栓，试机正常。",
        idempotency_key="closure-detail-close",
    )
    client.force_login(context["equipment"])
    page = client.get(reverse("maintenance:record-detail", args=[record.pk]))
    html = page.content.decode()
    assert "更换防护罩螺栓，试机正常。" in html
    assert timezone.localtime(closed.closed_at).strftime("%Y-%m-%d %H:%M") in html
    assert (context["equipment"].display_name or context["equipment"].username) in html
    assert reverse("maintenance:problem-close", args=[closed.pk]) not in html
    MaintenanceProblem.objects.filter(pk=closed.pk).update(closed_by=None)
    historical = client.get(reverse("maintenance:record-detail", args=[record.pk]))
    assert historical.status_code == 200
    assert "历史账号" in historical.content.decode()
    history_list = client.get(reverse("maintenance:problem-list"), {"status": "closed"})
    assert history_list.status_code == 200 and "历史账号" in history_list.content.decode()
