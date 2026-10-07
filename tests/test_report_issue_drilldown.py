"""Issue-aggregate drilldown retains its exact filters without widening destination scope."""
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse

from apps.masterdata.models import Company
from apps.reports.drilldown import issue_detail_links
from apps.reports.supply_forms import SupplyReportFilterForm
from django.http import QueryDict
from tests.test_sprint18_reports import report_context
from tests.test_sprint15_support import make_department, make_employee


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("report_key", ["supply_department_issue", "supply_employee_issue"])
def test_issue_aggregate_links_exact_item_and_period_and_returns_original_query(client, report_key):
    context = report_context()
    client.force_login(context["warehouse_user"])
    url = reverse("reports:supply-report-detail", args=[report_key])
    response = client.get(url, {"date_from":"2026-08-01", "date_to":"2026-08-31",
        "q":"S18", "management_mode":"consumable", "page_size":"25", "page":"7", "summary_page":"1"})
    assert response.status_code == 200
    assert response.context["dataset"].row_count == 1
    link = response.context["detail_rows"][0]["drilldown_url"]
    params = parse_qs(urlsplit(link).query, keep_blank_values=True)
    assert params["item_code"] == ["S18PAPER"]
    assert params["department"] == [str(context["department"].pk)]
    assert params["date_from"] == ["2026-08-01"]
    assert params["date_to"] == ["2026-08-31"]
    assert params["q"] == ["S18"]
    if report_key == "supply_employee_issue":
        assert params["employee"] == [str(context["employee"].pk)]
    detail = client.get(link)
    assert detail.status_code == 200
    assert detail.context["dataset"].row_count == 1
    assert detail.context["dataset"].rows[0]["document_no"] == context["paper_issue"].document_no
    assert "origin_query" not in detail.context["dataset"].filters
    assert "origin_report" not in detail.context["dataset"].filters
    back = detail.context["report_return_url"]
    back_params = parse_qs(urlsplit(back).query)
    assert back_params["q"] == ["S18"]
    assert back_params["page"] == ["1"]  # actual resolved source page, rather than invalid requested page 7
    assert back_params["page_size"] == ["25"]
    assert "origin_query=" in detail.context["pagination_query"]
    assert "origin_query=" in detail.context["reset_url"]
    export_form = detail.content.decode().split('data-report-export')[0].rsplit('<form',1)[1]
    assert 'name="origin_query"' not in export_form
    assert client.get(back).context["dataset"].row_count == 1
    assert "查看领退明细" in response.content.decode()


def test_employee_drilldown_preserves_own_scope_and_rejects_forged_origin_department(client):
    context = report_context()
    other_department = make_department(context["company"], "OTHER")
    other_employee = make_employee(context["company"], other_department, "OTHEREMP")
    client.force_login(context["employee_user"])
    response = client.get(reverse("reports:supply-report-detail", args=["supply_employee_issue"]))
    link = response.context["detail_rows"][0]["drilldown_url"]
    assert link
    detail = client.get(link)
    assert detail.status_code == 200
    assert all(row["employee"] == context["employee"].name for row in detail.context["dataset"].rows)
    assert not any(column.kind == "money" for column in detail.context["dataset"].definition.columns)
    forged = client.get(reverse("reports:supply-report-detail", args=["supply_issue_detail"]), {
        "origin_report":"supply_employee_issue",
        "origin_query":urlencode({"department":other_department.pk, "employee":other_employee.pk})})
    assert forged.status_code == 200
    assert "report_return_url" not in forged.context
    forbidden = client.get(reverse("reports:supply-report-detail", args=["supply_issue_detail"]), {
        "department":other_department.pk, "employee":other_employee.pk})
    assert forbidden.status_code == 400


def test_origin_is_fixed_route_and_historical_employee_department_conflict_has_no_broad_link(client):
    context = report_context()
    client.force_login(context["warehouse_user"])
    response = client.get(reverse("reports:supply-report-detail", args=["supply_issue_detail"]), {
        "origin_report":"supply_department_issue",
        "origin_query":urlencode({"q":"S18", "page":"1", "next":"https://evil.invalid/", "token":"private"})})
    assert response.status_code == 200
    assert response.context["report_return_url"].startswith(reverse("reports:supply-report-detail", args=["supply_department_issue"]))
    assert "evil.invalid" not in response.context["report_return_url"]
    assert "token" not in response.context["report_return_url"]
    moved_department = make_department(context["company"], "MOVED")
    context["employee"].department = moved_department
    context["employee"].save(update_fields=["department"])
    form = SupplyReportFilterForm({}, actor=context["warehouse_user"], company=context["company"], report_key="supply_employee_issue")
    assert form.is_valid()
    row = {"item_code":"S18PAPER", "_summary_identity":{"department":context["department"].pk, "employee":context["employee"].pk}}
    assert issue_detail_links(actor=context["warehouse_user"], report_key="supply_employee_issue", rows=[row],
        form=form, query=QueryDict(""), page_number=1) == [None]
