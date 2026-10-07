"""Presentation-only navigation from issue aggregates to their scoped detail report."""
from urllib.parse import parse_qsl

from django.http import QueryDict

from .catalog import report_url
from .permissions import can_view_report
from .schemas import SUPPLY_REPORT_REGISTRY
from .supply_forms import FILTERS_BY_REPORT, SupplyReportFilterForm


ISSUE_SUMMARIES = {"supply_department_issue", "supply_employee_issue"}
ORIGIN_FIELDS = {"origin_report", "origin_query"}
PAGE_FIELDS = {"page", "page_size", "summary_page"}


def issue_detail_links(*, actor, report_key, rows, form, query, page_number):
    if report_key not in ISSUE_SUMMARIES or not can_view_report(actor, "supply_issue_detail"):
        return [None] * len(rows)
    dimensions = ("department", "employee") if report_key == "supply_employee_issue" else ("department",)
    allowed = {name: {str(pk) for pk in form.fields[name].queryset.filter(
        pk__in={row.get("_summary_identity", {}).get(name) for row in rows} - {None}
    ).values_list("pk", flat=True)} for name in dimensions}
    employee_departments = ({str(pk): str(department) for pk, department in
        form.fields["employee"].queryset.filter(pk__in=allowed["employee"]).values_list("pk", "department_id")}
        if "employee" in dimensions else {})
    origin = query.copy()
    origin["page"] = str(page_number)
    links = []
    for row in rows:
        identity = row.get("_summary_identity", {})
        if not row.get("item_code") or any(str(identity.get(name)) not in allowed[name] for name in dimensions):
            links.append(None)
            continue
        if employee_departments and employee_departments[str(identity["employee"])] != str(identity["department"]):
            links.append(None)
            continue
        detail = QueryDict(mutable=True)
        for name in FILTERS_BY_REPORT["supply_issue_detail"]:
            if name in query:
                detail[name] = query[name]
        for name in dimensions:
            detail[name] = str(identity[name])
        detail["item_code"] = row["item_code"]
        detail["page_size"] = query.get("page_size", "50")
        detail["origin_report"] = report_key
        detail["origin_query"] = origin.urlencode()
        links.append(report_url("supply_issue_detail") + "?" + detail.urlencode() + "#report-detail-heading")
    return links


def issue_origin_context(*, actor, company, report_key, values):
    origin_key = values.get("origin_report", "")
    if report_key != "supply_issue_detail" or origin_key not in ISSUE_SUMMARIES or not can_view_report(actor, origin_key):
        return {}
    try:
        pairs = parse_qsl(values.get("origin_query", "")[:5000], max_num_fields=40)
    except ValueError:
        return {}
    allowed = FILTERS_BY_REPORT[origin_key] | PAGE_FIELDS
    query = QueryDict(mutable=True)
    for key, value in pairs:
        if key in allowed:
            query[key] = value
    filters = query.copy()
    for key in PAGE_FIELDS:
        filters.pop(key, None)
    form = SupplyReportFilterForm(filters, actor=actor, company=company, report_key=origin_key)
    if not form.is_valid():
        return {}
    for key in ("page", "summary_page"):
        value = query.get(key)
        if value and (not value.isdecimal() or len(value) > 8 or int(value) < 1):
            query.pop(key)
    if query.get("page_size") not in {"25", "50", "100"}:
        query.pop("page_size", None)
    return {"report_return_url": report_url(origin_key) + "?" + query.urlencode() + "#report-detail-heading",
        "report_return_title": SUPPLY_REPORT_REGISTRY[origin_key].title,
        "report_origin_fields": [("origin_report", origin_key), ("origin_query", query.urlencode())]}


def preserve_origin_pagination(context):
    for name in ("pagination_query", "summary_pagination_query"):
        if name in context:
            query = QueryDict(context[name], mutable=True)
            for key, value in context.get("report_origin_fields", ()):
                query[key] = value
            context[name] = query.urlencode()
