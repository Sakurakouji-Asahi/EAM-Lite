"""Server-rendered Sprint 3 asset-master views."""

from __future__ import annotations

import uuid
from urllib.parse import urlencode
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import FieldDoesNotExist, PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import Q
from django.db.utils import OperationalError, ProgrammingError
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.encoding import escape_uri_path

from apps.assets.access import asset_company_for_request, asset_queryset_for_request, asset_or_404
from apps.assets.forms import (
    AssetAttachmentUploadForm,
    AssetAttachmentVoidForm,
    AssetCustomValueForm,
    AssetDeleteForm,
    AssetDraftForm,
    AssetEquipmentNumberForm,
    AssetSubmitForm,
    AssetWithdrawForm,
    RequestedCodingSchemeForm,
)
from apps.assets.models import Asset, AssetCustomField, AttachmentLink
from apps.assets.permissions import (
    ASSET_GLOBAL_WRITE_ROLES,
    can_create_attachment_link,
    can_create_asset_draft,
    can_delete_asset_draft,
    can_edit_asset_draft,
    can_edit_asset_equipment_number,
    can_set_requested_coding_scheme,
    can_submit_asset,
    can_view_asset_summary_fields,
    can_view_asset_p1,
    can_view_attachment,
    can_view_financial_fields,
    can_void_attachment_link,
    can_withdraw_asset,
    scoped_assets_p1,
)
from apps.masterdata.models import FixedAssetCategory
from apps.assets.list_filters import (FILTER_LABELS, CHOICES, describe_list_filters, filter_asset_list, normalize_list_filters, with_ledger_status)
from apps.assets.qr_forms import SingleLabelPrintForm
from apps.assets.qr_permissions import can_manage_labels
from apps.assets.lifecycle_permissions import (
    TERMINAL_STATUSES,
    can_lifecycle_action,
)
from apps.assets.services import (
    FINANCIAL_FIELD_NAMES,
    create_asset_draft,
    delete_asset_draft,
    set_requested_coding_scheme,
    submit_asset_for_finance,
    update_asset_draft,
    update_asset_equipment_number,
    upload_asset_attachment,
    void_asset_attachment,
    withdraw_asset_to_draft,
)
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.finance.permissions import can_manage_finance
from apps.finance.readiness import finance_confirmation_pending
from apps.assets.registration import create_registered_asset, register_asset
from apps.masterdata.models import (
    AssetCategory,
    Attachment,
    Department,
    Employee,
    Location,
)
from apps.masterdata.permissions import (
    resolve_department_ids,
    role_names_for,
)
from apps.reports.permissions import can_export_report


FORBIDDEN_DRAFT_POST_FIELDS = FINANCIAL_FIELD_NAMES | frozenset(
    {
        "asset_status",
        "record_status",
        "asset_code",
        "current_issued_code",
        "requested_coding_scheme",
        "tracking_mode",
    }
)

ASSET_SERVICE_ERROR_LABELS = {
    "attachments": "资产照片",
    "custom_values": "动态字段",
}


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                message = error
                if target is None:
                    label = ASSET_SERVICE_ERROR_LABELS.get(field)
                    if label is None:
                        try:
                            label = str(Asset._meta.get_field(field).verbose_name)
                        except FieldDoesNotExist:
                            label = None
                    if label:
                        message = f"{label}：{error}"
                form.add_error(target, message)
    else:
        for error in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, error)


def _audit_forbidden_fields(request, *, company, object_id=""):
    attempted = sorted(FORBIDDEN_DRAFT_POST_FIELDS.intersection(request.POST))
    if not attempted:
        return
    write_business_audit_log(
        company=company,
        user=request.user,
        action="asset_forbidden_field_attempt",
        object_type="Asset",
        object_id=object_id,
        old_data={},
        new_data={"attempted_fields": attempted},
        **request_audit_context(request),
    )
    raise PermissionDenied("资产实物表单包含无权写入字段，已拒绝并记录安全事件。")


def _tree_path(node):
    if node is None:
        return "—"
    values = []
    seen = set()
    current = node
    while current is not None and current.pk not in seen:
        seen.add(current.pk)
        values.append(current.name)
        current = current.parent
    return " / ".join(reversed(values))


def _configure_hierarchy_labels(form):
    if "category" in form.fields:
        form.fields["category"].label_from_instance = _tree_path
    if "location" in form.fields:
        form.fields["location"].label_from_instance = _tree_path
    if "responsible_employee" in form.fields:
        form.fields["responsible_employee"].label_from_instance = (
            lambda employee: f"{employee.employee_no} · {employee.name} · {employee.department}"
        )


def _form_sections(form):
    sections = [
        ("基本资料", ("asset_name", "category", "quantity", "unit", "serial_number"), False),
        ("使用信息", ("department", "responsible_employee", "location", "acquisition_date", "commissioning_date", "is_maintenance_required"), False),
        ("更多实物资料（选填）", ("brand", "model", "manufacturer", "factory_number", "equipment_number", "historical_code", "vehicle_plate", "chassis_number", "calibration_number", "description", "notes"), True),
    ]
    if form.identity_enabled:
        sections.insert(1, ("编码资料", ("management_attribute", "component_of", "coding_year", "coding_year_note"), False))
    return tuple({"title": title, "fields": [form[name] for name in names], "optional": optional,
                  "expanded": any(form[name].errors for name in names)} for title, names, optional in sections)


def _custom_value(value):
    field_type = value.custom_field.field_type
    if field_type in {AssetCustomField.FieldType.TEXT, AssetCustomField.FieldType.SELECT}:
        return value.value_text
    if field_type == AssetCustomField.FieldType.DECIMAL:
        return value.value_decimal
    if field_type == AssetCustomField.FieldType.DATE:
        return value.value_date
    if field_type == AssetCustomField.FieldType.BOOLEAN:
        return value.value_boolean
    return None


def _selected_category(request, company, *, asset=None, form=None):
    if form is not None and getattr(form, "cleaned_data", None):
        category = form.cleaned_data.get("category")
        if category is not None:
            return category
    raw_id = request.POST.get("category") if request.method == "POST" else None
    if raw_id:
        try:
            return AssetCategory.objects.get(company=company, is_active=True, pk=raw_id)
        except (AssetCategory.DoesNotExist, TypeError, ValueError, ValidationError):
            return None
    return asset.category if asset is not None else None


def _custom_value_forms(request, *, company, category, asset=None):
    if category is None:
        return []
    existing = {}
    if asset is not None:
        existing = {
            value.custom_field_id: _custom_value(value)
            for value in asset.custom_values.select_related("custom_field")
        }
    forms = []
    for custom_field in AssetCustomField.objects.filter(
        company=company, category=category, is_active=True
    ).order_by("display_order", "normalized_code"):
        kwargs = {
            "custom_field": custom_field,
            "prefix": f"custom_{custom_field.pk}",
            "initial": {"value": existing.get(custom_field.pk)},
        }
        if request.method == "POST":
            kwargs["data"] = request.POST
        forms.append(AssetCustomValueForm(**kwargs))
    return forms


def _custom_payload(custom_forms):
    return {
        str(form.custom_field.pk): form.cleaned_data.get("value")
        for form in custom_forms
    }


@login_required
def asset_list(request):
    company = asset_company_for_request()
    scoped_queryset = asset_queryset_for_request(request.user, company)
    include_archived = request.GET.get("record_status") == "archived"
    base_queryset = scoped_queryset.filter(
        record_status=(
            Asset.RecordStatus.ARCHIVED
            if include_archived
            else Asset.RecordStatus.ACTIVE
        )
    )
    roles = role_names_for(request.user)
    can_financial_filters = can_view_financial_fields(request.user)
    individual_durable_view = request.GET.get("view", "") == "individual_durable"
    p1_asset_ids = scoped_assets_p1(request.user, company).values("pk")
    list_has_p1 = not base_queryset.exclude(pk__in=p1_asset_ids).exists()
    filter_errors = []
    try:
        filters = normalize_list_filters(request.GET, actor=request.user, company=company)
        if not list_has_p1:
            for key in ("maintenance_required", "has_serial_number", "has_attachments"):
                filters[key] = ""
        queryset = filter_asset_list(
            with_ledger_status(base_queryset, company=company, actor=request.user),
            filters, actor=request.user, company=company,
        )
    except ValidationError as exc:
        filters = {key: request.GET.get(key, "") for key in FILTER_LABELS}
        filter_errors = exc.messages
        queryset = base_queryset.none()
    query = filters["q"]
    filter_query = urlencode({key: value for key, value in filters.items() if value})
    can_create = bool(roles.intersection(ASSET_GLOBAL_WRITE_ROLES)) or bool(
        "department_manager" in roles
        and resolve_department_ids(request.user, company)
    )
    category_ids = base_queryset.values("category_id")
    department_ids = base_queryset.exclude(department_id=None).values("department_id")
    employee_ids = base_queryset.exclude(responsible_employee_id=None).values(
        "responsible_employee_id"
    )
    from apps.masterdata.location_tree import LocationTree
    location_ids = base_queryset.exclude(location_id=None).values_list("location_id", flat=True)
    location_options = LocationTree(company).options(location_ids)
    page = Paginator(queryset.order_by("-created_at", "id"), 25).get_page(
        request.GET.get("page")
    )
    for item in page:
        from apps.assets.status_display import label_status_display
        item.current_label_display = label_status_display(item, item.current_label_status, item._identification_method)
    return render(
        request,
        "assets/asset_list.html",
        {
            "company": company,
            "page": page,
            "query": query,
            "filters": filters,
            "filter_errors": filter_errors,
            "filter_query": filter_query,
            "extra_filters_open": any(filters.get(key) for key in ("fixed_asset_category", "maintenance_required", "label_status", "has_serial_number", "has_attachments", "initialized_from", "initialized_to", "created_from", "created_to")),
            "label_choices": CHOICES["label_status"].items(),
            "fixed_categories": FixedAssetCategory.objects.filter(company=company) if can_financial_filters else (),
            "status_choices": CHOICES["asset_status"].items(),
            "categories": AssetCategory.objects.filter(pk__in=category_ids).order_by(
                "category_level", "normalized_code"
            ),
            "departments": Department.objects.filter(pk__in=department_ids).order_by(
                "normalized_code"
            ),
            "employees": Employee.objects.filter(pk__in=employee_ids).order_by(
                "normalized_employee_no"
            ),
            "locations": location_options,
            "list_has_p1": list_has_p1,
            "list_column_count": 10 + (2 if list_has_p1 else 0) + (1 if can_financial_filters else 0),
            "can_create": can_create,
            "can_financial_filters": can_financial_filters,
            "individual_durable_view": individual_durable_view,
            "individual_durable_hint": individual_durable_view,
            "can_open_imports": bool(
                roles.intersection(
                    {"system_admin", "finance", "equipment", "warehouse", "hr"}
                )
            ),
            "can_open_reports": can_export_report(request.user, "asset_ledger"),
            "can_open_label_queue": bool(
                roles.intersection({"finance", "equipment", "warehouse"})
            ),
        },
    )


@login_required
def asset_list_export(request):
    from django.utils.cache import add_never_cache_headers
    from apps.reports.permissions import require_export_report
    from apps.reports.queries import build_report_dataset, ReportValidationError
    from apps.reports.services import generate_report_export

    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    company = asset_company_for_request()
    require_export_report(request.user, "asset_ledger")
    source = request.POST if request.method == "POST" else request.GET
    if set(source) - set(FILTER_LABELS) - {"csrfmiddlewaretoken", "idempotency_key"}:
        from django.http import HttpResponseBadRequest
        return HttpResponseBadRequest("包含不支持的台账筛选条件。")
    errors, dataset, filters = [], None, {}
    key = source.get("idempotency_key") or uuid.uuid4().hex
    try:
        filters = normalize_list_filters(source, actor=request.user, company=company)
        payload = {"asset_list_filters": filters}
        if request.method == "POST":
            if not source.get("idempotency_key"):
                raise ValidationError("导出请求已失效，请返回预览后重试。")
            export = generate_report_export(
                actor=request.user, company=company, report_key="asset_ledger",
                filters=payload, idempotency_key=key, request=request,
            )
            return redirect("reports:export-detail", pk=export.pk)
        dataset = build_report_dataset(actor=request.user, company=company, report_key="asset_ledger", filters=payload)
    except (ValidationError, ReportValidationError) as exc:
        errors = getattr(exc, "messages", None) or exc.errors
    response = render(request, "assets/asset_export.html", {
        "filters": filters, "filter_query": urlencode({k: v for k, v in filters.items() if v}),
        "display_filters": describe_list_filters(filters, company=company),
        "dataset": dataset, "preview_rows": dataset.rows[:100] if dataset else (),
        "idempotency_key": key, "errors": errors,
    }, status=400 if errors else 200)
    add_never_cache_headers(response)
    return response


def _render_asset_form(request, *, company, asset=None):
    action = request.POST.get("asset_action", "register" if asset is None else "draft")
    registration_requested = asset is None and action == "register"
    initial = {}
    if asset is None and request.method == "GET":
        if request.GET.get("source") == "individual_durable":
            initial["management_attribute"] = "LV"
        if request.GET.get("component_of"):
            try:
                parent_id = uuid.UUID(request.GET["component_of"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise Http404("主资产标识格式无效。") from exc
            parent = get_object_or_404(scoped_assets_p1(request.user, company, Asset.objects.filter(
                record_status="active", current_issued_code__identity__subitem_number=0,
            ).select_related("current_issued_code__identity", "department", "responsible_employee", "location", "category")),
                pk=parent_id)
            initial.update({name: getattr(parent, name) for name in ("category", "department", "responsible_employee", "location", "unit")})
            initial.update({"component_of": parent, "management_attribute": parent.identity.management_attribute,
                            "coding_year": parent.identity.coding_year, "coding_year_note": parent.identity.year_note})
    form = AssetDraftForm(
        request.POST or None,
        actor=request.user,
        company=company,
        instance=asset,
        registration_requested=registration_requested,
        initial=initial,
    )
    _configure_hierarchy_labels(form)
    form_valid = form.is_valid() if request.method == "POST" else False
    category = _selected_category(
        request, company, asset=asset, form=form if form_valid else None
    )
    if request.method == "GET" and initial.get("category") is not None:
        category = initial["category"]
    custom_forms = _custom_value_forms(
        request, company=company, category=category, asset=asset
    )
    custom_valid = (
        all(custom_form.is_valid() for custom_form in custom_forms)
        if request.method == "POST"
        else False
    )
    if request.method == "POST" and form_valid and custom_valid:
        try:
            if action not in {"register", "draft"}:
                raise ValidationError("未知的资产保存动作。")
            if asset is not None and action == "register":
                raise ValidationError("请保存草稿后，从资产详情办理实物建档。")
            asset_data = {
                key: value for key, value in form.cleaned_data.items()
                if key != "idempotency_key"
            }
            if asset is None:
                create_service = create_registered_asset if registration_requested else create_asset_draft
                registration_options = (
                    {"idempotency_key": form.cleaned_data["idempotency_key"]}
                    if registration_requested else {}
                )
                saved = create_service(
                    actor=request.user,
                    company=company,
                    data=asset_data,
                    custom_values=_custom_payload(custom_forms),
                    request=request,
                    **registration_options,
                )
                messages.success(
                    request,
                    f"资产 {saved.asset_code} 已建立，可办理标签和日常管理；照片和财务资料可以后补。"
                    if registration_requested else "资产草稿已保存，可继续补充实物资料。",
                )
            else:
                saved = update_asset_draft(
                    actor=request.user,
                    asset=asset,
                    data=asset_data,
                    custom_values=_custom_payload(custom_forms),
                    request=request,
                )
                messages.success(request, "资产草稿已保存。")
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            if (
                request.POST.get("next_step") == "attachments"
                and can_create_attachment_link(request.user, saved, "A0")
            ):
                return redirect("assets:attachment-upload", pk=saved.pk)
            return redirect("assets:asset-detail", pk=saved.pk)
    elif request.method == "POST" and category is None:
        form.add_error("category", "请选择当前公司的启用实物分类。")
    return render(
        request,
        "assets/asset_form.html",
        {
            "company": company,
            "asset": asset,
            "form": form,
            "form_sections": _form_sections(form),
            "custom_value_forms": custom_forms,
            "individual_durable_hint": request.GET.get("source")
            == "individual_durable",
            "cancel_url": (
                redirect("assets:asset-detail", pk=asset.pk).url
                if asset is not None
                else redirect("assets:asset-list").url
            ),
        },
    )


@login_required
def asset_create(request):
    company = asset_company_for_request()
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST":
        _audit_forbidden_fields(request, company=company)
    return _render_asset_form(request, company=company)


@login_required
def asset_edit(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST":
        _audit_forbidden_fields(request, company=company, object_id=asset.pk)
    if not can_edit_asset_draft(request.user, asset):
        raise PermissionDenied("您没有维护此资产草稿的权限。")
    return _render_asset_form(request, company=company, asset=asset)


@login_required
def asset_equipment_number(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    if not can_edit_asset_equipment_number(request.user, asset):
        raise PermissionDenied("您没有补录或更正此资产设备编号的权限。")
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = AssetEquipmentNumberForm(
        request.POST or None,
        initial={"equipment_number": asset.equipment_number},
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_asset_equipment_number(
                actor=request.user,
                asset=asset,
                equipment_number=form.cleaned_data["equipment_number"],
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "设备编号已保存。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "assets/equipment_number_form.html",
        {"asset": asset, "form": form},
        status=400 if request.method == "POST" else 200,
    )


@login_required
def asset_detail(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    can_p1 = can_view_asset_p1(request.user, asset)
    can_summary_fields = can_view_asset_summary_fields(request.user, asset)
    can_financial = can_view_financial_fields(request.user)
    can_manage_financial = can_manage_finance(request.user)
    finance_pending = finance_confirmation_pending(asset)
    roles = role_names_for(request.user)
    current_qr = asset.qr_identities.filter(status="active").first()
    can_manage_label_actions = can_manage_labels(request.user, asset)
    archived = asset.record_status == Asset.RecordStatus.ARCHIVED
    terminal = asset.asset_status in TERMINAL_STATUSES
    active_loan = asset.loans.filter(status="active").first() if can_p1 else None
    latest_disposal = (
        asset.disposals.order_by("-created_at").first() if can_p1 else None
    )
    maintenance_plans = []
    can_create_maintenance_plan = False
    clearance_items = []
    if can_p1:
        try:
            from apps.maintenance.permissions import (
                can_manage_maintenance_plan,
                scoped_maintenance_plans,
            )

            maintenance_plans = scoped_maintenance_plans(
                request.user,
                company,
                asset.maintenance_plans.select_related("responsible_employee"),
            )
            can_create_maintenance_plan = (
                asset.is_maintenance_required
                and not terminal
                and can_manage_maintenance_plan(request.user, asset)
            )
        except (ImportError, OperationalError, ProgrammingError):
            maintenance_plans = []
    try:
        from apps.offboarding.permissions import scoped_clearance_items

        clearance_items = scoped_clearance_items(
            request.user,
            company,
            asset.clearance_items.select_related(
                "clearance__employee", "clearance__supplements_clearance"
            ),
        ).order_by("-clearance__initiated_at")
    except (ImportError, OperationalError, ProgrammingError):
        clearance_items = []
    attachment_filter = Q(pk__in=[])
    if can_p1:
        attachment_filter |= Q(security_class=AttachmentLink.SecurityClass.A0)
    if roles.intersection({"finance", "management"}):
        attachment_filter |= Q(security_class=AttachmentLink.SecurityClass.A1)
    attachment_queryset = (
        asset.attachment_links.filter(
            attachment_filter,
            status=AttachmentLink.Status.ACTIVE,
            attachment__is_available=True,
            attachment__malware_scan_status__in=(
                Attachment.MalwareScanStatus.POLICY_LIMITED,
                Attachment.MalwareScanStatus.CLEAN,
            ),
        )
        .select_related("attachment", "created_by")
        .order_by("role", "created_at")
    )
    attachment_rows = [
        {
            "link": link,
            "can_void": can_void_attachment_link(request.user, link),
        }
        for link in attachment_queryset
    ]
    custom_values = (
        asset.custom_values.select_related("custom_field").order_by(
            "custom_field__display_order", "custom_field__normalized_code"
        )
        if can_p1
        else []
    )
    return render(
        request,
        "assets/asset_detail.html",
        {
            "company": company,
            "asset": asset,
            "can_p1": can_p1,
            "can_summary_fields": can_summary_fields,
            "can_financial": can_financial,
            "can_manage_financial": can_manage_financial,
            "finance_pending": finance_pending,
            "location_path": _tree_path(asset.location),
            "category_path": _tree_path(asset.category),
            "custom_values": [
                {"field": value.custom_field, "value": _custom_value(value)}
                for value in custom_values
            ],
            "attachment_rows": attachment_rows,
            "can_edit": can_edit_asset_draft(request.user, asset),
            "can_edit_equipment_number": can_edit_asset_equipment_number(request.user, asset),
            "can_submit": can_submit_asset(request.user, asset),
            "can_withdraw": can_withdraw_asset(request.user, asset),
            "can_delete": can_delete_asset_draft(request.user, asset),
            "can_set_scheme": can_set_requested_coding_scheme(request.user, asset),
            "can_upload_a0": can_create_attachment_link(request.user, asset, "A0"),
            "can_upload_a1": can_create_attachment_link(request.user, asset, "A1"),
            "can_manage_labels": can_manage_label_actions,
            "current_qr": current_qr,
            "direct_label_print_form": (
                SingleLabelPrintForm()
                if current_qr
                and current_qr.label_status in {"ready_to_print", "printed"}
                and can_manage_label_actions
                else None
            ),
            "archived": archived,
            "can_create_component": bool(asset.identity and asset.identity.subitem_number == 0
                and not archived and asset.asset_status in {"pending_label", "in_use", "idle", "under_repair"}
                and can_create_asset_draft(request.user, company, asset.department) and can_p1),
            "identity_parent": asset.component_of if asset.component_of_id and can_view_asset_p1(request.user, asset.component_of) else None,
            "identity_components": scoped_assets_p1(request.user, company, asset.components.select_related(
                "current_issued_code__identity").order_by("created_at")) if can_p1 else [],
            "can_manage_trace": bool(can_p1 and asset.current_issued_code_id and not archived and not terminal
                and can_create_asset_draft(request.user, company, asset.department)),
            "origin_incoming": asset.origin_incoming.filter(source_asset__in=scoped_assets_p1(request.user, company))
                .select_related("source_asset", "source_issued_code", "reversal__recorded_by").order_by("recorded_at") if can_p1 else [],
            "origin_outgoing": asset.origin_outgoing.filter(target_asset__in=scoped_assets_p1(request.user, company))
                .select_related("target_asset", "target_issued_code", "reversal").order_by("recorded_at") if can_p1 else [],
            "composition": asset.composition_revisions.first() if can_p1 else None,
            "composition_history": asset.composition_revisions.defer("members").select_related("recorded_by")[:10] if can_p1 else [],
            "custody_return_record": getattr(asset, "custody_return_record", None) if can_p1 else None,
            "custody_return_history": asset.custody_returns.select_related("recorded_by", "reversal__recorded_by").order_by("-recorded_at") if can_p1 else [],
            "can_correct_trace": bool(can_p1 and not archived and can_create_asset_draft(request.user, company, asset.department)),
            "can_return_custody": bool(can_p1 and asset.current_issued_code_id and not archived
                and asset.management_attribute == "LS" and not getattr(asset, "custody_return_record", None)
                and can_create_asset_draft(request.user, company, asset.department)),
            "terminal": terminal,
            "active_loan": active_loan,
            "latest_disposal": latest_disposal,
            "maintenance_plans": maintenance_plans,
            "can_create_maintenance_plan": can_create_maintenance_plan,
            "clearance_items": clearance_items,
            "movements": (
                asset.movements.select_related(
                    "from_department", "to_department", "from_employee", "to_employee",
                    "from_location", "to_location", "operated_by",
                ).order_by("-effective_at", "-created_at")[:25]
                if can_p1
                else []
            ),
            "lifecycle_actions": {
                "transfer": not archived and can_lifecycle_action(request.user, asset, "transfer") and asset.asset_status in {"in_use", "idle"},
                "idle": not archived and can_lifecycle_action(request.user, asset, "idle") and asset.asset_status == "in_use",
                "activate": not archived and can_lifecycle_action(request.user, asset, "activate") and asset.asset_status == "idle",
                "repair_start": not archived and can_lifecycle_action(request.user, asset, "repair_start") and asset.asset_status in {"in_use", "idle"},
                "repair_complete": not archived and can_lifecycle_action(request.user, asset, "repair_complete") and asset.asset_status == "under_repair",
                "loan": not archived and can_lifecycle_action(request.user, asset, "loan") and asset.asset_status in {"in_use", "idle"},
                "loan_return": not archived and can_lifecycle_action(request.user, asset, "loan_return") and asset.asset_status == "loaned" and active_loan is not None,
                "disposal_start": not archived and can_lifecycle_action(request.user, asset, "disposal_start") and asset.asset_status in {"in_use", "idle", "under_repair"},
                "code_correction": not archived and not terminal and can_lifecycle_action(request.user, asset, "code_correction") and asset.current_issued_code_id is not None,
                "archive": not archived and terminal and can_lifecycle_action(request.user, asset, "archive"),
                "restore": archived and terminal and can_lifecycle_action(request.user, asset, "restore_visibility"),
            },
        },
    )


@login_required
def asset_submit(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    # Resolve a repeated registration before constructing a draft-only form.
    if request.method == "POST" and asset.current_issued_code_id is not None:
        try:
            register_asset(
                actor=request.user, asset=asset,
                idempotency_key=request.POST.get("idempotency_key"), request=request,
            )
        except ValidationError:
            return HttpResponseBadRequest("建档请求与已保存记录不一致，请刷新资产详情。")
        messages.info(request, "资产已建立，未重复生成编号。")
        return redirect("assets:asset-detail", pk=asset.pk)
    form = AssetSubmitForm(request.POST or None, actor=request.user, asset=asset)
    if request.method == "POST" and form.is_valid():
        try:
            registered = register_asset(
                actor=request.user, asset=asset,
                idempotency_key=form.cleaned_data["idempotency_key"], request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, f"资产 {registered.asset_code} 已建立；无需等待财务复核，照片可以后补。")
            return redirect("assets:asset-detail", pk=registered.pk)
    return render(request, "assets/action_form.html", {
        "asset": asset, "form": form, "title": "建立实物资产",
        "description": "本次建立正式编号和二维码。照片可后补；财务在资料齐备后另行确认折旧。",
        "button_label": "确认建档", "button_class": "primary",
        "show_asset_edit": can_edit_asset_draft(request.user, asset),
    })


@login_required
def asset_withdraw(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = AssetWithdrawForm(
        request.POST or None, actor=request.user, asset=asset
    )
    if request.method == "POST" and form.is_valid():
        try:
            asset = withdraw_asset_to_draft(
                actor=request.user,
                asset=asset,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "资产已撤回/退回为草稿，原因已记录。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "assets/action_form.html",
        {
            "asset": asset,
            "form": form,
            "title": "撤回或退回更正",
            "description": "此操作不会生成或释放正式编号。",
            "button_label": "确认退回草稿",
            "button_class": "warning",
        },
    )


@login_required
def asset_delete(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = AssetDeleteForm(request.POST or None, actor=request.user, asset=asset)
    if request.method == "POST" and form.is_valid():
        try:
            delete_asset_draft(
                actor=request.user,
                asset=asset,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "未提交资产草稿已删除，审计记录已保留。")
            return redirect("assets:asset-list")
    return render(
        request,
        "assets/action_form.html",
        {
            "asset": asset,
            "form": form,
            "title": "删除资产草稿",
            "description": "仅无附件和其他业务引用的未提交草稿可以删除。",
            "button_label": "确认删除草稿",
            "button_class": "danger",
        },
    )


@login_required
def requested_scheme(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = RequestedCodingSchemeForm(
        request.POST or None, actor=request.user, asset=asset
    )
    if request.method == "POST" and form.is_valid():
        try:
            asset = set_requested_coding_scheme(
                actor=request.user,
                asset=asset,
                coding_scheme=form.cleaned_data["requested_coding_scheme"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "指定编码方案版本已保存；本操作不会正式发号。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "assets/requested_scheme.html",
        {"asset": asset, "form": form},
    )


@login_required
def attachment_upload(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    initial = {}
    if request.GET.get("role") == "photo" and can_create_attachment_link(request.user, asset, "A0"):
        initial = {"role": "photo", "security_class": "A0"}
    elif request.GET.get("role") == "invoice" and can_create_attachment_link(request.user, asset, "A1"):
        initial = {"role": "invoice", "security_class": "A1"}
    form = AssetAttachmentUploadForm(
        request.POST or None,
        request.FILES or None,
        actor=request.user,
        asset=asset,
        initial=initial,
    )
    if initial.get("role") == "photo":
        form.fields["file"].widget.attrs.update({
            "accept": "image/jpeg,image/png,image/webp", "capture": "environment",
        })
    if request.method == "POST" and form.is_valid():
        try:
            upload_asset_attachment(
                actor=request.user,
                asset=asset,
                uploaded_file=form.cleaned_data["file"],
                role=form.cleaned_data["role"],
                security_class=form.cleaned_data["security_class"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "附件已安全上传并关联资产。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "assets/attachment_upload.html",
        {"asset": asset, "form": form, "photo_capture": initial.get("role") == "photo"},
    )


@login_required
def attachment_download(request, asset_pk, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, asset_pk)
    link = get_object_or_404(
        AttachmentLink.objects.select_related("attachment", "asset"),
        pk=pk,
        asset=asset,
        company=company,
    )
    if not can_view_attachment(request.user, link):
        raise PermissionDenied("您没有查看或下载此附件的权限。")
    attachment = link.attachment
    if (
        not attachment.is_available
        or attachment.malware_scan_status
        not in {
            Attachment.MalwareScanStatus.POLICY_LIMITED,
            Attachment.MalwareScanStatus.CLEAN,
        }
        or not default_storage.exists(attachment.storage_key)
    ):
        raise Http404("附件当前不可用。")
    write_business_audit_log(
        company=company,
        user=request.user,
        action="asset_attachment_download",
        object_type="AttachmentLink",
        object_id=link.pk,
        old_data={},
        new_data={
            "asset": str(asset.pk),
            "role": link.role,
            "security_class": link.security_class,
        },
        **request_audit_context(request),
    )
    filename = Path(attachment.safe_filename).name
    response = FileResponse(
        default_storage.open(attachment.storage_key, "rb"),
        as_attachment=True,
        filename=filename,
        content_type=attachment.mime_type,
    )
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + escape_uri_path(filename)
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def attachment_void(request, asset_pk, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, asset_pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    link = get_object_or_404(
        AttachmentLink.objects.select_related("attachment", "asset"),
        pk=pk,
        asset=asset,
        company=company,
    )
    if not can_void_attachment_link(request.user, link):
        raise PermissionDenied("您没有作废此附件的权限。")
    form = AssetAttachmentVoidForm(
        request.POST or None, actor=request.user, link=link
    )
    if request.method == "POST" and form.is_valid():
        try:
            void_asset_attachment(
                actor=request.user,
                link=link,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "附件已作废；文件和元数据继续保留。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "assets/action_form.html",
        {
            "asset": asset,
            "attachment_link": link,
            "form": form,
            "title": "作废附件",
            "description": "作废后默认不再显示或下载，但文件和元数据不会物理删除。",
            "button_label": "确认作废",
            "button_class": "danger",
        },
    )
