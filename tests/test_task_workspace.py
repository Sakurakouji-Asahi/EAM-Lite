from datetime import timedelta

import pytest
from django.urls import reverse

from apps.core.task_workspace import build_task_workspace
from apps.maintenance.domain import business_date
from apps.maintenance.services import create_maintenance_plan
from tests.test_sprint3_support import make_user
from tests.test_sprint9_support import maintenance_context


def test_workspace_omits_unavailable_categories_even_when_aggregates_are_nonzero():
    workspace = build_task_workspace(
        navigation={"tasks": {"can_view_offboarding": True}},
        dashboard={"pending": {"pending_finance": 9, "pending_label": 8,
                               "inventory_exceptions": 7, "maintenance_overdue": 6,
                               "offboarding_unresolved": 2}},
        lifecycle={"can_view_loans": False, "loans": {"overdue": 5},
                   "can_view_disposals": False, "disposals": {"actual": 4}},
        supply_dashboard={"low_stock_count": 3, "pending_clearance_count": 1},
    )
    assert {card["key"] for card in workspace["cards"]} == {"offboarding", "supply_clearance"}
    assert workspace["priorities"] == []
    assert workspace["active_categories"] == 2
    assert {card["count"] for card in workspace["cards"]} == {1, 2}


def test_zero_work_retains_authorized_entry_points_without_false_priority():
    workspace = build_task_workspace(
        navigation={"tasks": {"can_view_maintenance": True, "can_view_asset_inventory": True}},
        dashboard={"pending": {}}, lifecycle={"can_view_loans": True},
    )
    assert workspace["active_categories"] == 0
    assert workspace["priorities"] == []
    assert len(workspace["cards"]) == 4
    assert all(card["count"] == 0 and card["state"] == "clear" for card in workspace["cards"])


def test_overdue_priorities_use_matching_business_filters_and_keep_independent_counts():
    workspace = build_task_workspace(
        navigation={"tasks": {"can_view_maintenance": True, "can_view_asset_inventory": True},
                    "supplies": {"can_view_stock": True}},
        dashboard={"pending": {"maintenance_upcoming": 2, "maintenance_overdue": 3,
                               "inventory_exceptions": 4}},
        lifecycle={"can_view_loans": True, "loans": {"active": 8, "overdue": 1, "due": 2}},
        supply_dashboard={"low_stock_count": 5},
    )
    cards = {card["key"]: card for card in workspace["cards"]}
    assert cards["maintenance"]["count"] == 5
    assert cards["loans"]["count"] == 8
    assert workspace["active_categories"] == 4
    assert [(item["key"], item["count"]) for item in workspace["priorities"]] == [
        ("overdue_loans", 1), ("overdue_maintenance", 3), ("asset_exceptions", 4), ("low_stock", 5),
    ]
    assert workspace["priorities"][0]["url"] == reverse("assets:loan-workbench") + "?state=overdue"
    assert workspace["priorities"][1]["url"] == reverse("maintenance:due-list") + "?due_scope=overdue"
    assert workspace["priorities"][2]["url"] == reverse("inventory:task-list") + "?work=unresolved"


@pytest.mark.django_db
def test_task_center_includes_overdue_maintenance_and_matches_destination_scope(client):
    context = maintenance_context("TASKWORK")
    create_maintenance_plan(
        actor=context["equipment"], company=context["company"], asset=context["asset"],
        name="已到期保养", cycle_value=1, cycle_unit="month",
        responsible_employee=context["responsible"], advance_notice_days=3,
        standard_content="核对并维护", first_due_date=business_date() - timedelta(days=2),
    )
    client.force_login(context["equipment"])
    response = client.get(reverse("task-center"))
    assert response.status_code == 200
    workspace = response.context["task_workspace"]
    maintenance = next(card for card in workspace["cards"] if card["key"] == "maintenance")
    assert maintenance["count"] == 2
    assert "今日及即将到期 1 项 · 逾期 1 项" in response.content.decode()
    priority = next(item for item in workspace["priorities"] if item["key"] == "overdue_maintenance")
    due_page = client.get(priority["url"])
    assert due_page.status_code == 200
    assert len(due_page.context["items"]) == priority["count"] == 1
    assert due_page.context["items"][0]["due_status"] == "overdue"
    assert {"private", "no-store"} <= set(response["Cache-Control"].split(", "))

    client.force_login(make_user("task-work-unassigned", "employee"))
    other = client.get(reverse("task-center"))
    other_maintenance = next(card for card in other.context["task_workspace"]["cards"] if card["key"] == "maintenance")
    assert other_maintenance["count"] == 0
    assert not other.context["task_workspace"]["priorities"]


@pytest.mark.django_db
def test_hr_task_center_only_shows_clearance_entries(client):
    maintenance_context("TASKHR")
    client.force_login(make_user("task-work-hr", "hr"))
    response = client.get(reverse("task-center"))
    assert response.status_code == 200
    assert {card["key"] for card in response.context["task_workspace"]["cards"]} <= {"offboarding", "supply_clearance"}
    assert 'data-task-card="maintenance"' not in response.content.decode()
    assert 'data-task-priority="overdue_maintenance"' not in response.content.decode()
