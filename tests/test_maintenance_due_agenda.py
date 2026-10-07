from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.paginator import Paginator
from django.test import RequestFactory
from django.urls import reverse

from apps.maintenance.due_agenda import agenda_context
from apps.maintenance.services import create_maintenance_plan
from tests.test_sprint3_support import make_user
from tests.test_sprint9_support import maintenance_context


@pytest.mark.django_db
def test_due_agenda_leap_day_window_and_daily_navigation_preserve_search(client, monkeypatch):
    context = maintenance_context("AGENDAEDGE")
    today = date(2028,2,29)
    monkeypatch.setattr("apps.maintenance.views.business_date", lambda:today)
    for suffix, due in (("previous", date(2028,2,28)), ("today", today),
                        ("next", date(2028,3,1)), ("outside", date(2028,3,3))):
        create_maintenance_plan(actor=context["equipment"], company=context["company"], asset=context["asset"],
            name=f"DATE-EDGE {suffix}", cycle_value=1, cycle_unit="month",
            responsible_employee=context["responsible"], advance_notice_days=2,
            standard_content="检查日期边界", first_due_date=due)
    client.force_login(context["equipment"])
    url = reverse("maintenance:due-list")
    query = {"q":"DATE-EDGE", "department":str(context["department"].pk),
        "responsible_employee":str(context["responsible"].pk), "page":"2"}
    response = client.get(url, query)
    assert response.status_code == 200
    assert response.context["due_view"] == "agenda"
    assert response.context["counts"] == {"upcoming":1, "due_today":1, "overdue":1}
    groups = response.context["agenda_groups"]
    assert [group["date"] for group in groups] == [date(2028,2,28), today, date(2028,3,1)]
    assert [group["due_status"] for group in groups] == ["overdue", "due_today", "upcoming"]
    assert [group["due_timing"] for group in groups] == ["逾期 1 天", "今日到期", "距到期 1 天"]
    assert "DATE-EDGE outside" not in response.content.decode()
    daily_link = groups[1]["day_url"]
    daily_params = parse_qs(urlsplit(daily_link).query)
    assert "page" not in daily_params
    for key in ("q", "department", "responsible_employee"):
        assert daily_params[key] == [query[key]]
    daily = client.get(url + daily_link)
    assert daily.status_code == 200
    assert daily.context["selected_due_date"] == today
    assert len(daily.context["items"]) == 1
    assert daily.context["counts"] == response.context["counts"]
    assert daily.context["agenda_today_count"] == 1
    item = daily.context["items"][0]
    assert reverse("maintenance:plan-complete", args=[item["plan"].pk]) in daily.content.decode()
    as_list = client.get(url + daily.context["due_view_links"]["list"])
    assert as_list.status_code == 200
    assert as_list.context["due_view"] == "list"
    assert as_list.context["selected_due_date"] == today
    assert len(as_list.context["items"]) == 1
    cleared = client.get(url + daily.context["agenda_all_dates_url"])
    assert cleared.status_code == 200 and len(cleared.context["items"]) == 3


@pytest.mark.django_db
def test_agenda_keeps_employee_scope_and_rejects_invalid_date_or_view(client):
    context = maintenance_context("AGENDASCOPE")
    url = reverse("maintenance:due-list")
    client.force_login(make_user("agenda-outside-responsibility", "employee"))
    response = client.get(url, {"q":context["asset"].asset_code, "view":"agenda"})
    assert response.status_code == 200
    assert not response.context["agenda_groups"]
    assert response.context["agenda_total_days"] == 0
    assert context["plan"].name not in response.content.decode()
    assert client.get(url, {"department":context["department"].pk}).status_code == 400
    client.force_login(context["equipment"])
    for params in ({"due_date":"2027-02-29"}, {"view":"calendar-invalid"}):
        invalid = client.get(url, params)
        assert invalid.status_code == 400
        assert not invalid.context["agenda_groups"]


def test_agenda_daily_count_covers_all_pages_and_date_link_resets_page():
    due = date(2026,12,31)
    rows = [{"plan":SimpleNamespace(next_maintenance_date=due), "due_status":"overdue",
        "due_label":"逾期", "due_timing":"逾期 1 天"} for _ in range(26)]
    page = Paginator(rows,25).page(2)
    request = RequestFactory().get("/maintenance/due/", {"q":"YEAR-END", "page":"2", "view":"agenda"})
    form = SimpleNamespace(is_valid=lambda:True, cleaned_data={"due_date":None, "view":"agenda"})
    result = agenda_context(request, form=form, items=rows, page=page, today=date(2027,1,1))
    assert len(result["agenda_groups"]) == 1
    group = result["agenda_groups"][0]
    assert group["count"] == 26 and group["visible_count"] == 1
    params = parse_qs(urlsplit(group["day_url"]).query)
    assert params["due_date"] == ["2026-12-31"]
    assert params["q"] == ["YEAR-END"]
    assert "page" not in params
