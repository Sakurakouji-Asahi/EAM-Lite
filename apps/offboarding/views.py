"""Server-rendered HTTP boundary for Sprint 10 employee asset clearance."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db.models import F, IntegerField, Q
from django.db.models.expressions import ExpressionWrapper
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import escape_uri_path

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.assets.lifecycle_permissions import can_lifecycle_action
from apps.assets.models import Asset, AttachmentLink
from apps.masterdata.models import Attachment, InitializationSetting
from apps.masterdata.permissions import (
    current_company,
    resolve_department_ids,
    role_names_for,
)
from apps.offboarding.forms import (
    ClearanceAttachmentUploadForm,
    ClearanceAttachmentVoidForm,
    ClearanceCompleteForm,
    ClearanceInitiateForm,
    ClearanceItemReturnForm,
    ClearanceItemTransferForm,
    ClearanceRefreshForm,
    SupplementalClearanceForm,
)
from apps.offboarding.models import (
    EmployeeAssetClearance,
    EmployeeAssetClearanceItem,
)
from apps.offboarding.permissions import (
    can_complete_clearance,
    can_create_supplemental_clearance,
    can_manage_clearance_attachment,
    can_refresh_clearance,
    can_view_clearance_attachment,
    require_complete_clearance,
    require_create_supplemental_clearance,
    require_initiate_clearance,
    require_refresh_clearance,
    require_view_clearance_attachment,
    scoped_clearance_items,
    scoped_clearances,
)


_CLEARANCE_VIEW_ROLES = frozenset(
    {
        "finance",
        "equipment",
        "department_manager",
        "employee",
        "warehouse",
        "hr",
        "management",
    }
)
_ASSET_STATUS_LABELS = dict(Asset.AssetStatus.choices)


def _company():
    company = current_company()
    if company is None or not company.is_active:
        raise Http404("尚未配置启用公司。")
    if not InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists():
        raise PermissionDenied("系统初始化尚未完成，离职资产清退入口暂不可用。")
    return company


def _clearances(user, company):
    return scoped_clearances(
        user,
        company,
        EmployeeAssetClearance.objects.select_related(
            "company",
            "employee",
            "employee__department",
            "initiated_by",
            "completed_by",
            "supplements_clearance",
        ),
    )


def _clearance(request, pk):
    return get_object_or_404(_clearances(request.user, _company()), pk=pk)


def _item(request, clearance, pk):
    return get_object_or_404(
        scoped_clearance_items(
            request.user,
            clearance.company,
            EmployeeAssetClearanceItem.objects.select_related(
                "company",
                "clearance__employee",
                "asset__department",
                "asset__responsible_employee",
                "asset__location",
                "source_loan",
                "movement",
                "disposal",
                "resolved_by",
            ),
        ),
        pk=pk,
        clearance=clearance,
    )


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
        return
    for error in getattr(exc, "messages", [str(exc)]):
        form.add_error(None, error)


def _render_action(
    request,
    *,
    form,
    title,
    cancel_url,
    description="",
    button_label="确认",
    danger=False,
    multipart=False,
):
    return render(
        request,
        "offboarding/action_form.html",
        {
            "form": form,
            "title": title,
            "cancel_url": cancel_url,
            "description": description,
            "button_label": button_label,
            "danger": danger,
            "multipart": multipart,
        },
    )


def _active_loan(item):
    if item.source_loan_id and item.source_loan.status == "active":
        return item.source_loan
    return item.asset.loans.filter(
        status="active",
        borrower_type="internal_employee",
        borrower_employee=item.clearance.employee,
    ).first()


def _location_path(location):
    names, current, seen = [], location, set()
    while current is not None:
        if current.pk in seen:
            return "位置数据异常"
        seen.add(current.pk)
        names.append(current.name)
        current = current.parent
    return " / ".join(reversed(names)) or "—"


def _visible_attachments(user, clearance, items):
    links = AttachmentLink._base_manager.filter(
        Q(clearance=clearance) | Q(clearance_item__in=items),
        company=clearance.company,
        status=AttachmentLink.Status.ACTIVE,
        attachment__is_available=True,
        attachment__malware_scan_status__in=(
            Attachment.MalwareScanStatus.POLICY_LIMITED,
            Attachment.MalwareScanStatus.CLEAN,
        ),
    ).select_related("attachment", "created_by", "clearance_item")
    rows = []
    for link in links:
        if not can_view_clearance_attachment(user, link):
            continue
        target = link.clearance or link.clearance_item
        rows.append(
            {
                "link": link,
                "target": target,
                "can_void": can_manage_clearance_attachment(
                    user, target, security_class=link.security_class
                ),
            }
        )
    return rows


@login_required
def clearance_list(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    company = _company()
    if not role_names_for(request.user).intersection(_CLEARANCE_VIEW_ROLES):
        raise PermissionDenied("您没有访问离职资产清退列表的权限。")
    clearances = _clearances(request.user, company)
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    if query:
        clearances = clearances.filter(
            Q(employee__employee_no__icontains=query)
            | Q(employee__name__icontains=query)
            | Q(employee__department__name__icontains=query)
        )
    if status in EmployeeAssetClearance.Status.values:
        clearances = clearances.filter(status=status)
    clearances = clearances.annotate(
        processed_assets=ExpressionWrapper(
            F("total_assets_snapshot") - F("unresolved_assets"),
            output_field=IntegerField(),
        )
    )
    return render(
        request,
        "offboarding/clearance_list.html",
        {
            "clearances": clearances,
            "status_choices": EmployeeAssetClearance.Status.choices,
            "filters": {"q": query, "status": status},
            "can_initiate": "hr" in role_names_for(request.user),
        },
    )


@login_required
def clearance_initiate(request):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    company = _company()
    require_initiate_clearance(request.user)
    initial = {}
    employee_id = request.GET.get("employee")
    if employee_id:
        initial["employee"] = employee_id
    form = ClearanceInitiateForm(
        request.POST or None,
        actor=request.user,
        company=company,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import initiate_clearance

        try:
            clearance = initiate_clearance(
                actor=request.user,
                employee=form.cleaned_data["employee"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                remark=form.cleaned_data["remark"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "员工已进入离职处理中，清退快照已建立。")
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    return _render_action(
        request,
        form=form,
        title="发起离职资产清退",
        description=(
            "确认后员工将立即停止接收新领用、转交和内部借用；其登录账号不会被静默停用。"
        ),
        button_label="确认发起清退",
        cancel_url=reverse("offboarding:clearance-list"),
        danger=True,
    )


@login_required
def clearance_detail(request, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    clearance = _clearance(request, pk)
    items = list(
        scoped_clearance_items(
            request.user,
            clearance.company,
            clearance.items.select_related(
                "asset__department",
                "asset__responsible_employee",
                "asset__location",
                "source_loan",
                "movement",
                "disposal",
                "resolved_by",
            ),
        )
    )
    roles = role_names_for(request.user)
    active = clearance.status in {
        EmployeeAssetClearance.Status.OPEN,
        EmployeeAssetClearance.Status.BLOCKED,
    }
    attachment_rows = _visible_attachments(request.user, clearance, items)
    attachments_by_item = {}
    clearance_attachments = []
    for row in attachment_rows:
        if row["link"].clearance_item_id:
            attachments_by_item.setdefault(row["link"].clearance_item_id, []).append(row)
        else:
            clearance_attachments.append(row)
    item_rows = []
    for item in items:
        loan = _active_loan(item)
        can_return = bool(
            active
            and item.resolution == EmployeeAssetClearanceItem.Resolution.PENDING
            and (
                (
                    loan is not None
                    and item.asset.asset_status == "loaned"
                    and can_lifecycle_action(
                        request.user, item.asset, "loan_return"
                    )
                )
                or (
                    item.source_type
                    == EmployeeAssetClearanceItem.SourceType.RESPONSIBILITY
                    and item.asset.responsible_employee_id
                    == clearance.employee_id
                    and item.asset.asset_status in {"in_use", "idle"}
                    and (
                        can_lifecycle_action(
                            request.user, item.asset, "assignment_return"
                        )
                        or "warehouse" in roles
                    )
                )
            )
        )
        can_transfer = bool(
            active
            and item.resolution == EmployeeAssetClearanceItem.Resolution.PENDING
            and item.asset.responsible_employee_id == clearance.employee_id
            and item.asset.asset_status in {"in_use", "idle"}
            and can_lifecycle_action(request.user, item.asset, "transfer")
        )
        can_dispose = bool(
            active
            and item.resolution == EmployeeAssetClearanceItem.Resolution.PENDING
            and loan is None
            and item.asset.asset_status in {"in_use", "idle", "under_repair"}
            and can_lifecycle_action(request.user, item.asset, "disposal_start")
        )
        item_rows.append(
            {
                "item": item,
                "active_loan": loan,
                "can_return": can_return,
                "can_transfer": can_transfer,
                "can_dispose": can_dispose,
                "can_upload": active
                and (
                    can_manage_clearance_attachment(
                        request.user, item, security_class="A0"
                    )
                    or can_manage_clearance_attachment(
                        request.user, item, security_class="A1"
                    )
                ),
                "attachments": attachments_by_item.get(item.pk, []),
                "original_status_label": _ASSET_STATUS_LABELS.get(
                    item.original_status, item.original_status
                ),
                "current_location_path": _location_path(item.asset.location),
            }
        )
    active_for_employee = EmployeeAssetClearance.objects.filter(
        company=clearance.company,
        employee=clearance.employee,
        status__in=("open", "blocked"),
    ).exclude(pk=clearance.pk).first()
    return render(
        request,
        "offboarding/clearance_detail.html",
        {
            "clearance": clearance,
            "item_rows": item_rows,
            "clearance_attachments": clearance_attachments,
            "is_hr": "hr" in roles,
            "can_refresh": active
            and can_refresh_clearance(request.user, clearance),
            "can_complete": active
            and can_complete_clearance(request.user, clearance),
            "can_create_supplement": clearance.status == "completed"
            and not clearance.is_supplement
            and can_create_supplemental_clearance(request.user, clearance),
            "active_for_employee": active_for_employee,
            "can_upload_clearance": active
            and (
                can_manage_clearance_attachment(
                    request.user, clearance, security_class="A0"
                )
                or can_manage_clearance_attachment(
                    request.user, clearance, security_class="A1"
                    )
                ),
            "processed_assets": (
                clearance.total_assets_snapshot - clearance.unresolved_assets
            ),
        },
    )


@login_required
def clearance_refresh(request, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    clearance = _clearance(request, pk)
    require_refresh_clearance(request.user, clearance)
    if clearance.status not in {"open", "blocked"}:
        raise PermissionDenied("只有处理中清退单可以刷新或重新核对。")
    form = ClearanceRefreshForm(
        request.POST or None, actor=request.user, clearance=clearance
    )
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import refresh_clearance

        try:
            refresh_clearance(
                actor=request.user,
                clearance=clearance,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "清退关联已重新核对；后补项目保留了发现原因与时间。")
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    return _render_action(
        request,
        form=form,
        title="刷新/重新核对清退",
        description=(
            "仅补入发起前已经生效、但发起后才发现的结构化责任或内部借用关系；"
            "不能借此向离职员工新增资产。"
        ),
        button_label="确认重新核对",
        cancel_url=reverse("offboarding:clearance-detail", args=[clearance.pk]),
    )


@login_required
def clearance_supplement(request, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    original = _clearance(request, pk)
    require_create_supplemental_clearance(request.user, original)
    if original.status != EmployeeAssetClearance.Status.COMPLETED:
        raise PermissionDenied("只能从已完成的首次清退单建立补充清退。")
    if original.is_supplement:
        raise PermissionDenied("补充清退必须直接关联已完成的首次清退单。")
    existing = EmployeeAssetClearance.objects.filter(
        company=original.company,
        employee=original.employee,
        status__in=("open", "blocked"),
    ).first()
    if existing is not None:
        messages.info(request, "该员工已有活动清退单，已打开现有记录。")
        return redirect("offboarding:clearance-detail", pk=existing.pk)
    form = SupplementalClearanceForm(
        request.POST or None,
        actor=request.user,
        original_clearance=original,
    )
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import create_supplemental_clearance

        try:
            clearance = create_supplemental_clearance(
                actor=request.user,
                original_clearance=original,
                reason=form.cleaned_data["reason"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "补充清退单已建立；原单与原离职日期保持不变。")
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    return _render_action(
        request,
        form=form,
        title="建立补充清退",
        description=(
            "补充单只处理已完成后发现的遗漏资产；不会重新打开原单，也不会改写原离职日期。"
        ),
        button_label="确认建立补充单",
        cancel_url=reverse("offboarding:clearance-detail", args=[original.pk]),
    )


@login_required
def clearance_complete(request, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    clearance = _clearance(request, pk)
    require_complete_clearance(request.user, clearance)
    if clearance.status not in {"open", "blocked"}:
        raise PermissionDenied("只有处理中清退单可以执行完成动作。")
    form = ClearanceCompleteForm(
        request.POST or None, actor=request.user, clearance=clearance
    )
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import complete_clearance

        try:
            complete_clearance(
                actor=request.user,
                clearance=clearance,
                termination_date=form.cleaned_data.get("termination_date"),
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(
                request,
                "清退已完成；记录、快照和证据将永久保留。",
            )
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    description = (
        "这是补充清退：完成后不会改写员工原离职日期。"
        if clearance.is_supplement
        else (
            f"入职日期：{clearance.employee.hire_date or '未填写'}。"
            "请明确填写实际离职日期；系统会按上海业务日校验。"
        )
    )
    return _render_action(
        request,
        form=form,
        title="完成离职资产清退",
        description=description,
        button_label="二次确认并完成清退",
        cancel_url=reverse("offboarding:clearance-detail", args=[clearance.pk]),
        danger=True,
    )


def _restrict_transfer_form(form, user, company):
    if "department_manager" not in role_names_for(user):
        return
    department_ids = resolve_department_ids(user, company)
    form.fields["to_department"].queryset = form.fields[
        "to_department"
    ].queryset.filter(pk__in=department_ids)
    form.fields["to_responsible_employee"].queryset = form.fields[
        "to_responsible_employee"
    ].queryset.filter(department_id__in=department_ids)


def _restrict_return_form(form, user, company):
    if "department_manager" not in role_names_for(user):
        return
    department_ids = resolve_department_ids(user, company)
    form.fields["return_department"].queryset = form.fields[
        "return_department"
    ].queryset.filter(pk__in=department_ids)
    form.fields["received_by_employee"].queryset = form.fields[
        "received_by_employee"
    ].queryset.filter(department_id__in=department_ids)
    form.fields["return_responsible_employee"].queryset = form.fields[
        "return_responsible_employee"
    ].queryset.filter(department_id__in=department_ids)


@login_required
def clearance_item_return(request, clearance_pk, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    clearance = _clearance(request, clearance_pk)
    item = _item(request, clearance, pk)
    active_loan = _active_loan(item)
    if (
        clearance.status not in {"open", "blocked"}
        or item.resolution != EmployeeAssetClearanceItem.Resolution.PENDING
    ):
        raise PermissionDenied("该清退项目当前不能办理归还。")
    if active_loan is not None:
        allowed = bool(
            item.asset.asset_status == "loaned"
            and can_lifecycle_action(request.user, item.asset, "loan_return")
        )
    else:
        allowed = bool(
            item.source_type
            == EmployeeAssetClearanceItem.SourceType.RESPONSIBILITY
            and item.asset.responsible_employee_id == clearance.employee_id
            and item.asset.asset_status in {"in_use", "idle"}
            and (
                can_lifecycle_action(
                    request.user, item.asset, "assignment_return"
                )
                or "warehouse" in role_names_for(request.user)
            )
        )
    if not allowed:
        raise PermissionDenied("您没有办理此清退项目归还的权限。")
    form = ClearanceItemReturnForm(
        request.POST or None, actor=request.user, item=item
    )
    _restrict_return_form(form, request.user, item.company)
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import return_clearance_item

        payload = {
            key: form.cleaned_data[key]
            for key in (
                "returned_at",
                "received_by_employee",
                "return_department",
                "return_responsible_employee",
                "return_location",
                "return_asset_status",
                "idempotency_key",
                "remark",
            )
        }
        try:
            return_clearance_item(
                actor=request.user,
                item=item,
                request=request,
                **payload,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "归还已通过正式借还流程完成，清退状态已同步。")
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    return _render_action(
        request,
        form=form,
        title=f"清退归还：{item.asset_code_snapshot}",
        description="本操作将生成正式借出归还记录和 AssetMovement；不会直接清空责任人。",
        button_label="确认归还",
        cancel_url=reverse("offboarding:clearance-detail", args=[clearance.pk]),
    )


@login_required
def clearance_item_transfer(request, clearance_pk, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    clearance = _clearance(request, clearance_pk)
    item = _item(request, clearance, pk)
    if (
        clearance.status not in {"open", "blocked"}
        or item.resolution != EmployeeAssetClearanceItem.Resolution.PENDING
        or item.asset.responsible_employee_id != clearance.employee_id
        or item.asset.asset_status not in {"in_use", "idle"}
        or not can_lifecycle_action(request.user, item.asset, "transfer")
    ):
        raise PermissionDenied("您没有办理此清退项目转交的权限。")
    form = ClearanceItemTransferForm(
        request.POST or None, actor=request.user, item=item
    )
    _restrict_transfer_form(form, request.user, item.company)
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import transfer_clearance_item

        payload = {
            key: form.cleaned_data[key]
            for key in (
                "to_department",
                "to_responsible_employee",
                "to_location",
                "effective_at",
                "reason",
                "idempotency_key",
                "remark",
            )
        }
        try:
            transfer_clearance_item(
                actor=request.user,
                item=item,
                request=request,
                **payload,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "责任转交已生成正式 AssetMovement，清退状态已同步。")
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    return _render_action(
        request,
        form=form,
        title=f"清退转交：{item.asset_code_snapshot}",
        description="目标员工必须为同公司在职启用人员；部门经理的来源与目标部门都必须在授权范围内。",
        button_label="确认转交",
        cancel_url=reverse("offboarding:clearance-detail", args=[clearance.pk]),
    )


def _attachment_target(request, clearance, target_type, target_pk):
    if target_type == "clearance":
        if str(clearance.pk) != str(target_pk):
            raise Http404("附件目标不属于该清退单。")
        return clearance
    if target_type == "item":
        return _item(request, clearance, target_pk)
    raise Http404("清退附件目标类型无效。")


@login_required
def clearance_attachment_upload(request, clearance_pk, target_type, target_pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    clearance = _clearance(request, clearance_pk)
    target = _attachment_target(request, clearance, target_type, target_pk)
    form = ClearanceAttachmentUploadForm(
        request.POST or None,
        request.FILES or None,
        actor=request.user,
        target=target,
    )
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import upload_clearance_attachment

        try:
            upload_clearance_attachment(
                actor=request.user,
                target=target,
                uploaded_file=form.cleaned_data["uploaded_file"],
                security_class=form.cleaned_data["security_class"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "清退证据已上传到私有附件存储。")
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    return _render_action(
        request,
        form=form,
        title="上传清退证据",
        description="文件不会通过公开媒体路径暴露；每次下载都会重新校验对象范围和 A0/A1 权限。",
        button_label="上传证据",
        cancel_url=reverse("offboarding:clearance-detail", args=[clearance.pk]),
        multipart=True,
    )


@login_required
def clearance_attachment_download(request, clearance_pk, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    clearance = _clearance(request, clearance_pk)
    link = get_object_or_404(
        AttachmentLink._base_manager.select_related(
            "attachment",
            "clearance",
            "clearance_item__clearance",
            "clearance_item__asset__department",
        ),
        Q(clearance=clearance) | Q(clearance_item__clearance=clearance),
        pk=pk,
        company=clearance.company,
        status=AttachmentLink.Status.ACTIVE,
        attachment__is_available=True,
        attachment__malware_scan_status__in=(
            Attachment.MalwareScanStatus.POLICY_LIMITED,
            Attachment.MalwareScanStatus.CLEAN,
        ),
    )
    require_view_clearance_attachment(request.user, link)
    attachment = link.attachment
    if not default_storage.exists(attachment.storage_key):
        raise Http404("附件文件当前不可用。")
    audit_context = request_audit_context(request)
    write_business_audit_log(
        company=clearance.company,
        user=request.user,
        action="employee_offboarding.attachment_downloaded",
        object_type="AttachmentLink",
        object_id=link.pk,
        old_data={},
        new_data={
            "clearance_id": str(clearance.pk),
            "clearance_item_id": str(link.clearance_item_id or ""),
            "security_class": link.security_class,
        },
        **audit_context,
    )
    response = FileResponse(
        default_storage.open(attachment.storage_key, "rb"),
        content_type=attachment.mime_type,
    )
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + escape_uri_path(attachment.safe_filename)
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def clearance_attachment_void(request, clearance_pk, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    clearance = _clearance(request, clearance_pk)
    link = get_object_or_404(
        AttachmentLink._base_manager.select_related(
            "attachment", "clearance", "clearance_item__clearance"
        ),
        Q(clearance=clearance) | Q(clearance_item__clearance=clearance),
        pk=pk,
        company=clearance.company,
    )
    target = link.clearance or link.clearance_item
    if not can_manage_clearance_attachment(
        request.user, target, security_class=link.security_class
    ):
        raise PermissionDenied("您没有作废此清退证据的权限。")
    form = ClearanceAttachmentVoidForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from apps.offboarding.services import void_clearance_attachment

        try:
            void_clearance_attachment(
                actor=request.user,
                link=link,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "清退证据已作废；文件元数据和审计仍永久保留。")
            return redirect("offboarding:clearance-detail", pk=clearance.pk)
    return _render_action(
        request,
        form=form,
        title="作废清退证据",
        description=(
            f"附件：{link.attachment.safe_filename}。作废不会物理删除文件或历史。"
        ),
        button_label="确认作废",
        cancel_url=reverse("offboarding:clearance-detail", args=[clearance.pk]),
        danger=True,
    )
