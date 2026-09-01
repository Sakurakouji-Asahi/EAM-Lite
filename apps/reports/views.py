"""Server-rendered Sprint 11 report, export and external-reference views."""

from __future__ import annotations

import uuid
from calendar import monthrange
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.encoding import escape_uri_path
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.assets.models import Asset
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
)


REPORT_PREVIEW_LIMIT = 100
_REPORT_FILTER_KEYS = frozenset(
    {
        "report_type",
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
    result = []
    labels = {
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
                instance = _MODEL_FILTERS[key].objects.filter(
                    company=company, pk=value
                ).first()
                value = str(instance) if instance is not None else value
            result.append(
                (labels[key], "是" if value is True else "否" if value is False else value)
            )
    return result


def _dataset_context(dataset, company):
    columns = tuple(dataset.definition.columns)
    preview_rows = [
        [(column, row.get(column.key)) for column in columns]
        for row in dataset.rows[:REPORT_PREVIEW_LIMIT]
    ]
    return {
        "dataset": dataset,
        "columns": columns,
        "preview_rows": preview_rows,
        "preview_limit": REPORT_PREVIEW_LIMIT,
        "display_filters": _display_filters(dataset.filters, company),
        "generated_at": timezone.now(),
    }


def _report_center_navigation(actor):
    supply_definitions = tuple(
        definition
        for key, definition in SUPPLY_REPORT_REGISTRY.items()
        if can_view_report(actor, key)
    )
    return {
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
    unexpected = set(request.GET) - _REPORT_FILTER_KEYS
    if unexpected:
        return _no_store(HttpResponseBadRequest("包含不支持的报表筛选参数。"))
    company = _company_or_400()
    form = ReportFilterForm(
        request.GET or None,
        actor=request.user,
        company=company,
        initial={
            "report_type": (
                "offboarding_unresolved"
                if not can_view_report(request.user, "asset_ledger")
                else "asset_ledger"
            )
        },
    )
    context = {
        "form": form,
        "dataset": None,
        **_report_center_navigation(request.user),
    }
    if request.GET:
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
                context.update(_dataset_context(dataset, company))
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
    if not form.is_valid():
        return _render_sensitive(
            request, "reports/report_center.html", {"form": form, "dataset": None}, status=400
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
            request, "reports/report_center.html", {"form": form, "dataset": None}, status=400
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
        {"definitions": definitions},
    )


@never_cache
@login_required
@require_GET
def supply_report_detail(request, report_key):
    from apps.reports.supply_forms import FILTERS_BY_REPORT, SupplyReportFilterForm

    definition = _supply_definition_or_404(report_key)
    denied = _require_no_store(require_view_report, request.user, report_key)
    if denied:
        return denied
    unexpected = set(request.GET) - FILTERS_BY_REPORT[report_key] - {"page"}
    if unexpected:
        return _no_store(HttpResponseBadRequest("包含不支持的低值物品报表筛选参数。"))
    company = _company_or_400()
    bound_data = request.GET.copy() if request.GET else None
    if bound_data is None and report_key != "supply_stock_movement":
        bound_data = {}
    if bound_data is not None:
        bound_data.pop("page", None)
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
    }
    should_query = report_key != "supply_stock_movement" or bool(request.GET)
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
            page_obj = Paginator(dataset.rows, 50).get_page(request.GET.get("page"))
            columns = tuple(dataset.definition.columns)
            context.update(
                dataset=dataset,
                definition=dataset.definition,
                columns=columns,
                page_obj=page_obj,
                table_rows=tuple(
                    tuple((column, row.get(column.key)) for column in columns)
                    for row in page_obj.object_list
                ),
                can_export=can_export_report(request.user, report_key),
                export_idempotency_key=uuid.uuid4().hex,
                pagination_query=urlencode(
                    [(key, value) for key, values in request.GET.lists() if key != "page" for value in values]
                ),
            )
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
    if not form.is_valid():
        return _render_sensitive(
            request,
            "reports/supply_report.html",
            {
                "definition": SUPPLY_REPORT_REGISTRY[report_key],
                "form": form,
                "dataset": None,
                "can_export": False,
            },
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
            {
                "definition": SUPPLY_REPORT_REGISTRY[report_key],
                "form": form,
                "dataset": None,
                "can_export": False,
            },
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
    source = request.POST if request.method == "POST" else request.GET
    allowed = _TPLUS_FILTER_KEYS | (
        {"csrfmiddlewaretoken", "action"} if request.method == "POST" else set()
    )
    if set(source) - allowed:
        return _no_store(HttpResponseBadRequest("包含不支持的 T+ 筛选参数。"))
    if request.method == "POST" and request.POST.get("action") not in {
        "preview",
        "generate",
    }:
        return _no_store(HttpResponseBadRequest("T+ 页面动作无效。"))
    data = request.POST if request.method == "POST" else request.GET or None
    initial = {"idempotency_key": uuid.uuid4().hex}
    form = TplusExportForm(
        data,
        actor=request.user,
        company=company,
        initial=initial,
    )
    context = {"form": form, "dataset": None}
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
                        for row in dataset.asset_rows[:REPORT_PREVIEW_LIMIT]
                    ],
                    "entry_columns": TPLUS_ENTRY_COLUMNS,
                    "entry_preview_rows": [
                        [(column, row.get(column.key)) for column in TPLUS_ENTRY_COLUMNS]
                        for row in dataset.entry_rows[:REPORT_PREVIEW_LIMIT]
                    ],
                    "total_rows": [
                        (_TPLUS_TOTAL_LABELS[key], dataset.totals[key])
                        for key in TPLUS_TOTAL_METRICS
                    ],
                    "generated_at": timezone.now(),
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
    context["history"] = ExportLog.objects.filter(
        company=company, export_type=ExportLog.ExportType.TPLUS_RECONCILIATION
    ).select_related("requested_by")[:50]
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
            "can_download": can_download_export(request.user, export_log),
            "display_filters": {
                key: value
                for key, value in export_log.filters_json.items()
                if not key.startswith("_")
            },
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
    if set(request.GET) - {"q", "page"}:
        return _no_store(HttpResponseBadRequest("包含不支持的外部引用筛选参数。"))
    if query:
        from django.db.models import Q

        assets = assets.filter(
            Q(asset_code__icontains=query)
            | Q(asset_name__icontains=query)
            | Q(external_references__reference_value__icontains=query)
        ).distinct()
    page_obj = Paginator(assets, 50).get_page(request.GET.get("page"))
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
            "query": query,
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
    if set(request.GET) or set(request.POST) - {
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
            return _no_store(redirect("reports:external-reference-list"))
    return _render_sensitive(
        request,
        "reports/external_reference_form.html",
        {"form": form, "asset": asset, "current": current},
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
