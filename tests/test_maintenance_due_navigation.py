from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.maintenance.domain import business_date
from apps.maintenance.services import create_maintenance_plan
from tests.test_sprint3_support import make_user
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def test_due_summary_links_preserve_scope_reset_page_and_show_remaining_days(client):
    context = maintenance_context("MAINTDUENAV")
    asset = context["asset"]
    asset.equipment_number = "MAINT-DUE-NAV-EQ"
    asset.save(update_fields=["equipment_number"])
    today = business_date()
    for suffix, due in (("today", today), ("overdue", today - timedelta(days=5))):
        create_maintenance_plan(
            actor=context["equipment"], company=context["company"], asset=asset,
            name=f"MAINTDUENAV {suffix}", cycle_value=1, cycle_unit="month",
            responsible_employee=context["responsible"], advance_notice_days=3,
            standard_content="检查紧固与润滑状态", first_due_date=due,
        )
    client.force_login(context["equipment"])
    url = reverse("maintenance:due-list")
    filters = {"q": "MAINT-DUE-NAV-EQ", "department": str(context["department"].pk),
               "responsible_employee": str(context["responsible"].pk), "page": 2}
    page = client.get(url, filters)
    assert page.status_code == 200
    assert page.context["counts"] == {"upcoming": 1, "due_today": 1, "overdue": 1}
    html = page.content.decode()
    assert "距到期 3 天" in html and "逾期 5 天" in html
    assert "MAINT-DUE-NAV-EQ" in html
    assert {item["due_timing"] for item in page.context["items"]} == {"距到期 3 天", "今日到期", "逾期 5 天"}
    for scope, link in page.context["due_links"].items():
        params = parse_qs(urlsplit(link).query)
        assert "page" not in params
        assert params["due_scope"] == [scope]
        for name in ("q", "department", "responsible_employee"):
            assert params[name] == [filters[name]]
        selected = client.get(url + link)
        assert selected.status_code == 200
        assert selected.context["counts"] == page.context["counts"]
        assert len(selected.context["items"]) == 1
        assert selected.context["items"][0]["due_status"] == scope


def test_due_navigation_keeps_unassigned_users_outside_plan_scope(client):
    context = maintenance_context("MAINTDUESCOPE")
    client.force_login(make_user("maintenance-due-unassigned", "employee"))
    page = client.get(reverse("maintenance:due-list"), {"q": context["asset"].asset_code})
    assert page.status_code == 200
    assert page.context["counts"] == {"upcoming": 0, "due_today": 0, "overdue": 0}
    assert not page.context["items"]
    assert context["plan"].name not in page.content.decode()
