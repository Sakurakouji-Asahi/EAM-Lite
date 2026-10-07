"""Server-rendered, permission-scoped Sprint 8 inventory views."""

from __future__ import annotations

import uuid
from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from apps.core.multi_upload import upload_many
from apps.core.qr_csrf import build_qr_opaque_origin_bridge
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotAllowed, StreamingHttpResponse
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import escape_uri_path
from django.views.decorators.debug import sensitive_post_parameters

from apps.assets.forms import AssetDraftForm
from apps.assets.models import Asset, AssetQrIdentity, AttachmentLink
from apps.assets.permissions import scoped_assets
from apps.inventory.forms import (
    InventoryAttachmentUploadForm,
    InventoryAttachmentVoidForm,
    InventoryResolutionCorrectionForm,
    InventoryResolutionForm,
    InventoryResultFilterForm,
    InventoryScanForm,
    InventoryStopForm,
    InventorySurplusForm,
    InventorySurplusResolutionForm,
    InventoryTaskCancelForm,
    InventoryTaskCloseForm,
    InventoryTaskForm,
    InventoryTaskListFilterForm,
    SupplementalInventoryScanForm,
)
from apps.inventory.models import (
    InventoryResolution,
    InventoryScan,
    InventorySurplus,
    InventoryTask,
    InventoryTaskAssignee,
    InventoryTaskAsset,
)
from apps.inventory.result_export import inventory_result_csv
from apps.inventory.permissions import (
    can_close_inventory_task,
    can_convert_inventory_surplus,
    can_manage_inventory_attachment,
    can_publish_inventory_task,
    can_reconcile_inventory_task,
    can_scan_inventory_task,
    can_view_inventory_attachment,
    require_view_inventory_task,
    scoped_inventory_tasks,
)
from apps.inventory.services import (
    cancel_inventory_task,
    close_inventory_task,
    convert_surplus_to_asset_draft,
    correct_inventory_resolution,
    create_inventory_surplus,
    create_inventory_task_draft,
    inventory_task_summary,
    preview_inventory_assets,
    publish_inventory_task,
    resolve_inventory_difference,
    resolve_inventory_surplus,
    scan_inventory_asset,
    stop_inventory_scanning,
    supplemental_scan,
    update_inventory_task_draft,
    require_inventory_attachment_download,
)
from apps.masterdata.models import Attachment, InitializationSetting
from apps.masterdata.permissions import (
    current_company,
    resolve_department_ids,
    role_names_for,
)


FORMAL_INVENTORY_STATUSES = (
    "pending_label",
    "in_use",
    "idle",
    "loaned",
    "under_repair",
    "pending_disposal",
)
INVENTORY_VIEW_ROLES = frozenset(
    {"finance", "equipment", "department_manager", "employee", "warehouse", "management"}
)
ASSET_STATUS_LABELS = dict(Asset.AssetStatus.choices)
SCAN_CONTEXT_SESSION_PREFIX = "inventory_scan_context:"
RECENT_SCAN_SESSION_PREFIX = "inventory_recent_scan:"
QR_SCAN_BRIDGE_SALT = "eam-lite.inventory.qr-scan-bridge.v1"
QR_SCAN_BRIDGE_MAX_AGE_SECONDS = 10 * 60


def _protect_scan_response(response):
    """Keep scan secrets and device state out of caches and referrer headers."""
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _render_scan(request, template_name, context, *, status=200):
    context = {
        **context,
        "opaque_origin_bridge": build_qr_opaque_origin_bridge(
            user_id=request.user.pk,
            session_key=request.session.session_key,
            path=request.path_info,
        ),
    }
    return _protect_scan_response(
        render(request, template_name, context, status=status)
    )


def _scan_context_session_key(task):
    return f"{SCAN_CONTEXT_SESSION_PREFIX}{task.pk}"


def _scan_progress_context(task):
    detail_url = reverse("inventory:task-detail", args=[task.pk])
    return {
        "summary": inventory_task_summary(task),
        "summary_links": {
            "expected": f"{detail_url}#inventory-results",
            "scanned": f"{detail_url}?row_view=scanned#inventory-results",
            "exception": f"{detail_url}?row_view=exception#inventory-results",
            "missing": f"{detail_url}?row_view=missing#inventory-results",
        },
    }


def _scan_entry_context(request, task, **extra):
    recent_key = f"{RECENT_SCAN_SESSION_PREFIX}{task.pk}"
    recent_id = request.session.get(recent_key)
    recent_scan = None
    if recent_id:
        try:
            recent_scan = InventoryScan.objects.select_related("task_asset").filter(
                pk=recent_id, company=task.company, inventory_task=task,
                scanned_by=request.user,
            ).first()
        except (ValidationError, ValueError, TypeError):
            pass
        if recent_scan is None:
            request.session.pop(recent_key, None)
    return {
        "task": task, **_scan_progress_context(task), "recent_scan": recent_scan,
        "can_stop_scanning": can_reconcile_inventory_task(request.user, task),
        **extra,
    }


def _scan_form_context(task, row, form, *, supplemental):
    previous_scan = row.scans.filter(is_effective=True).select_related(
        "actual_location", "actual_employee", "scanned_by",
    ).first()
    return {
        "task": task, "row": row, "form": form, "supplemental": supplemental,
        **_scan_progress_context(task), "previous_scan": previous_scan,
        "previous_status_label": ASSET_STATUS_LABELS.get(
            previous_scan.actual_status, previous_scan.actual_status,
        ) if previous_scan else "",
        "expected_status_label": ASSET_STATUS_LABELS.get(
            row.expected_asset_status, row.expected_asset_status,
        ),
    }


def build_qr_scan_bridge(*, user, task, qr_identity):
    """Create a short-lived QR-to-inventory handoff without embedding the Token."""

    return signing.dumps(
        {
            "user_id": str(user.pk),
            "task_id": str(task.pk),
            "identity_id": str(qr_identity.pk),
        },
        salt=QR_SCAN_BRIDGE_SALT,
        compress=True,
    )


def _identity_and_row_from_bridge(request, task, value):
    try:
        payload = signing.loads(
            str(value or ""),
            salt=QR_SCAN_BRIDGE_SALT,
            max_age=QR_SCAN_BRIDGE_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return None, None, "盘点入口已失效，请重新扫描资产二维码。", "invalid"
    if not isinstance(payload, dict) or any(
        payload.get(key) != expected
        for key, expected in (
            ("user_id", str(request.user.pk)),
            ("task_id", str(task.pk)),
        )
    ):
        return None, None, "盘点入口已失效，请重新扫描资产二维码。", "invalid"
    identity = AssetQrIdentity.objects.select_related("asset").filter(
        pk=payload.get("identity_id"),
        company=task.company,
        status="active",
        label_status="attached",
        asset__company=task.company,
    ).first()
    if identity is None:
        return None, None, "二维码无效、已失效或标签尚未启用，本次扫码不会计入进度。", "invalid"
    row = task.task_assets.filter(asset=identity.asset).first()
    if row is None:
        return identity, None, "此二维码有效，但对应资产不在本次盘点范围内（非本任务资产）。", "non_task"
    return identity, row, None, None


def _begin_scan_context(request, task, identity):
    context_key = uuid.uuid4().hex
    request.session[_scan_context_session_key(task)] = {
        "key": context_key,
        "identity_id": str(identity.pk),
    }
    response = redirect(
        "inventory:task-scan-context",
        pk=task.pk,
        context_key=context_key,
    )
    return _protect_scan_response(response)


def _submitted_qr_token(value):
    """Accept current token-only labels and earlier URL labels."""
    from apps.assets.qr_services import token_from_scanned_payload

    return token_from_scanned_payload(value)


def _company():
    company = current_company()
    if company is None or not company.is_active:
        raise Http404("尚未配置启用公司。")
    if not InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists():
        raise PermissionDenied("系统初始化尚未完成，盘点入口暂不可用。")
    return company


def _task(request, pk):
    company = _company()
    return get_object_or_404(
        scoped_inventory_tasks(
            request.user,
            company,
            InventoryTask.objects.select_related(
                "company",
                "scope_department",
                "scope_location",
                "scope_category",
                "created_by",
                "scanning_stopped_by",
                "closed_by",
                "cancelled_by",
            ),
        ),
        pk=pk,
    )


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for error in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, error)


def _task_code(company):
    prefix = timezone.localdate().strftime("INV-%Y%m%d")
    for _ in range(8):
        value = f"{prefix}-{uuid.uuid4().hex[:8].upper()}"
        if not InventoryTask.objects.filter(company=company, task_code=value).exists():
            return value
    raise ValidationError("无法生成唯一盘点任务编号，请重试。")


def _task_form_choices(form, actor):
    roles = role_names_for(actor)
    allowed_types = []
    if "finance" in roles:
        allowed_types.extend(("department", "full", "special"))
    elif "equipment" in roles:
        allowed_types.extend(("department", "special"))
    elif "department_manager" in roles:
        allowed_types.append("department")
    labels = dict(InventoryTask.InventoryType.choices)
    form.fields["inventory_type"].choices = [
        (value, labels[value]) for value in allowed_types
    ]
    if (
        "department_manager" in roles
        and not roles.intersection({"finance", "equipment"})
    ):
        form.fields["scope_type"].choices = [("department", "部门")]


def _selected_assets(actor, company):
    return scoped_assets(
        actor,
        company,
        Asset.objects.select_related("department", "responsible_employee", "location"),
    ).filter(
        record_status="active",
        asset_status__in=FORMAL_INVENTORY_STATUSES,
        current_issued_code__isnull=False,
    ).order_by("asset_code")


def _task_form_data(request):
    if request.method != "POST":
        return None
    data = request.POST.copy()
    selected = data.getlist("selected_asset_ids_ui")
    if selected or "selected_asset_ids_ui_present" in data:
        data["selected_asset_ids"] = ",".join(selected)
    return data


def _task_initial(task, *, persisted_assignees=None):
    return {
        "name": task.name,
        "inventory_type": task.inventory_type,
        "scope_type": task.scope_type,
        "scope_department": task.scope_department_id,
        "scope_category": task.scope_category_id,
        "scope_location": task.scope_location_id,
        "selected_asset_ids": ",".join(
            task.scope_definition_json.get("selected_asset_ids", ())
        ),
        "planned_start": task.planned_start,
        "planned_end": task.planned_end,
        "assignees": (list(task.assignees.values_list("user_id", flat=True)) if persisted_assignees is None
                      else [row.user_id for row in persisted_assignees]),
        "remark": task.remark,
    }


@login_required
def task_list(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    company = _company()
    roles = role_names_for(request.user)
    if not roles.intersection(INVENTORY_VIEW_ROLES):
        raise PermissionDenied("您没有访问盘点管理的权限。")
    queryset = scoped_inventory_tasks(
        request.user,
        company,
        InventoryTask.objects.select_related(
            "scope_department", "scope_category", "scope_location", "created_by"
        ),
    )
    assigned = InventoryTaskAssignee.objects.filter(
        inventory_task=OuterRef("pk"), company=company, user=request.user,
    )
    queryset = queryset.annotate(is_assigned_to_me=Exists(assigned))
    form = InventoryTaskListFilterForm(request.GET)
    valid = form.is_valid()
    query, status, work, relation = "", "", "", ""
    if valid:
        query, status, work, relation = (form.cleaned_data[name] for name in ("q", "status", "work", "relation"))
        if query:
            queryset = queryset.filter(Q(task_code__icontains=query) | Q(name__icontains=query))
        if relation == "assigned":
            queryset = queryset.filter(is_assigned_to_me=True)
        elif relation == "created":
            queryset = queryset.filter(created_by=request.user)
        if work == "unscanned":
            pending = InventoryTaskAsset.objects.filter(inventory_task=OuterRef("pk"), inventory_status="pending")
            queryset = queryset.filter(status="in_progress").filter(Exists(pending))
        elif work == "unresolved":
            pending = InventoryTaskAsset.objects.filter(
                inventory_task=OuterRef("pk"), inventory_status__in=("exception", "missing"),
            )
            surplus = InventorySurplus.objects.filter(inventory_task=OuterRef("pk"), resolution_status="pending")
            queryset = queryset.filter(status="reconciliation").filter(Exists(pending) | Exists(surplus))
    else:
        queryset = queryset.none()
    counts = queryset.aggregate(**{
        value: Count("pk", filter=Q(status=value), distinct=True)
        for value in InventoryTask.Status.values
    })
    counts[""] = sum(counts.values())
    cards = []
    for value, label in (("", "全部"), *InventoryTask.Status.choices):
        params = request.GET.copy()
        params.pop("page", None)
        params["status"] = value
        cards.append({"value":value, "label":label, "count":counts[value], "url":"?" + params.urlencode(), "active":status == value})
    if status:
        queryset = queryset.filter(status=status)
    page_obj = Paginator(queryset.order_by("-created_at", "pk"), 25).get_page(
        request.GET.get("page")
    )
    for task in page_obj:
        task.can_scan_from_list = can_scan_inventory_task(request.user, task)
    active_filters = []
    if valid:
        for name in ("q", "status", "relation", "work"):
            value = form.cleaned_data[name]
            if not value:
                continue
            params = request.GET.copy()
            params.pop("page", None)
            params.pop(name, None)
            label = value if name == "q" else dict(form.fields[name].choices)[value]
            active_filters.append({"label":form.fields[name].label, "value":label, "url":"?" + params.urlencode()})
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    can_create = bool(roles.intersection({"finance", "equipment"})) or bool(
        "department_manager" in roles
        and resolve_department_ids(request.user, company)
    )
    return render(
        request,
        "inventory/task_list.html",
        {
            "tasks": page_obj,
            "page_obj": page_obj,
            "pagination_query": pagination_params.urlencode(),
            "status": status, "work":work, "relation":relation,
            "query": query,
            "status_choices": InventoryTask.Status.choices,
            "can_create": can_create,
            "filter_form": form, "status_cards":cards, "active_filters":active_filters,
        },
        status=200 if valid else 400,
    )


def _render_task_form(request, *, task=None):
    company = _company()
    data = _task_form_data(request)
    initial = _task_initial(task) if task is not None and request.method != "GET" else None
    if task is not None and request.method == "GET":
        from .models import InventoryTaskAssignee
        from .draft_revision import inventory_edit_revision_token
        assignees = list(InventoryTaskAssignee.objects.filter(inventory_task_id=task.pk).order_by("pk"))
        initial = _task_initial(task, persisted_assignees=assignees)
        initial["expected_revision"] = inventory_edit_revision_token(
            actor=request.user, task=task, persisted_assignees=assignees)
    form = InventoryTaskForm(
        data,
        actor=request.user,
        company=company,
        initial=initial,
        task=task,
    )
    _task_form_choices(form, request.user)
    draft_token = ""
    if task is None:
        if request.method == "POST":
            try:
                draft_identity = signing.loads(
                    request.POST.get("_draft_token", ""),
                    salt="inventory-task-draft",
                    max_age=86400,
                )
            except signing.BadSignature as exc:
                raise PermissionDenied("盘点任务草稿令牌无效或已过期，请重新打开新建页。") from exc
            create_key = draft_identity["idempotency_key"]
            task_code = draft_identity["task_code"]
            draft_token = request.POST["_draft_token"]
        else:
            create_key = uuid.uuid4().hex
            task_code = _task_code(company)
            draft_token = signing.dumps(
                {"idempotency_key": create_key, "task_code": task_code},
                salt="inventory-task-draft",
            )
    else:
        create_key = task.idempotency_key
        task_code = task.task_code
    if request.method == "POST" and form.is_valid():
        payload = dict(form.cleaned_data)
        expected_revision = payload.pop("expected_revision", None)
        payload["idempotency_key"] = create_key
        payload["task_code"] = task.task_code if task else task_code
        try:
            if task is None:
                saved = create_inventory_task_draft(
                    actor=request.user,
                    company=company,
                    data=payload,
                    assignee_users=form.cleaned_data["assignees"],
                    request=request,
                )
                messages.success(request, "盘点任务草稿已创建；尚未生成应盘快照。")
            else:
                saved = update_inventory_task_draft(
                    actor=request.user,
                    task=task,
                    data=payload,
                    assignee_users=form.cleaned_data["assignees"],
                    expected_revision=expected_revision,
                    request=request,
                )
                messages.success(request, "盘点任务草稿已保存。")
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, PermissionDenied):
                raise
            _service_error(form, exc)
        else:
            return redirect("inventory:task-detail", pk=saved.pk)
    selected_ids = set(
        str(value)
        for value in (
            form.data.get("selected_asset_ids", "").split(",")
            if form.is_bound
            else (task.scope_definition_json.get("selected_asset_ids", ()) if task else ())
        )
        if value
    )
    return render(
        request,
        "inventory/task_form.html",
        {
            "form": form,
            "task": task,
            "task_code": task.task_code if task else task_code,
            "idempotency_key": create_key,
            "draft_token": draft_token,
            "available_assets": _selected_assets(request.user, company),
            "selected_ids": selected_ids,
        },
    )


@login_required
def task_create(request):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    company = _company()
    roles = role_names_for(request.user)
    if not (
        roles.intersection({"finance", "equipment"})
        or (
            "department_manager" in roles
            and resolve_department_ids(request.user, company)
        )
    ):
        raise PermissionDenied("您没有创建盘点任务的权限。")
    return _render_task_form(request)


@login_required
def task_edit(request, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    task = _task(request, pk)
    if not can_publish_inventory_task(request.user, task):
        raise PermissionDenied("您没有编辑此盘点任务草稿的权限。")
    return _render_task_form(request, task=task)


def _attachment_rows(user, task):
    links = AttachmentLink._base_manager.filter(
        company=task.company,
    ).filter(
        Q(inventory_surplus__inventory_task=task)
        | Q(inventory_scan__inventory_task=task)
        | Q(inventory_resolution__inventory_task_asset__inventory_task=task)
    ).select_related(
        "attachment",
        "created_by",
        "inventory_surplus",
        "inventory_scan",
        "inventory_resolution",
    ).order_by("created_at")
    return [
        {
            "link": link,
            "can_void": can_manage_inventory_attachment(
                user,
                link.inventory_surplus
                or link.inventory_scan
                or link.inventory_resolution,
            ),
        }
        for link in links
        if can_view_inventory_attachment(user, link)
    ]


def _publication_preview(request, task):
    if not can_publish_inventory_task(request.user, task):
        return {}
    try:
        assets = preview_inventory_assets(actor=request.user, task=task)
        page = Paginator(assets, 25).get_page(request.GET.get("preview_page"))
        from apps.assets.form_options import location_path
        for asset in page:
            asset.preview_location_path = location_path(asset.location)
        return {"preview_page": page}
    except ValidationError as exc:
        return {"preview_error": "；".join(exc.messages)}


def _filtered_result_rows(task, form):
    """Use the same effective evidence and filters for the screen and download."""
    rows = task.task_assets.select_related(
        "asset",
        "asset__department",
        "asset__responsible_employee",
        "asset__location",
        "expected_department",
        "expected_employee",
        "expected_location",
    ).prefetch_related(
        Prefetch("scans", queryset=InventoryScan.objects.select_related(
            "inventory_task", "actual_location", "actual_employee"
        )),
        "resolutions",
    )
    if form.is_valid():
        query = form.cleaned_data["q"]
        if query:
            rows = rows.filter(
                Q(expected_code_snapshot__icontains=query)
                | Q(expected_name_snapshot__icontains=query)
                | Q(expected_employee_snapshot__icontains=query)
                | Q(asset__equipment_number__icontains=query)
            )
        view = form.cleaned_data["row_view"]
        effective_scans = InventoryScan.objects.filter(task_asset=OuterRef("pk"), is_effective=True)
        active_resolutions = InventoryResolution.objects.filter(
            inventory_task_asset=OuterRef("pk"), status="active"
        )
        if view == "missing":
            rows = rows.filter(~Exists(effective_scans))
        elif view == "scanned":
            rows = rows.filter(Exists(effective_scans))
        elif view == "normal":
            rows = rows.filter(Exists(effective_scans.filter(result="normal")))
        elif view == "exception":
            rows = rows.filter(Exists(effective_scans.exclude(result="normal")))
        elif view == "unresolved":
            rows = rows.filter(inventory_status__in=("exception", "missing")).filter(~Exists(active_resolutions))
        elif view == "resolved":
            rows = rows.filter(Exists(active_resolutions))
    else:
        rows = rows.none()
    return rows


@login_required
def task_detail(request, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    task = _task(request, pk)
    form = InventoryResultFilterForm(request.GET)
    valid = form.is_valid()
    rows = _filtered_result_rows(task, form)
    page_obj = Paginator(rows, form.cleaned_data.get("page_size") or 25).get_page(request.GET.get("page"))
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    row_items = []
    for row in page_obj:
        scans = list(row.scans.all())
        resolutions = list(row.resolutions.all())
        effective_scan = next((item for item in scans if item.is_effective), None)
        active_resolution = next(
            (item for item in resolutions if item.status == "active"), None
        )
        row_items.append(
            {
                "row": row,
                "scan": effective_scan,
                "scan_history": scans,
                "resolution": active_resolution,
                "resolution_history": resolutions,
                "expected_status_label": ASSET_STATUS_LABELS.get(
                    row.expected_asset_status, row.expected_asset_status
                ),
                "actual_status_label": ASSET_STATUS_LABELS.get(
                    effective_scan.actual_status if effective_scan else "",
                    effective_scan.actual_status if effective_scan else "",
                ),
                "can_upload_scan": any(
                    item.is_effective
                    and can_manage_inventory_attachment(request.user, item)
                    for item in scans
                ),
                "can_upload_resolution": any(
                    item.status == "active"
                    and can_manage_inventory_attachment(request.user, item)
                    for item in resolutions
                ),
            }
        )
    summary = inventory_task_summary(task) if task.status != "draft" else None
    return render(
        request,
        "inventory/task_detail.html",
        {
            "task": task,
            "summary": summary,
            "summary_links": {
                "expected": "?row_view=#inventory-results",
                "scanned": "?row_view=scanned#inventory-results",
                "exception": "?row_view=exception#inventory-results",
                "missing": "?row_view=missing#inventory-results",
            },
            "filter_form": form,
            "page_obj": page_obj,
            "pagination_query": pagination_params.urlencode(),
            "can_export_results": valid and task.status != "draft",
            "difference_source_query": urlencode(
                {"return_to": request.get_full_path() + "#inventory-results"}),
            **_publication_preview(request, task),
            "row_items": row_items,
            "assignees": task.assignees.select_related("user").order_by("user__username"),
            "surpluses": (task.surpluses.filter(resolution_status="pending") if request.GET.get("surplus_view") == "pending" else task.surpluses.all()).select_related(
                "found_by", "resolved_by", "linked_asset"
            ).order_by("found_at"),
            "attachments": _attachment_rows(request.user, task),
            "actions": {
                "edit": task.status == "draft" and can_publish_inventory_task(request.user, task),
                "publish": can_publish_inventory_task(request.user, task),
                "scan": can_scan_inventory_task(request.user, task),
                "reconcile": can_reconcile_inventory_task(request.user, task),
                "close": task.status == "reconciliation" and can_close_inventory_task(request.user, task),
                "correct": task.status == "closed" and can_close_inventory_task(request.user, task),
                "cancel": task.status in {"draft", "in_progress", "reconciliation"}
                and can_close_inventory_task(request.user, task),
            },
        },
        status=200 if valid else 400,
    )


@login_required
def task_result_export(request, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    task = _task(request, pk)
    if task.status == "draft":
        return HttpResponse("任务尚未发布，暂无应盘快照可导出。", status=400)
    form = InventoryResultFilterForm(request.GET)
    if not form.is_valid():
        return task_detail(request, pk)
    response = StreamingHttpResponse(
        inventory_result_csv(task, _filtered_result_rows(task, form)),
        content_type="text/csv; charset=utf-8",
    )
    # Keep user-entered task codes out of response header values.
    response["Content-Disposition"] = f'attachment; filename="inventory-{task.pk}.csv"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _confirm_action(
    request,
    *,
    task,
    title,
    description,
    callback,
    button_label,
    danger=False,
):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    error = None
    if request.method == "POST":
        if request.POST.get("confirm") != "on":
            error = "请勾选确认后再提交。"
        else:
            try:
                result = callback()
            except ValidationError as exc:
                error = "；".join(exc.messages)
            else:
                messages.success(request, f"{title}已完成。")
                return redirect("inventory:task-detail", pk=result.pk)
    return render(
        request,
        "inventory/confirm_action.html",
        {
            "task": task,
            "title": title,
            "description": description,
            "button_label": button_label,
            "danger": danger,
            "error": error,
            **_publication_preview(request, task),
        },
    )


@login_required
def task_publish(request, pk):
    task = _task(request, pk)
    if not can_publish_inventory_task(request.user, task):
        raise PermissionDenied("您没有发布此盘点任务的权限。")
    return _confirm_action(
        request,
        task=task,
        title="发布盘点任务",
        description="发布后将原子生成不可变应盘快照；快照不会随之后的调拨、责任人或位置变化而改变。",
        button_label="发布并生成快照",
        callback=lambda: publish_inventory_task(
            actor=request.user, task=task, request=request
        ),
    )


@sensitive_post_parameters("token")
@login_required
def task_scan_entry(request, pk):
    task = _task(request, pk)
    if not can_scan_inventory_task(request.user, task):
        raise PermissionDenied("您不是此任务的有效执行人，或任务已停止扫码。")
    if request.method == "POST":
        bridge = request.POST.get("scan_bridge")
        if bridge:
            identity, row, error, error_kind = _identity_and_row_from_bridge(
                request, task, bridge
            )
        else:
            token = _submitted_qr_token(request.POST.get("token"))
            if not token:
                return _render_scan(
                    request,
                    "inventory/scan_entry.html",
                    _scan_entry_context(request, task,
                        error="二维码无效或无法识别，请重新扫描资产标签。",
                        error_kind="invalid",
                    ),
                    status=403,
                )
            identity = AssetQrIdentity.objects.select_related("asset").filter(
                company=task.company,
                public_token=token,
                status="active",
                label_status="attached",
                asset__company=task.company,
            ).first()
            if identity is None:
                row = None
                error = "二维码无效、已失效或标签尚未启用，本次扫码不会计入进度。"
                error_kind = "invalid"
            else:
                row = task.task_assets.filter(asset=identity.asset).first()
                if row is None:
                    error = "此二维码有效，但对应资产不在本次盘点范围内（非本任务资产）。"
                    error_kind = "non_task"
                else:
                    error = None
                    error_kind = None
        if error:
            return _render_scan(
                request,
                "inventory/scan_entry.html",
                _scan_entry_context(request, task, error=error, error_kind=error_kind),
                status=403,
            )
        return _begin_scan_context(request, task, identity)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _render_scan(
        request,
        "inventory/scan_entry.html",
        _scan_entry_context(request, task),
    )


@login_required
def task_scan_context(request, pk, context_key):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    task = _task(request, pk)
    if not can_scan_inventory_task(request.user, task):
        raise PermissionDenied("您不是此任务的有效执行人，或任务已停止扫码。")
    state = request.session.get(_scan_context_session_key(task))
    if (
        not isinstance(state, dict)
        or state.get("key") != context_key
        or not state.get("identity_id")
    ):
        raise PermissionDenied("扫码上下文无效或已失效，请重新扫描。")
    identity = AssetQrIdentity.objects.select_related("asset").filter(
        pk=state["identity_id"],
        company=task.company,
        status="active",
        label_status="attached",
        asset__company=task.company,
    ).first()
    row = None
    if identity is not None:
        row = task.task_assets.filter(asset=identity.asset).first()
    if identity is None:
        request.session.pop(_scan_context_session_key(task), None)
        raise PermissionDenied("二维码无效、已失效或标签尚未启用，请重新扫描。")
    if row is None:
        request.session.pop(_scan_context_session_key(task), None)
        raise PermissionDenied("此二维码有效，但对应资产是非本任务资产。")
    form = InventoryScanForm(
        request.POST or None,
        actor=request.user,
        task=task,
        task_asset=row,
    )
    if request.method == "POST" and form.is_valid():
        try:
            scan = scan_inventory_asset(
                actor=request.user,
                task=task,
                public_token=identity.public_token,
                actual_location=form.cleaned_data["actual_location"],
                actual_employee=form.cleaned_data["actual_employee"],
                actual_status=form.cleaned_data["actual_status"],
                other_mismatch=form.cleaned_data["other_mismatch"],
                note=form.cleaned_data["note"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            request.session.pop(_scan_context_session_key(task), None)
            request.session[f"{RECENT_SCAN_SESSION_PREFIX}{task.pk}"] = str(scan.pk)
            messages.success(request, f"已记录盘点结果：{scan.get_result_display()}。")
            return _protect_scan_response(
                redirect("inventory:task-scan", pk=task.pk)
            )
    return _render_scan(
        request,
        "inventory/scan_form.html",
        _scan_form_context(task, row, form, supplemental=False),
    )


def _bound_action_form(
    request,
    *,
    task,
    form,
    title,
    description,
    callback,
    button_label,
    danger=False,
):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST" and form.is_valid():
        try:
            result = callback(form.cleaned_data)
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, f"{title}已完成。")
            return redirect("inventory:task-detail", pk=task.pk)
    return render(
        request,
        "inventory/action_form.html",
        {
            "task": task,
            "form": form,
            "title": title,
            "description": description,
            "button_label": button_label,
            "danger": danger,
        },
    )


@login_required
def task_stop(request, pk):
    task = _task(request, pk)
    form = InventoryStopForm(request.POST or None, actor=request.user, task=task)
    return _bound_action_form(
        request,
        task=task,
        form=form,
        title="停止扫码",
        description="停止后任务进入差异处理；普通扫码立即关闭，未扫描快照行显示为未盘。",
        button_label="停止扫码并进入差异处理",
        danger=True,
        callback=lambda data: stop_inventory_scanning(
            actor=request.user,
            task=task,
            reason=data["reason"],
            idempotency_key=data["idempotency_key"],
            request=request,
        ),
    )


@login_required
def task_close(request, pk):
    task = _task(request, pk)
    form = InventoryTaskCloseForm(request.POST or None, actor=request.user, task=task)
    return _bound_action_form(
        request,
        task=task,
        form=form,
        title="关闭盘点任务",
        description="关闭前后端会锁定任务并重新核对所有异常、未盘和盘盈结论；关闭后快照及原结论永久只读。",
        button_label="确认关闭任务",
        danger=True,
        callback=lambda data: close_inventory_task(
            actor=request.user,
            task=task,
            idempotency_key=data["idempotency_key"],
            request=request,
        ),
    )


@login_required
def task_cancel(request, pk):
    task = _task(request, pk)
    form = InventoryTaskCancelForm(request.POST or None, actor=request.user, task=task)
    return _bound_action_form(
        request,
        task=task,
        form=form,
        title="取消盘点任务",
        description="取消不会删除执行人、快照、扫描、差异结论或盘盈证据。",
        button_label="确认取消并保留证据",
        danger=True,
        callback=lambda data: cancel_inventory_task(
            actor=request.user,
            task=task,
            reason=data["reason"],
            idempotency_key=data["idempotency_key"],
            request=request,
        ),
    )


def _row(request, task, pk):
    return get_object_or_404(
        InventoryTaskAsset.objects.select_related(
            "inventory_task",
            "asset",
            "asset__department",
            "asset__responsible_employee",
            "asset__location",
        ),
        pk=pk,
        inventory_task=task,
        company=task.company,
    )


@sensitive_post_parameters("public_token")
@login_required
def task_supplement(request, task_pk, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    task = _task(request, task_pk)
    row = _row(request, task, pk)
    if InventoryResolution.objects.filter(
        inventory_task_asset=row,
        status="active",
    ).exists():
        raise PermissionDenied("已有有效处理结论的行不得再执行受控补盘。")
    form = SupplementalInventoryScanForm(
        request.POST or None,
        actor=request.user,
        task=task,
        task_asset=row,
    )
    token = _submitted_qr_token(request.POST.get("public_token"))
    if request.method == "POST" and form.is_valid():
        if not token:
            form.add_error(None, "受控补盘必须重新扫描当前二维码。")
        else:
            try:
                scan = supplemental_scan(
                    actor=request.user,
                    task_asset=row,
                    public_token=token,
                    actual_location=form.cleaned_data["actual_location"],
                    actual_employee=form.cleaned_data["actual_employee"],
                    actual_status=form.cleaned_data["actual_status"],
                    other_mismatch=form.cleaned_data["other_mismatch"],
                    note=form.cleaned_data["note"],
                    supplement_reason=form.cleaned_data["supplement_reason"],
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    request=request,
                )
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                messages.success(request, f"补盘结果已保存：{scan.get_result_display()}。")
                return redirect("inventory:task-detail", pk=task.pk)
    return _render_scan(
        request,
        "inventory/scan_form.html",
        _scan_form_context(task, row, form, supplemental=True),
    )


def _resolution_callback(request, task, row, data):
    return resolve_inventory_difference(
        actor=request.user,
        task_asset=row,
        resolution_type=data["resolution_type"],
        conclusion=data["conclusion"],
        to_department=data.get("to_department"),
        to_responsible_employee=data.get("to_responsible_employee"),
        to_location=data.get("to_location"),
        to_status=data.get("to_status"),
        effective_at=data.get("effective_at"),
        idempotency_key=data["idempotency_key"],
        request=request,
    )


def _difference_pending_rows(request, task, row):
    from .difference_workspace import difference_return_url
    source = difference_return_url(request, task)
    form = InventoryResultFilterForm(QueryDict(urlsplit(source).query))
    rows = _filtered_result_rows(task, form).filter(inventory_status__in=("exception", "missing"))
    active = InventoryResolution.objects.filter(inventory_task_asset=OuterRef("pk"), status="active")
    return rows.filter(~Exists(active)).exclude(pk=row.pk).order_by("expected_code_snapshot", "pk")


def _difference_form_view(request, *, task, row, form, title, description,
                          button_label, callback, previous_resolution=None):
    from .difference_workspace import difference_evidence, difference_return_query, difference_return_url
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    source = difference_return_url(request, task)
    if request.method == "POST" and form.is_valid():
        try:
            callback(form.cleaned_data)
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, f"{row.expected_code_snapshot}：{title}已完成。")
            if previous_resolution is None and request.POST.get("action") == "continue":
                next_row = _difference_pending_rows(request, task, row).first()
                if next_row is not None:
                    return redirect(reverse("inventory:task-resolve", args=[task.pk, next_row.pk])
                                    + "?" + difference_return_query(request, task))
                messages.info(request, "当前查询范围内的应盘差异已处理完，可返回明细检查其他差异或盘盈。")
            return redirect(source)
    evidence = difference_evidence(row)
    pending_count = _difference_pending_rows(request, task, row).count() if previous_resolution is None else 0
    return render(request, "inventory/difference_form.html", {
        "task": task, "row": row, "form": form, "title": title, "description": description,
        "button_label": button_label, "difference_return_to": source,
        "difference_other_pending": pending_count, "previous_resolution": previous_resolution,
        "difference_can_save": (task.status == "closed" and previous_resolution.status == "active"
                                if previous_resolution is not None else
                                task.status == "reconciliation" and not evidence["difference_active_resolution"]
                                and (not evidence["difference_scan"] or evidence["difference_scan"].result != "normal")),
        **evidence,
    })


@login_required
def task_resolve(request, task_pk, pk):
    task = _task(request, task_pk)
    row = _row(request, task, pk)
    form = InventoryResolutionForm(request.POST if request.method == "POST" else None, actor=request.user, task=task)
    return _difference_form_view(
        request,
        task=task,
        row=row,
        form=form,
        title="形成盘点差异结论",
        description="核对发布快照和有效现场结果，再选择处理结论。",
        button_label="保存处理结论",
        callback=lambda data: _resolution_callback(request, task, row, data),
    )


@login_required
def resolution_correct(request, task_pk, pk):
    task = _task(request, task_pk)
    resolution = get_object_or_404(
        InventoryResolution.objects.select_related("inventory_task_asset", "resolved_by",
            "inventory_task_asset__asset__department", "inventory_task_asset__asset__responsible_employee",
            "inventory_task_asset__asset__location"),
        pk=pk,
        company=task.company,
        inventory_task_asset__inventory_task=task,
    )
    form = InventoryResolutionCorrectionForm(
        request.POST if request.method == "POST" else None, actor=request.user, task=task
    )
    return _difference_form_view(
        request,
        task=task,
        row=resolution.inventory_task_asset,
        previous_resolution=resolution,
        form=form,
        title="新增关闭后更正结论",
        description="原结论不会被覆盖或删除；新记录会指向原结论，任务保持关闭。",
        button_label="保存更正记录",
        callback=lambda data: correct_inventory_resolution(
            actor=request.user,
            resolution=resolution,
            resolution_type=data["resolution_type"],
            conclusion=data["conclusion"],
            correction_reason=data["correction_reason"],
            to_department=data.get("to_department"),
            to_responsible_employee=data.get("to_responsible_employee"),
            to_location=data.get("to_location"),
            to_status=data.get("to_status"),
            effective_at=data.get("effective_at"),
            idempotency_key=data["idempotency_key"],
            request=request,
        ),
    )


@login_required
def surplus_create(request, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    task = _task(request, pk)
    form = InventorySurplusForm(request.POST or None, actor=request.user, task=task)
    if request.method == "POST" and form.is_valid():
        try:
            surplus = create_inventory_surplus(
                actor=request.user,
                task=task,
                temporary_name=form.cleaned_data["temporary_name"],
                temporary_category_text=form.cleaned_data["temporary_category_text"],
                temporary_location_text=form.cleaned_data["temporary_location_text"],
                remark=form.cleaned_data["remark"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "盘盈记录已建立；请继续上传现场照片证据。")
            return redirect(
                "inventory:surplus-detail", task_pk=task.pk, pk=surplus.pk
            )
    return render(
        request,
        "inventory/action_form.html",
        {
            "task": task,
            "form": form,
            "title": "登记盘盈",
            "description": "系统外实物先建立盘盈待确认记录，不伪造资产 ID，也不占用正式编号。保存后请上传至少一张现场照片。",
            "button_label": "保存盘盈待确认",
        },
    )


def _surplus(request, task, pk):
    return get_object_or_404(
        InventorySurplus.objects.select_related(
            "inventory_task", "found_by", "resolved_by", "linked_asset"
        ),
        pk=pk,
        company=task.company,
        inventory_task=task,
    )


@login_required
def surplus_detail(request, task_pk, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    task = _task(request, task_pk)
    surplus = _surplus(request, task, pk)
    links = []
    for link in surplus.attachment_links.select_related("attachment", "created_by"):
        if can_view_inventory_attachment(request.user, link):
            links.append(
                {
                    "link": link,
                    "can_void": can_manage_inventory_attachment(request.user, surplus),
                }
            )
    return render(
        request,
        "inventory/surplus_detail.html",
        {
            "task": task,
            "surplus": surplus,
            "attachments": links,
            "can_upload": can_manage_inventory_attachment(request.user, surplus),
            "can_resolve": task.status == "reconciliation"
            and can_convert_inventory_surplus(request.user, surplus)
            and surplus.resolution_status == "pending",
            "can_convert": surplus.resolution_status == "pending"
            and can_convert_inventory_surplus(request.user, surplus),
        },
    )


@login_required
def surplus_resolve(request, task_pk, pk):
    task = _task(request, task_pk)
    surplus = _surplus(request, task, pk)
    if not can_convert_inventory_surplus(request.user, surplus):
        raise PermissionDenied("您没有处理此盘盈的权限。")
    form = InventorySurplusResolutionForm(
        request.POST or None,
        initial={"idempotency_key": uuid.uuid4().hex},
    )
    return _bound_action_form(
        request,
        task=task,
        form=form,
        title="确认盘盈处理结论",
        description="处理结论永久留痕；此动作不会建立或修改资产主档。",
        button_label="保存盘盈结论",
        callback=lambda data: resolve_inventory_surplus(
            actor=request.user,
            surplus=surplus,
            resolution_status=data["resolution_status"],
            remark=data["remark"],
            idempotency_key=data["idempotency_key"],
            request=request,
        ),
    )


@login_required
def surplus_convert(request, task_pk, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    task = _task(request, task_pk)
    surplus = _surplus(request, task, pk)
    if not can_convert_inventory_surplus(request.user, surplus):
        raise PermissionDenied("只有 finance 可确认盘盈并转为资产草稿。")
    form = AssetDraftForm(
        request.POST or None,
        actor=request.user,
        company=task.company,
    )
    remark = (request.POST.get("conversion_remark") or "").strip()
    key = request.POST.get("_idempotency_key") or uuid.uuid4().hex
    if request.method == "POST" and form.is_valid():
        if not remark:
            form.add_error(None, "盘盈转资产草稿必须填写处理说明。")
        else:
            try:
                asset = convert_surplus_to_asset_draft(
                    actor=request.user,
                    surplus=surplus,
                    asset_data=form.cleaned_data,
                    custom_values=None,
                    remark=remark,
                    idempotency_key=key,
                    request=request,
                )
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                messages.success(
                    request, "盘盈已转为资产草稿；尚未生成正式编号或二维码。"
                )
                return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "inventory/surplus_convert.html",
        {
            "task": task,
            "surplus": surplus,
            "form": form,
            "remark": remark,
            "idempotency_key": key,
        },
    )


def _attachment_target(request, task, target_type, target_pk):
    model_map = {
        "surplus": (InventorySurplus, "inventory_task"),
        "scan": (InventoryScan, "inventory_task"),
        "resolution": (InventoryResolution, "inventory_task_asset__inventory_task"),
    }
    try:
        model, task_lookup = model_map[target_type]
    except KeyError as exc:
        raise Http404("盘点附件目标类型无效。") from exc
    return get_object_or_404(
        model._base_manager,
        pk=target_pk,
        company=task.company,
        **{task_lookup: task},
    )


@login_required
def attachment_upload(request, task_pk, target_type, target_pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    task = _task(request, task_pk)
    target = _attachment_target(request, task, target_type, target_pk)
    form = InventoryAttachmentUploadForm(
        request.POST or None,
        request.FILES or None,
        actor=request.user,
        target=target,
    )
    if request.method == "POST" and form.is_valid():
        from apps.inventory.services import upload_inventory_attachment

        try:
            upload_many(upload_inventory_attachment,
                actor=request.user,
                target=target,
                uploaded_file=form.cleaned_data["uploaded_file"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "盘点证据已上传到私有附件存储。")
            if target_type == "surplus":
                return redirect(
                    "inventory:surplus-detail", task_pk=task.pk, pk=target.pk
                )
            return redirect("inventory:task-detail", pk=task.pk)
    return render(
        request,
        "inventory/action_form.html",
        {
            "task": task,
            "form": form,
            "title": "上传盘点证据",
            "description": "附件存放于 Web root 之外；下载时会再次校验任务、角色和对象范围。",
            "button_label": "上传证据",
            "multipart": True,
        },
    )


@login_required
def attachment_download(request, task_pk, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    task = _task(request, task_pk)
    link = get_object_or_404(
        AttachmentLink._base_manager.select_related(
            "attachment",
            "inventory_surplus__inventory_task",
            "inventory_scan__inventory_task",
            "inventory_resolution__inventory_task_asset__inventory_task",
        ),
        pk=pk,
        company=task.company,
        status="active",
        attachment__is_available=True,
        attachment__malware_scan_status__in=(
            Attachment.MalwareScanStatus.POLICY_LIMITED,
            Attachment.MalwareScanStatus.CLEAN,
        ),
    )
    target = link.inventory_surplus or link.inventory_scan or link.inventory_resolution
    target_task = getattr(target, "inventory_task", None)
    if target_task is None and target is not None:
        target_task = target.inventory_task_asset.inventory_task
    if target_task is None or target_task.pk != task.pk:
        raise Http404("附件不属于该盘点任务。")
    link = require_inventory_attachment_download(actor=request.user, link=link)
    attachment = link.attachment
    if not default_storage.exists(attachment.storage_key):
        raise Http404("附件文件当前不可用。")
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
def attachment_void(request, task_pk, pk):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    task = _task(request, task_pk)
    link = get_object_or_404(
        AttachmentLink._base_manager.select_related(
            "attachment",
            "inventory_surplus",
            "inventory_scan",
            "inventory_resolution__inventory_task_asset",
        ),
        pk=pk,
        company=task.company,
    )
    target = link.inventory_surplus or link.inventory_scan or link.inventory_resolution
    target_task = getattr(target, "inventory_task", None)
    if target_task is None and target is not None:
        target_task = target.inventory_task_asset.inventory_task
    if target_task is None or target_task.pk != task.pk:
        raise Http404("附件不属于该盘点任务。")
    if not can_manage_inventory_attachment(request.user, target):
        raise PermissionDenied("您没有作废此盘点证据的权限。")
    form = InventoryAttachmentVoidForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from apps.inventory.services import void_inventory_attachment

        try:
            void_inventory_attachment(
                actor=request.user,
                link=link,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "盘点证据已作废；文件元数据和审计记录仍保留。")
            return redirect("inventory:task-detail", pk=task.pk)
    return render(
        request,
        "inventory/action_form.html",
        {
            "task": task,
            "form": form,
            "title": "作废盘点证据",
            "description": f"附件：{link.attachment.safe_filename}。作废不会物理删除文件或历史。",
            "button_label": "确认作废",
            "danger": True,
        },
    )
