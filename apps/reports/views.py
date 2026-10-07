"""Server-rendered Sprint 11 report, export and external-reference views."""

from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import date, timedelta
from urllib.parse import urlencode

from apps.core.return_navigation import safe_return_url
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseForbidden, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import escape_uri_path
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.assets.models import Asset, AssetExternalReference
from django.db.models import Exists, OuterRef
from apps.core.pagination import paginate_query
from apps.reports.history_forms import ExportHistoryFilterForm
from apps.reports.export_history import export_file_context, export_history_return_url
from apps.assets.permissions import scoped_assets
from apps.masterdata.models import (
    AssetCategory,
    Department,
    Employee,
    FixedAssetCategory,
)
from apps.masterdata.permissions import current_company
from apps.reports.forms import (
    ExternalReferenceForm,
    ReportFilterForm,
    TplusExportForm,
)
from apps.reports.models import ExportLog
from apps.reports.catalog import REPORT_DESCRIPTIONS, report_navigation, report_scope_note, report_url
from apps.reports.summaries import ReportSummary
from apps.reports.permissions import (
    can_download_export,
    can_export_report,
    can_manage_external_reference,
    can_view_report,
    require_export_report,
    require_manage_external_reference,
    require_tplus_export,
    require_view_export,
    require_view_external_reference,
    require_view_report,
)
from apps.reports.queries import (
    ReportValidationError,
    build_report_dataset,
    build_tplus_dataset,
)
from apps.reports.schemas import (
    REPORT_REGISTRY,
    SUPPLY_REPORT_REGISTRY,
    SUPPLY_REPORT_KEYS,
    TPLUS_ENTRY_COLUMNS,
    TPLUS_TOTAL_METRICS,
    get_report_definition,
    RETIRED_REPORT_KEYS,
    RETIRED_REPORT_MESSAGE,
)


REPORT_PAGE_SIZES = (25, 50, 100)
_PRESENTATION_KEYS = {"page", "page_size", "summary_page"}
_REPORT_FILTER_KEYS = frozenset(
    {
        "report_type",
        "q",
        "as_of_date",
        "period_start",
        "period_end",
        "department",
        "category",
        "fixed_asset_category",
        "responsible_employee",
        "asset_status",
        "accounting_treatment",
        "asset_scope",
        "label_scope",
        "maintenance_due_scope",
        "include_drafts",
        "include_disposed",
    }
)
_TPLUS_FILTER_KEYS = frozenset(
    {
        "period",
        "department",
        "category",
        "fixed_asset_category",
        "include_disposed",
        "idempotency_key",
    }
)
_TPLUS_TOTAL_LABELS = {
    "original_cost": "原值",
    "opening_accumulated_depreciation": "期初累计折旧",
    "automatic_depreciation": "本期自动折旧",
    "manual_depreciation": "本期手工折旧",
    "adjustment_net": "本期调整净额",
    "reversal_net": "本期冲销净额",
    "ending_accumulated_depreciation": "期末累计折旧",
    "impairment": "减值准备",
    "ending_book_value": "期末账面净值",
    "disposal_income": "处置收入",
}
_MODEL_FILTERS = {
    "department": Department,
    "category": AssetCategory,
    "fixed_asset_category": FixedAssetCategory,
    "responsible_employee": Employee,
}


def _company_or_400():
    company = current_company(include_inactive=True)
    if company is None:
        raise Http404("当前没有可用公司。")
    return company


def _no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _render_sensitive(request, template_name, context, *, status=200):
    return _no_store(render(request, template_name, context, status=status))


def _require_no_store(check, *args):
    try:
        check(*args)
    except PermissionDenied:
        return _no_store(HttpResponseForbidden("您没有执行此操作的权限。"))
    return None


def _filter_dict(cleaned_data):
    result = {}
    for key, value in cleaned_data.items():
        if (
            key == "report_type"
            or value in (None, "")
            or (value is False and key != "include_disposed")
        ):
            continue
        result[key] = value.pk if key in _MODEL_FILTERS else value
    return result


def _display_filters(filters, company):
    if "asset_list_filters" in filters:
        from apps.assets.list_filters import describe_list_filters
        return describe_list_filters(filters["asset_list_filters"], company=company)
    result = []
    labels = {
        "q": "关键词",
        "as_of_date": "基准日期",
        "period_start": "期间开始",
        "period_end": "期间结束",
        "department": "部门",
        "category": "实物分类",
        "fixed_asset_category": "固定资产类别",
        "responsible_employee": "责任人",
        "asset_status": "资产状态",
        "accounting_treatment": "会计认定",
        "asset_scope": "资产范围",
        "label_scope": "标签范围",
        "maintenance_due_scope": "保养到期范围",
        "include_drafts": "纳入草稿",
        "include_disposed": "纳入已处置资产",
    }
    for key, value in filters.items():
        if key in labels:
            if key in _MODEL_FILTERS:
                try:
                    instance = _MODEL_FILTERS[key].objects.filter(
                        company=company, pk=value
                    ).first()
                except (ValidationError, ValueError, TypeError):
                    # Failed export requests may retain an invalid original identifier.
                    instance = None
                value = str(instance) if instance is not None else value
            elif key in ReportFilterForm.base_fields and hasattr(ReportFilterForm.base_fields[key], "choices"):
                value = dict(ReportFilterForm.base_fields[key].choices).get(value, value)
            result.append(
                (labels[key], "是" if value is True else "否" if value is False else value)
            )
    return result


def _form_query(form):
    """Serialize the full applied form, retaining explicit blanks and false."""
    query = QueryDict(mutable=True)
    for name in form.fields:
        value = form.cleaned_data.get(name)
        if hasattr(value, "pk"):
            value = value.pk
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, bool):
            value = "true" if value else "false"
        query[name] = "" if value is None else str(value)
    return query


def _summary_detail_links(report_key, rows, form, query):
    dimensions = {
        "department_assets": ("department",),
        "employee_assets": ("department", "responsible_employee"),
        "fixed_asset_detail": ("fixed_asset_category",),
        "controlled_non_fixed_assets": ("department",),
    }.get(report_key, ())
    if not dimensions or form is None:
        return [None] * len(rows)
    allowed_ids = {
        name: {str(pk) for pk in form.fields[name].queryset.filter(
            pk__in={row.get("_filter_ids", {}).get(name) for row in rows} - {None}
        ).values_list("pk", flat=True)}
        for name in dimensions
    }
    links = []
    for row in rows:
        ids = row.get("_filter_ids", {})
        if any(str(ids.get(name)) not in allowed_ids[name] for name in dimensions):
            links.append(None)
            continue
        narrowed = query.copy()
        narrowed.pop("page", None)
        narrowed.pop("summary_page", None)
        for name in dimensions:
            narrowed[name] = str(ids[name])
        links.append("?" + narrowed.urlencode() + "#report-detail-heading")
    return links


def _dataset_context(dataset, company, request, form=None):
    from .workspace_views import preset_context
    from .source_links import page_source_links
    columns = tuple(dataset.definition.columns)
    page_size = int(request.GET.get("page_size", 50))
    page_obj = Paginator(range(dataset.row_count), page_size).get_page(request.GET.get("page"))
    start = page_obj.start_index() - 1
    end = page_obj.end_index()
    page_rows = []
    accumulator = ReportSummary(dataset.definition)
    for index, row in enumerate(dataset.rows):
        accumulator.add(row)
        if start <= index < end:
            page_rows.append(row)
    page_obj.object_list = page_rows
    preview_rows = [
        [(column, row.get(column.key)) for column in columns]
        for row in page_obj.object_list
    ]
    query = _form_query(form) if form is not None else request.GET.copy()
    query.pop("page", None)
    query["page_size"] = str(page_size)
    summary = accumulator.result()
    summary_page = Paginator(summary["rows"], 20).get_page(request.GET.get("summary_page"))
    summary_query = query.copy()
    summary_query.pop("summary_page", None)
    summary_query["page"] = str(page_obj.number)
    query["summary_page"] = str(summary_page.number)
    summary_rows = [[(column, row.get(column.key)) for column in summary["columns"]] for row in summary_page.object_list]
    summary_links = _summary_detail_links(dataset.definition.key, summary_page.object_list, form, query)
    actor = getattr(request, 'user', None)
    source_links = page_source_links(actor, company, page_rows) if actor and company else [{} for _ in page_rows]
    from .drilldown import issue_detail_links
    drilldown_links = issue_detail_links(actor=actor, report_key=dataset.definition.key, rows=page_rows,
        form=form, query=query, page_number=page_obj.number) if actor and form else [None for _ in page_rows]
    return {
        "dataset": dataset,
        **(preset_context(actor, company, dataset.definition.key, query) if actor and company else {}),
        "columns": columns,
        "preview_rows": preview_rows,
        "table_rows": preview_rows,
        "detail_rows": [{"cells": [(column,value,links.get(column.key)) for column,value in cells],
                         "asset_url":links.get("asset_code"), "drilldown_url":drilldown}
                        for cells,links,drilldown in zip(preview_rows,source_links,drilldown_links)],
        "has_drilldown_links": any(drilldown_links),
        "page_obj": page_obj,
        "page_size": page_size,
        "page_sizes": REPORT_PAGE_SIZES,
        "pagination_query": query.urlencode(),
        "summary": summary,
        "summary_page": summary_page,
        "summary_pagination_query": summary_query.urlencode(),
        "summary_rows": summary_rows,
        "summary_details": [{"cells": cells, "url": url} for cells, url in zip(summary_rows, summary_links)],
        "has_summary_links": any(summary_links),
        "display_filters": _display_filters(dataset.filters, company),
        "generated_at": timezone.now(),
    }


def _supply_display_filters(form):
    result = []
    for name, value in form.cleaned_data.items():
        if value in (None, "", False):
            continue
        field = form.fields[name]
        if hasattr(value, "pk"):
            value = str(value)
        elif hasattr(field, "choices"):
            value = dict(field.choices).get(value, value)
        elif value is True:
            value = "是"
        result.append((field.label, value))
    return result


def _export_return_url(export_log):
    key = export_log.export_type
    filters = {name: value for name, value in export_log.filters_json.items() if not name.startswith("_")}
    if isinstance(filters.get("asset_list_filters"), dict):
        return reverse("assets:asset-list") + "?" + urlencode(filters["asset_list_filters"], doseq=True)
    if key in SUPPLY_REPORT_KEYS:
        from apps.reports.supply_forms import FILTERS_BY_REPORT
        filters = {name: value for name, value in filters.items() if name in FILTERS_BY_REPORT[key]}
    else:
        allowed = _TPLUS_FILTER_KEYS - {"idempotency_key"} if key == "tplus_reconciliation" else _REPORT_FILTER_KEYS
        filters = {name: value for name, value in filters.items() if name in allowed}
    url = report_url(key)
    return url + (("&" if "?" in url else "?") + urlencode(filters) if filters else "")


def _export_filter_context(export_log, actor, company):
    key = export_log.export_type
    filters = {name: value for name, value in export_log.filters_json.items() if not name.startswith("_")}
    if isinstance(filters.get("asset_list_filters"), dict):
        return {"display_filters": dict(_display_filters(filters, company)),
                "return_report_url": _export_return_url(export_log)}
    if key in SUPPLY_REPORT_KEYS:
        from apps.reports.supply_forms import FILTERS_BY_REPORT, SupplyReportFilterForm
        filters = {name: value for name, value in filters.items() if name in FILTERS_BY_REPORT[key]}
        form = SupplyReportFilterForm(filters, actor=actor, company=company, report_key=key)
        if form.is_valid():
            display = dict(_supply_display_filters(form))
        else:
            display = {SupplyReportFilterForm.base_fields[name].label: value for name, value in filters.items()}
    elif key == "tplus_reconciliation":
        filters = {name: value for name, value in filters.items() if name in _TPLUS_FILTER_KEYS and name != "idempotency_key"}
        display = {"会计期间": filters.get("period", "")}
        display.update(_display_filters(filters, company))
    else:
        display = dict(_display_filters(filters, company))
        filters = {name: value for name, value in filters.items() if name in _REPORT_FILTER_KEYS}
    return {"display_filters": display, "return_report_url": _export_return_url(export_log)}


def _filter_layout(form, report_key):
    common = {"q", "as_of_date", "period_start", "period_end", "date_from", "date_to", "department", "warehouse"}
    if report_key == "employee_assets":
        common.add("responsible_employee")
    elif report_key in {"supply_employee_issue", "supply_custody_balance", "supply_custody_movement"}:
        common.add("employee")
    else:
        common.add("category")
    basic, advanced = [], []
    advanced_open = False
    for field in form.visible_fields():
        if field.name in common:
            basic.append(field)
        else:
            advanced.append(field)
            value = field.value()
            default = form.get_initial_for_field(field.field, field.name)
            if field.field.widget.input_type == "checkbox":
                changed = bool(value) != bool(default)
            else:
                changed = value not in (None, "") and str(value) != str(default)
            advanced_open |= bool(field.errors) or changed
    return {"basic_filters": basic, "advanced_filters": advanced, "advanced_filters_open": advanced_open}


def _report_center_navigation(actor, selected=""):
    supply_definitions = tuple(
        definition
        for key, definition in SUPPLY_REPORT_REGISTRY.items()
        if can_view_report(actor, key)
    )
    return {
        "report_groups": report_navigation(actor, selected),
        "report_description": REPORT_DESCRIPTIONS.get(selected, ""),
        "report_scope_note": report_scope_note(selected),
        "can_view_asset_reports": can_view_report(actor, "asset_ledger"),
        "can_view_financial_reports": can_view_report(actor, "fixed_asset_detail"),
        "can_view_inventory_reports": can_view_report(actor, "inventory_results"),
        "can_view_offboarding_reports": can_view_report(
            actor, "offboarding_unresolved"
        ),
        "can_view_tplus_report": can_view_report(actor, "tplus_reconciliation"),
        "supply_report_definitions": supply_definitions,
        "supply_report_count": len(supply_definitions),
    }


@never_cache
@login_required
@require_GET
def report_center(request):
    unexpected = set(request.GET) - _REPORT_FILTER_KEYS - _PRESENTATION_KEYS
    if unexpected:
        return _no_store(HttpResponseBadRequest("包含不支持的报表筛选参数。"))
    if request.GET.get("page_size", "50") not in {str(size) for size in REPORT_PAGE_SIZES}:
        return _no_store(HttpResponseBadRequest("每页条数请选择 25、50 或 100。"))
    if request.GET.get("report_type") in RETIRED_REPORT_KEYS:
        denied = _require_no_store(require_view_report, request.user, request.GET["report_type"])
        if denied:
            return denied
        filters = request.GET.copy()
        filters["report_type"] = "asset_ledger"
        messages.info(request, RETIRED_REPORT_MESSAGE)
        return _no_store(redirect(reverse("reports:report-center") + "?" + filters.urlencode()))
    company = _company_or_400()
    available = [key for key, definition in REPORT_REGISTRY.items() if not definition.tplus and can_view_report(request.user, key)]
    if not available:
        return _no_store(HttpResponseForbidden("您没有查看报表的权限。"))
    report_key = request.GET.get("report_type") or available[0]
    if report_key in REPORT_REGISTRY and not can_view_report(request.user, report_key):
        return _no_store(HttpResponseForbidden("您没有查看此报表的权限。"))
    data = request.GET.copy()
    for name in _PRESENTATION_KEYS:
        data.pop(name, None)
    data["report_type"] = report_key
    # A bare report entry has the same defaults as its reset link. Submitted
    # checkboxes retain their explicit checked/unchecked meaning.
    if set(data) <= {"report_type"}:
        from apps.reports.catalog import FILTERS_BY_REPORT
        if "include_disposed" in FILTERS_BY_REPORT.get(report_key, set()):
            data["include_disposed"] = "true"
        today = timezone.localdate()
        if "as_of_date" in FILTERS_BY_REPORT.get(report_key, set()):
            data["as_of_date"] = today.isoformat()
        if report_key == "monthly_depreciation":
            data["period_start"] = today.replace(day=1).isoformat()
            data["period_end"] = today.replace(day=monthrange(today.year, today.month)[1]).isoformat()
    form = ReportFilterForm(
        data,
        actor=request.user,
        company=company,
    )
    context = {
        "form": form,
        "dataset": None,
        "definition": REPORT_REGISTRY.get(report_key),
        "reset_url": report_url(report_key) if report_key in REPORT_REGISTRY else reverse("reports:report-center"),
        **_filter_layout(form, report_key),
        **_report_center_navigation(request.user, report_key),
    }
    if form.is_bound:
        if form.is_valid():
            report_key = form.cleaned_data["report_type"]
            denied = _require_no_store(require_view_report, request.user, report_key)
            if denied:
                return denied
            try:
                dataset = build_report_dataset(
                    actor=request.user,
                    company=company,
                    report_key=report_key,
                    filters=_filter_dict(form.cleaned_data),
                )
            except ReportValidationError as exc:
                for error in exc.errors:
                    form.add_error(None, error)
            else:
                context.update(_dataset_context(dataset, company, request, form))
                context["can_export"] = can_export_report(request.user, report_key)
                context["export_idempotency_key"] = uuid.uuid4().hex
        status = 200 if form.is_valid() else 400
        return _render_sensitive(request, "reports/report_center.html", context, status=status)
    return _render_sensitive(request, "reports/report_center.html", context)


@never_cache
@login_required
@require_POST
def report_export(request):
    company = _company_or_400()
    raw_report_key = request.POST.get("report_type", "")
    if raw_report_key not in REPORT_REGISTRY:
        return _no_store(HttpResponseBadRequest("报表类型无效。"))
    denied = _require_no_store(require_export_report, request.user, raw_report_key)
    if denied:
        return denied
    if set(request.GET) or set(request.POST) - _REPORT_FILTER_KEYS - {
        "csrfmiddlewaretoken",
        "idempotency_key",
    }:
        return _no_store(HttpResponseBadRequest("包含不支持的报表导出参数。"))
    form = ReportFilterForm(request.POST, actor=request.user, company=company)
    error_context = {"form": form, "dataset": None, "definition": REPORT_REGISTRY[raw_report_key],
                     "reset_url": report_url(raw_report_key), **_report_center_navigation(request.user, raw_report_key),
                     **_filter_layout(form, raw_report_key)}
    if not form.is_valid():
        return _render_sensitive(
            request, "reports/report_center.html", error_context, status=400
        )
    report_key = form.cleaned_data["report_type"]
    try:
        from apps.reports.services import generate_report_export

        export_log = generate_report_export(
            actor=request.user,
            company=company,
            report_key=report_key,
            filters=_filter_dict(form.cleaned_data),
            idempotency_key=request.POST.get("idempotency_key") or uuid.uuid4().hex,
            request=request,
        )
    except (ReportValidationError, ValidationError) as exc:
        errors = getattr(exc, "errors", None) or exc.messages
        for error in errors:
            form.add_error(None, error)
        return _render_sensitive(
            request, "reports/report_center.html", error_context, status=400
        )
    return _no_store(redirect("reports:export-detail", pk=export_log.pk))


def _supply_definition_or_404(report_key):
    if report_key not in SUPPLY_REPORT_KEYS:
        raise Http404("低值物品报表不存在。")
    return SUPPLY_REPORT_REGISTRY[report_key]


@never_cache
@login_required
@require_GET
def supply_report_index(request):
    definitions = tuple(
        definition
        for key, definition in SUPPLY_REPORT_REGISTRY.items()
        if can_view_report(request.user, key)
    )
    if not definitions:
        return _no_store(HttpResponseForbidden("您没有查看低值物品报表的权限。"))
    return _render_sensitive(
        request,
        "reports/supply_report_index.html",
        {"definitions": definitions, **_report_center_navigation(request.user),
         "supply_entries": [{"definition": definition, "description": REPORT_DESCRIPTIONS[definition.key]} for definition in definitions]},
    )


@never_cache
@login_required
@require_GET
def supply_report_detail(request, report_key):
    from apps.reports.supply_forms import FILTERS_BY_REPORT, SupplyReportFilterForm
    from .drilldown import ORIGIN_FIELDS, issue_origin_context, preserve_origin_pagination

    definition = _supply_definition_or_404(report_key)
    denied = _require_no_store(require_view_report, request.user, report_key)
    if denied:
        return denied
    navigation_fields = ORIGIN_FIELDS if report_key == "supply_issue_detail" else set()
    unexpected = set(request.GET) - FILTERS_BY_REPORT[report_key] - _PRESENTATION_KEYS - navigation_fields
    if unexpected:
        return _no_store(HttpResponseBadRequest("包含不支持的低值物品报表筛选参数。"))
    if request.GET.get("page_size", "50") not in {str(size) for size in REPORT_PAGE_SIZES}:
        return _no_store(HttpResponseBadRequest("每页条数请选择 25、50 或 100。"))
    company = _company_or_400()
    bound_data = request.GET.copy()
    for name in _PRESENTATION_KEYS | navigation_fields:
        bound_data.pop(name, None)
    if not bound_data:
        if report_key == "supply_stock_movement":
            today = timezone.localdate()
            bound_data["date_from"] = today.replace(day=1).isoformat()
            bound_data["date_to"] = today.replace(day=monthrange(today.year, today.month)[1]).isoformat()
    form = SupplyReportFilterForm(
        bound_data,
        actor=request.user,
        company=company,
        report_key=report_key,
    )
    context = {
        "definition": definition,
        "form": form,
        "dataset": None,
        "can_export": False,
        "reset_url": report_url(report_key),
        **_filter_layout(form, report_key),
        **_report_center_navigation(request.user, report_key),
        **issue_origin_context(actor=request.user, company=company, report_key=report_key, values=request.GET),
    }
    if context.get("report_origin_fields"):
        context["reset_url"] = report_url(report_key) + "?" + urlencode(context["report_origin_fields"])
    should_query = True
    if should_query and form.is_valid():
        try:
            dataset = build_report_dataset(
                actor=request.user,
                company=company,
                report_key=report_key,
                filters=form.as_filters(),
            )
        except ReportValidationError as exc:
            for error in exc.errors:
                form.add_error(None, error)
        else:
            context.update(_dataset_context(dataset, company, request, form))
            context.update(
                definition=dataset.definition,
                can_export=can_export_report(request.user, report_key),
                export_idempotency_key=uuid.uuid4().hex,
                display_filters=_supply_display_filters(form),
            )
            preserve_origin_pagination(context)
    status = 400 if should_query and not form.is_valid() else 200
    return _render_sensitive(
        request, "reports/supply_report.html", context, status=status
    )


@never_cache
@login_required
@require_POST
def supply_report_export(request, report_key):
    from apps.reports.services import generate_report_export
    from apps.reports.supply_forms import FILTERS_BY_REPORT, SupplyReportFilterForm

    _supply_definition_or_404(report_key)
    denied = _require_no_store(require_export_report, request.user, report_key)
    if denied:
        return denied
    unexpected = set(request.POST) - FILTERS_BY_REPORT[report_key] - {
        "csrfmiddlewaretoken",
        "idempotency_key",
    }
    if unexpected or request.GET:
        return _no_store(HttpResponseBadRequest("包含不支持的低值物品导出参数。"))
    company = _company_or_400()
    form = SupplyReportFilterForm(
        request.POST,
        actor=request.user,
        company=company,
        report_key=report_key,
    )
    error_context = {"definition": SUPPLY_REPORT_REGISTRY[report_key], "form": form,
                     "dataset": None, "can_export": False, "reset_url": report_url(report_key),
                     **_report_center_navigation(request.user, report_key), **_filter_layout(form, report_key)}
    if not form.is_valid():
        return _render_sensitive(
            request,
            "reports/supply_report.html",
            error_context,
            status=400,
        )
    try:
        export_log = generate_report_export(
            actor=request.user,
            company=company,
            report_key=report_key,
            filters=form.as_filters(),
            idempotency_key=request.POST.get("idempotency_key") or uuid.uuid4().hex,
            request=request,
        )
    except (ReportValidationError, ValidationError) as exc:
        errors = getattr(exc, "errors", None) or exc.messages
        for error in errors:
            form.add_error(None, error)
        return _render_sensitive(
            request,
            "reports/supply_report.html",
            error_context,
            status=400,
        )
    return _no_store(redirect("reports:export-detail", pk=export_log.pk))


def _period_bounds(period):
    year, month = (int(part) for part in period.split("-", 1))
    start = date(year, month, 1)
    return start, start + timedelta(days=monthrange(year, month)[1])


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def tplus_export(request):
    company = _company_or_400()
    denied = _require_no_store(require_tplus_export, request.user)
    if denied:
        return denied
    history_keys = {"history-period","history-status","history-date_from","history-date_to","history_page"}
    source = request.POST if request.method == "POST" else request.GET
    allowed = _TPLUS_FILTER_KEYS | (
        {"csrfmiddlewaretoken", "action"} if request.method == "POST" else set()
    ) | {"asset_page", "entry_page"} | history_keys
    if set(source) - allowed:
        return _no_store(HttpResponseBadRequest("包含不支持的 T+ 筛选参数。"))
    if request.method == "POST" and request.POST.get("action") not in {
        "preview",
        "generate",
    }:
        return _no_store(HttpResponseBadRequest("T+ 页面动作无效。"))
    initial = {"idempotency_key": uuid.uuid4().hex}
    data = request.POST if request.method == "POST" else request.GET.copy()
    if request.method == "GET":
        for key in history_keys:
            data.pop(key,None)
        data = data or None
    if request.method == "GET" and data is not None:
        data.setdefault("idempotency_key", initial["idempotency_key"])
    form = TplusExportForm(
        data,
        actor=request.user,
        company=company,
        initial=initial,
    )
    context = {"form": form, "dataset": None, **_report_center_navigation(request.user, "tplus_reconciliation")}
    if data and form.is_valid():
        period_start, period_end = _period_bounds(form.cleaned_data["period"])
        filters = _filter_dict(
            {
                key: value
                for key, value in form.cleaned_data.items()
                if key not in {"period", "idempotency_key"}
            }
        )
        try:
            dataset = build_tplus_dataset(
                actor=request.user,
                company=company,
                period_start=period_start,
                period_end=period_end,
                filters=filters,
            )
            asset_page = Paginator(dataset.asset_rows, 50).get_page(source.get("asset_page"))
            entry_page = Paginator(dataset.entry_rows, 50).get_page(source.get("entry_page"))
            paging_filters = {"period": form.cleaned_data["period"], "idempotency_key": form.cleaned_data["idempotency_key"], **filters}
            paging_filters.update({key:request.GET[key] for key in history_keys if key in request.GET})
            paging_query = urlencode(paging_filters)
            context.update(
                {
                    "dataset": dataset,
                    "period": form.cleaned_data["period"],
                    "asset_columns": tuple(dataset.definition.columns),
                    "asset_preview_rows": [
                        [
                            (column, row.get(column.key))
                            for column in dataset.definition.columns
                        ]
                        for row in asset_page.object_list
                    ],
                    "entry_columns": TPLUS_ENTRY_COLUMNS,
                    "entry_preview_rows": [
                        [(column, row.get(column.key)) for column in TPLUS_ENTRY_COLUMNS]
                        for row in entry_page.object_list
                    ],
                    "total_rows": [
                        (_TPLUS_TOTAL_LABELS[key], dataset.totals[key])
                        for key in TPLUS_TOTAL_METRICS
                    ],
                    "generated_at": timezone.now(),
                    "asset_page_obj": asset_page,
                    "entry_page_obj": entry_page,
                    "tplus_pagination_query": paging_query,
                }
            )
            if request.method == "POST" and request.POST.get("action") == "generate":
                from apps.reports.services import generate_tplus_export

                export_log = generate_tplus_export(
                    actor=request.user,
                    company=company,
                    period_start=period_start,
                    period_end=period_end,
                    filters=filters,
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    request=request,
                )
                return _no_store(redirect("reports:export-detail", pk=export_log.pk))
        except (ReportValidationError, ValidationError) as exc:
            for error in getattr(exc, "errors", None) or exc.messages:
                form.add_error(None, error)
    status = 400 if data and not form.is_valid() else 200
    history_form = ExportHistoryFilterForm(request.GET,prefix="history")
    history = ExportLog.objects.filter(company=company,export_type=ExportLog.ExportType.TPLUS_RECONCILIATION).select_related("requested_by")
    if history_form.is_valid():
        values = history_form.cleaned_data
        if values["period"]:
            history = history.filter(filters_json__period=values["period"].strftime("%Y-%m"))
        if values["status"]:
            history = history.filter(status=values["status"])
        if values["date_from"]:
            history = history.filter(requested_at__date__gte=values["date_from"])
        if values["date_to"]:
            history = history.filter(requested_at__date__lte=values["date_to"])
    else:
        history = history.none()
        status = 400
    history_page, history_query = paginate_query(request,history.order_by("-requested_at","pk"),parameter="history_page")
    preserved = QueryDict(context["tplus_pagination_query"], mutable=True) if context.get("dataset") is not None else request.GET.copy()
    if "asset_page_obj" in context:
        preserved["asset_page"] = str(context["asset_page_obj"].number)
        preserved["entry_page"] = str(context["entry_page_obj"].number)
    for key in history_keys:
        preserved.pop(key,None)
    context.update(history=history_page,history_page_obj=history_page,history_pagination_query=history_query,
                   history_filter_form=history_form,history_preserved=list(preserved.items()),history_reset_query=preserved.urlencode())
    return _render_sensitive(request, "reports/tplus_export.html", context, status=status)


@never_cache
@login_required
@require_GET
def export_detail(request, pk):
    company = _company_or_400()
    export_log = get_object_or_404(
        ExportLog.objects.select_related("requested_by", "output_attachment").prefetch_related("totals"),
        company=company,
        pk=pk,
    )
    denied = _require_no_store(require_view_export, request.user, export_log)
    if denied:
        return denied
    return _render_sensitive(
        request,
        "reports/export_detail.html",
        {
            "export_log": export_log,
            "definition": get_report_definition(export_log.export_type),
            **export_file_context(request.user, export_log),
            "history_return_url": export_history_return_url(request),
            **_export_filter_context(export_log, request.user, company),
            "display_totals": [{"label": _TPLUS_TOTAL_LABELS.get(total.metric_key, total.metric_key),
                                "amount": total.amount, "currency": total.currency} for total in export_log.totals.all()],
        },
    )


@never_cache
@login_required
@require_GET
def export_download(request, pk):
    company = _company_or_400()
    try:
        from apps.reports.services import get_export_for_download

        attachment = get_export_for_download(
            actor=request.user,
            company=company,
            export_id=pk,
            request=request,
        )
    except PermissionDenied:
        return _no_store(HttpResponseForbidden("您没有下载此导出文件的权限。"))
    except (ExportLog.DoesNotExist, ValidationError) as exc:
        raise Http404("导出文件不存在。") from exc
    if not attachment.is_available or not default_storage.exists(attachment.storage_key):
        raise Http404("导出文件当前不可用。")
    response = FileResponse(
        default_storage.open(attachment.storage_key, "rb"),
        content_type=attachment.mime_type,
    )
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + escape_uri_path(attachment.safe_filename)
    )
    return _no_store(response)


@never_cache
@login_required
@require_GET
def external_reference_list(request):
    company = _company_or_400()
    denied = _require_no_store(require_view_external_reference, request.user)
    if denied:
        return denied
    assets = scoped_assets(
        request.user,
        company,
        Asset.objects.select_related("category", "department").prefetch_related(
            "external_references"
        ),
    ).exclude(asset_status__in=("draft", "pending_finance")).order_by(
        "asset_code", "id"
    )
    query = request.GET.get("q", "").strip()
    reference_state = request.GET.get("reference_state", "")
    if reference_state not in {"", "missing", "mapped"}:
        return _no_store(HttpResponseBadRequest("外部编码匹配状态无效。"))
    if set(request.GET) - {"q", "page", "reference_state"}:
        return _no_store(HttpResponseBadRequest("包含不支持的外部引用筛选参数。"))
    if query:
        from django.db.models import Q

        assets = assets.filter(
            Q(asset_code__icontains=query)
            | Q(asset_name__icontains=query)
            | Q(equipment_number__icontains=query)
            | Q(pk__in=AssetExternalReference.objects.filter(external_system="TPLUS",reference_type="asset_card_code",reference_value__icontains=query).values("asset_id"))
        ).distinct()
    if reference_state:
        has_reference = Exists(AssetExternalReference.objects.filter(asset_id=OuterRef("pk"),external_system="TPLUS",reference_type="asset_card_code").exclude(reference_value=""))
        assets = assets.alias(has_tplus_reference=has_reference).filter(has_tplus_reference=reference_state=="mapped")
    page_obj, pagination_query = paginate_query(request,assets,per_page=50)
    rows = []
    for asset in page_obj.object_list:
        reference = next(
            (
                item
                for item in asset.external_references.all()
                if item.external_system == "TPLUS"
                and item.reference_type == "asset_card_code"
            ),
            None,
        )
        rows.append({"asset": asset, "reference": reference})
    page_obj.object_list = rows
    return _render_sensitive(
        request,
        "reports/external_reference_list.html",
        {
            "page_obj": page_obj,
            "query": query, "reference_state":reference_state, "pagination_query":pagination_query,
            "can_manage": can_manage_external_reference(request.user),
        },
    )


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def external_reference_edit(request, asset_pk):
    company = _company_or_400()
    denied = _require_no_store(require_manage_external_reference, request.user)
    if denied:
        return denied
    if set(request.GET) - {"return_to"} or set(request.POST) - {
        "csrfmiddlewaretoken",
        "reference_value",
        "note",
        "reason",
    }:
        return _no_store(HttpResponseBadRequest("包含不支持的外部引用参数。"))
    asset = get_object_or_404(
        scoped_assets(request.user, company).exclude(
            asset_status__in=("draft", "pending_finance")
        ),
        pk=asset_pk,
    )
    current = asset.external_references.filter(
        external_system="TPLUS", reference_type="asset_card_code"
    ).first()
    form = ExternalReferenceForm(
        request.POST or None,
        initial={
            "reference_value": getattr(current, "reference_value", ""),
            "note": getattr(current, "note", ""),
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            from apps.reports.services import create_or_correct_external_reference

            create_or_correct_external_reference(
                actor=request.user,
                asset=asset,
                reference_value=form.cleaned_data["reference_value"],
                note=form.cleaned_data["note"],
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        else:
            messages.success(request, "T+ 资产卡片编码已保存，并记录更正审计。")
            return _no_store(redirect(safe_return_url(request, reverse("reports:external-reference-list"))))
    return _render_sensitive(
        request,
        "reports/external_reference_form.html",
        {"form": form, "asset": asset, "current": current, "return_url":safe_return_url(request, reverse("reports:external-reference-list"))},
        status=400 if request.method == "POST" and form.errors else 200,
    )


__all__ = [
    "export_detail",
    "export_download",
    "external_reference_edit",
    "external_reference_list",
    "report_center",
    "report_export",
    "tplus_export",
]
