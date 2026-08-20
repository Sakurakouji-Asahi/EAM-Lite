"""Server-rendered Sprint 3 asset-master views."""

from __future__ import annotations

import re
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.db.utils import OperationalError, ProgrammingError
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.encoding import escape_uri_path

from apps.assets.forms import (
    AssetAttachmentUploadForm,
    AssetAttachmentVoidForm,
    AssetCustomValueForm,
    AssetDeleteForm,
    AssetDraftForm,
    AssetSubmitForm,
    AssetWithdrawForm,
    RequestedCodingSchemeForm,
)
from apps.assets.models import Asset, AssetCustomField, AttachmentLink
from apps.assets.permissions import (
    ASSET_GLOBAL_WRITE_ROLES,
    can_create_attachment_link,
    can_delete_asset_draft,
    can_edit_asset_draft,
    can_set_requested_coding_scheme,
    can_submit_asset,
    can_view_asset_summary_fields,
    can_view_asset_p1,
    can_view_attachment,
    can_view_financial_fields,
    can_void_attachment_link,
    can_withdraw_asset,
    scoped_assets,
    scoped_assets_p1,
)
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
    upload_asset_attachment,
    void_asset_attachment,
    withdraw_asset_to_draft,
)
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.models import (
    AssetCategory,
    Attachment,
    Department,
    Employee,
    InitializationSetting,
    Location,
)
from apps.masterdata.permissions import (
    current_company,
    resolve_department_ids,
    role_names_for,
)


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


def _company_and_gate():
    company = current_company()
    if company is None:
        raise Http404("尚未配置启用公司。")
    if not InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists():
        raise PermissionDenied("系统初始化尚未完成，资产建账入口暂不可用。")
    return company


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
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


def _object_queryset(user, company):
    return scoped_assets(
        user,
        company,
        Asset.objects.select_related(
            "category",
            "department",
            "responsible_employee",
            "location",
            "requested_coding_scheme",
            "created_by",
            "submitted_by",
        ),
    )


def _asset_or_404(user, company, pk):
    return get_object_or_404(_object_queryset(user, company), pk=pk)


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
    return (
        {
            "title": "基本资料",
            "fields": [
                form[name]
                for name in (
                    "asset_name",
                    "category",
                    "brand",
                    "model",
                    "manufacturer",
                    "serial_number",
                    "factory_number",
                    "historical_code",
                    "quantity",
                    "unit",
                    "description",
                    "notes",
                )
            ],
        },
        {
            "title": "使用信息",
            "fields": [
                form[name]
                for name in (
                    "department",
                    "responsible_employee",
                    "location",
                    "acquisition_date",
                    "commissioning_date",
                    "is_maintenance_required",
                )
            ],
        },
    )


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


def _safe_filter(queryset, *, field, value, model, company):
    if not value:
        return queryset
    try:
        valid = model.objects.filter(company=company, pk=value).exists()
    except (TypeError, ValueError, ValidationError):
        valid = False
    return queryset.filter(**{field: value}) if valid else queryset.none()


@login_required
def asset_list(request):
    company = _company_and_gate()
    scoped_queryset = _object_queryset(request.user, company)
    include_archived = request.GET.get("record_status") == "archived"
    base_queryset = scoped_queryset.filter(
        record_status=(
            Asset.RecordStatus.ARCHIVED
            if include_archived
            else Asset.RecordStatus.ACTIVE
        )
    )
    roles = role_names_for(request.user)
    p1_asset_ids = scoped_assets_p1(request.user, company).values("pk")
    list_has_p1 = not base_queryset.exclude(pk__in=p1_asset_ids).exists()
    queryset = base_queryset
    query = request.GET.get("q", "").strip()
    if query:
        search = (
            Q(asset_code__icontains=query)
            | Q(asset_name__icontains=query)
            | Q(responsible_employee__name__icontains=query)
        )
        if list_has_p1:
            search |= (
                Q(model__icontains=query)
                | Q(serial_number__icontains=query)
                | Q(factory_number__icontains=query)
            )
        draft_match = re.fullmatch(r"D-([0-9A-Fa-f]{1,8})", query)
        if draft_match:
            queryset = queryset.annotate(
                _draft_uuid=Cast("id", output_field=CharField())
            )
            search |= Q(_draft_uuid__istartswith=draft_match.group(1))
        queryset = queryset.filter(search)

    filters = {
        "category": request.GET.get("category", ""),
        "department": request.GET.get("department", ""),
        "employee": request.GET.get("employee", ""),
        "location": request.GET.get("location", ""),
        "asset_status": request.GET.get("asset_status", ""),
        "record_status": request.GET.get("record_status", ""),
    }
    queryset = _safe_filter(
        queryset,
        field="category_id",
        value=filters["category"],
        model=AssetCategory,
        company=company,
    )
    queryset = _safe_filter(
        queryset,
        field="department_id",
        value=filters["department"],
        model=Department,
        company=company,
    )
    queryset = _safe_filter(
        queryset,
        field="responsible_employee_id",
        value=filters["employee"],
        model=Employee,
        company=company,
    )
    queryset = _safe_filter(
        queryset,
        field="location_id",
        value=filters["location"],
        model=Location,
        company=company,
    )
    valid_statuses = {choice for choice, _ in Asset.AssetStatus.choices}
    if filters["asset_status"]:
        queryset = (
            queryset.filter(asset_status=filters["asset_status"])
            if filters["asset_status"] in valid_statuses
            else queryset.none()
        )

    can_create = bool(roles.intersection(ASSET_GLOBAL_WRITE_ROLES)) or bool(
        "department_manager" in roles
        and resolve_department_ids(request.user, company)
    )
    category_ids = base_queryset.values("category_id")
    department_ids = base_queryset.exclude(department_id=None).values("department_id")
    employee_ids = base_queryset.exclude(responsible_employee_id=None).values(
        "responsible_employee_id"
    )
    location_ids = base_queryset.exclude(location_id=None).values("location_id")
    page = Paginator(queryset.order_by("-created_at"), 25).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "assets/asset_list.html",
        {
            "company": company,
            "page": page,
            "query": query,
            "filters": filters,
            "status_choices": Asset.AssetStatus.choices,
            "categories": AssetCategory.objects.filter(pk__in=category_ids).order_by(
                "category_level", "normalized_code"
            ),
            "departments": Department.objects.filter(pk__in=department_ids).order_by(
                "normalized_code"
            ),
            "employees": Employee.objects.filter(pk__in=employee_ids).order_by(
                "normalized_employee_no"
            ),
            "locations": Location.objects.filter(pk__in=location_ids).order_by(
                "level", "normalized_code"
            ),
            "list_has_p1": list_has_p1,
            "can_create": can_create,
        },
    )


def _render_asset_form(request, *, company, asset=None):
    form = AssetDraftForm(
        request.POST or None,
        actor=request.user,
        company=company,
        instance=asset,
    )
    _configure_hierarchy_labels(form)
    form_valid = form.is_valid() if request.method == "POST" else False
    category = _selected_category(
        request, company, asset=asset, form=form if form_valid else None
    )
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
            if asset is None:
                saved = create_asset_draft(
                    actor=request.user,
                    company=company,
                    data=form.cleaned_data,
                    custom_values=_custom_payload(custom_forms),
                    request=request,
                )
                messages.success(request, "资产草稿已创建；尚未生成正式编号。")
            else:
                saved = update_asset_draft(
                    actor=request.user,
                    asset=asset,
                    data=form.cleaned_data,
                    custom_values=_custom_payload(custom_forms),
                    request=request,
                )
                messages.success(request, "资产草稿已保存。")
        except ValidationError as exc:
            _service_error(form, exc)
        else:
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
            "cancel_url": (
                redirect("assets:asset-detail", pk=asset.pk).url
                if asset is not None
                else redirect("assets:asset-list").url
            ),
        },
    )


@login_required
def asset_create(request):
    company = _company_and_gate()
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST":
        _audit_forbidden_fields(request, company=company)
    return _render_asset_form(request, company=company)


@login_required
def asset_edit(request, pk):
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST":
        _audit_forbidden_fields(request, company=company, object_id=asset.pk)
    if not can_edit_asset_draft(request.user, asset):
        raise PermissionDenied("您没有维护此资产草稿的权限。")
    return _render_asset_form(request, company=company, asset=asset)


@login_required
def asset_detail(request, pk):
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, pk)
    can_p1 = can_view_asset_p1(request.user, asset)
    can_summary_fields = can_view_asset_summary_fields(request.user, asset)
    can_financial = can_view_financial_fields(request.user)
    roles = role_names_for(request.user)
    current_qr = asset.qr_identities.filter(status="active").first()
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
            "location_path": _tree_path(asset.location),
            "category_path": _tree_path(asset.category),
            "custom_values": [
                {"field": value.custom_field, "value": _custom_value(value)}
                for value in custom_values
            ],
            "attachment_rows": attachment_rows,
            "can_edit": can_edit_asset_draft(request.user, asset),
            "can_submit": can_submit_asset(request.user, asset),
            "can_withdraw": can_withdraw_asset(request.user, asset),
            "can_delete": can_delete_asset_draft(request.user, asset),
            "can_set_scheme": can_set_requested_coding_scheme(request.user, asset),
            "can_upload_a0": can_create_attachment_link(request.user, asset, "A0"),
            "can_upload_a1": can_create_attachment_link(request.user, asset, "A1"),
            "can_manage_labels": can_manage_labels(request.user, asset),
            "current_qr": current_qr,
            "archived": archived,
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
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST" and asset.asset_status == Asset.AssetStatus.PENDING_FINANCE:
        submit_asset_for_finance(actor=request.user, asset=asset, request=request)
        messages.info(request, "该资产已经处于待财务确认状态，未重复写入记录。")
        return redirect("assets:asset-detail", pk=asset.pk)
    form = AssetSubmitForm(
        request.POST or None, actor=request.user, asset=asset
    )
    if request.method == "POST" and form.is_valid():
        try:
            asset = submit_asset_for_finance(
                actor=request.user, asset=asset, request=request
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(
                request,
                "资产已提交至待财务确认；正式编号和财务确认尚未启用。",
            )
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "assets/action_form.html",
        {
            "asset": asset,
            "form": form,
            "title": "提交财务确认",
            "description": "提交后只进入待财务确认，不生成正式编号或二维码。",
            "button_label": "确认提交",
            "button_class": "primary",
        },
    )


@login_required
def asset_withdraw(request, pk):
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, pk)
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
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, pk)
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
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, pk)
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
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, pk)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = AssetAttachmentUploadForm(
        request.POST or None,
        request.FILES or None,
        actor=request.user,
        asset=asset,
    )
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
        {"asset": asset, "form": form},
    )


@login_required
def attachment_download(request, asset_pk, pk):
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, asset_pk)
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
    company = _company_and_gate()
    asset = _asset_or_404(request.user, company, asset_pk)
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
