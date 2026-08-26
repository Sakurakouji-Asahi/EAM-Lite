"""Server-rendered Sprint 6 QR label and scanning views."""

from __future__ import annotations

import secrets
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db.models import Count, Max, Prefetch, Q, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.assets.models import (
    Asset,
    AssetLabelPrintBatch,
    AssetLabelPrintItem,
    AssetQrIdentity,
)
from apps.assets.permissions import (
    can_view_asset,
    can_view_asset_p1,
    can_view_asset_summary_fields,
    can_view_attachment,
    can_view_financial_fields,
)
from apps.assets.qr_forms import (
    LabelAttachmentForm,
    LabelPrintForm,
    PrintResultForm,
    SingleLabelPrintForm,
    TokenRotationForm,
    WebLabelAttachmentForm,
)
from apps.assets.qr_permissions import (
    QR_ACTION_ROLES,
    can_manage_labels,
    require_label_action,
    scoped_printable_assets,
    scoped_scannable_assets,
)
from apps.assets.qr_services import (
    cancel_print_batch,
    confirm_label_attachment,
    confirm_print_batch,
    generate_print_batch,
    render_qr_svg,
    rotate_qr_identity,
)
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.maintenance.permissions import (
    can_complete_maintenance,
    can_view_maintenance_asset_summary,
)
from apps.maintenance.services import due_maintenance_plans
from apps.masterdata.permissions import current_company, role_names_for


QUEUE_STATUSES = frozenset({"ready_to_print", "printed", "attached"})


def _company():
    company = current_company()
    if company is None or not company.is_active:
        raise Http404("尚未配置启用公司。")
    return company


def _require_login(request):
    if request.user.is_authenticated:
        return None
    return redirect_to_login(request.get_full_path())


def _scan_response(response):
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _scan_redirect(token):
    return _scan_response(redirect("assets:qr-scan", token=token))


def _item_has_current_printable_identity(item):
    identity = item.qr_identity
    return (
        identity.status == AssetQrIdentity.Status.ACTIVE
        and identity.label_status
        in {
            AssetQrIdentity.LabelStatus.READY_TO_PRINT,
            AssetQrIdentity.LabelStatus.PRINTED,
        }
        and identity.asset.qr_identities.filter(
            pk=identity.pk,
            status=AssetQrIdentity.Status.ACTIVE,
        ).exists()
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


def _require_label_role(user):
    if not role_names_for(user).intersection(QR_ACTION_ROLES):
        raise PermissionDenied("您没有标签打印或贴标操作权限。")


def _location_path(location):
    names = []
    seen = set()
    while location is not None and location.pk not in seen:
        seen.add(location.pk)
        names.append(location.name)
        location = location.parent
    return " / ".join(reversed(names)) or "—"


def _maintenance_context(user, asset):
    from apps.maintenance.models import MaintenancePlan, MaintenanceRecord

    if not can_view_maintenance_asset_summary(user, asset):
        return {"show_maintenance": False}

    status_labels = {
        "upcoming": "即将到期",
        "due_today": "今日到期",
        "overdue": "逾期",
        "not_due": "未到提醒期",
    }
    plans = []
    queryset = MaintenancePlan.objects.select_related(
        "asset", "responsible_employee"
    ).filter(asset=asset, status="active")
    for plan, status in due_maintenance_plans(
        user,
        asset.company,
        queryset=queryset,
    ):
        plans.append(
            {
                "plan": plan,
                "due_status": status,
                "due_label": status_labels.get(status, "数据不足"),
                "can_complete": can_complete_maintenance(user, plan),
            }
        )
    latest_record = (
        MaintenanceRecord.objects.filter(
            asset=asset,
            status="confirmed",
            maintenance_plan__in=[item["plan"] for item in plans],
        )
        .select_related("maintenance_plan")
        .order_by("-completed_date", "-created_at", "-pk")
        .first()
    )
    return {
        "show_maintenance": bool(plans),
        "maintenance_plans": plans,
        "latest_maintenance": latest_record,
    }


def _queue_assets(user, company):
    active_identities = AssetQrIdentity.objects.filter(status="active").annotate(
        last_printed_at=Max("print_items__batch__printed_at")
    ).order_by("version")
    queryset = Asset.objects.select_related(
        "company", "department", "responsible_employee", "location"
    ).prefetch_related(
        Prefetch("qr_identities", queryset=active_identities, to_attr="active_qr_rows")
    )
    return list(
        scoped_printable_assets(user, company, queryset)
        .filter(asset_code__isnull=False, qr_identities__status="active")
        .distinct()
        .order_by("asset_code", "pk")
    )


def _current_qr(asset):
    rows = getattr(asset, "active_qr_rows", ())
    return rows[0] if rows else None


def _batch_for_user(user, company, pk):
    _require_label_role(user)
    batch = get_object_or_404(AssetLabelPrintBatch, company=company, pk=pk)
    asset_ids = set(
        batch.items.values_list("qr_identity__asset_id", flat=True)
    )
    allowed = set(
        scoped_printable_assets(user, company)
        .filter(pk__in=asset_ids)
        .values_list("pk", flat=True)
    )
    if allowed != asset_ids:
        raise PermissionDenied("打印批次包含您无权访问的资产。")
    return batch


@require_http_methods(["GET", "POST"])
def label_queue(request):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    _require_label_role(request.user)
    all_assets = _queue_assets(request.user, company)
    selected_status = (
        request.POST.get("status") if request.method == "POST" else request.GET.get("status")
    ) or "ready_to_print"
    if selected_status not in QUEUE_STATUSES:
        selected_status = "ready_to_print"

    rows = []
    printable_assets = []
    for asset in all_assets:
        qr_identity = _current_qr(asset)
        if qr_identity is None:
            continue
        if qr_identity.label_status in {"ready_to_print", "printed"}:
            printable_assets.append(asset)
        if qr_identity.label_status == selected_status:
            rows.append(
                {
                    "asset": asset,
                    "qr_identity": qr_identity,
                    "last_printed_at": qr_identity.last_printed_at,
                    "selectable": qr_identity.label_status != "attached",
                    "location_path": _location_path(asset.location),
                }
            )

    form = LabelPrintForm(
        request.POST or None,
        assets=printable_assets,
    )
    if request.method == "POST" and form.is_valid():
        selected_ids = form.cleaned_data["asset_ids"]
        selected_assets = [
            asset for asset in printable_assets if str(asset.pk) in selected_ids
        ]
        try:
            batch = generate_print_batch(
                actor=request.user,
                assets=selected_assets,
                idempotency_key=form.cleaned_data["idempotency_key"],
                include_responsible_employee=form.cleaned_data[
                    "include_responsible_employee"
                ],
                include_location=form.cleaned_data["include_location"],
                include_model=form.cleaned_data["include_model"],
                explicit_reprint=form.cleaned_data["explicit_reprint"],
                request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, f"打印批次 {batch.batch_code} 已记录并打开 A4 预览。")
            return redirect("assets:label-batch-print", pk=batch.pk)

    return render(
        request,
        "assets/qr_queue.html",
        {
            "form": form,
            "rows": rows,
            "selected_status": selected_status,
            "status_choices": AssetQrIdentity.LabelStatus.choices[1:],
        },
    )


@require_http_methods(["GET"])
def label_batch_list(request):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    _require_label_role(request.user)
    allowed_asset_ids = scoped_printable_assets(request.user, company).values("pk")
    batches = (
        AssetLabelPrintBatch.objects.filter(company=company)
        .annotate(
            item_count=Count("items", distinct=True),
            forbidden_item_count=Count(
                "items",
                filter=~Q(items__qr_identity__asset_id__in=allowed_asset_ids),
                distinct=True,
            ),
        )
        .filter(forbidden_item_count=0)
    )
    return render(request, "assets/qr_batch_list.html", {"batches": batches})


@require_http_methods(["GET"])
def label_batch_detail(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    batch = _batch_for_user(request.user, company, pk)
    items = list(
        batch.items.select_related("qr_identity__asset").order_by(
            "page_no", "position_no"
        )
    )
    return render(
        request,
        "assets/qr_batch_detail.html",
        {"batch": batch, "items": items, "result_form": PrintResultForm()},
    )


@require_http_methods(["GET"])
def label_batch_print(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    batch = _batch_for_user(request.user, company, pk)
    if batch.status != AssetLabelPrintBatch.Status.PRINTED:
        raise PermissionDenied("只有已记录打印操作的批次可打开打印视图。")
    items = list(
        batch.items.select_related("qr_identity__asset").order_by(
            "page_no", "position_no"
        )
    )
    if not items or any(not _item_has_current_printable_identity(item) for item in items):
        raise PermissionDenied("批次含已失效或非当前二维码身份，不得继续打印。")
    return render(
        request,
        "assets/label_print.html",
        {
            "batch": batch,
            "items": items,
            "qr_origin_is_temporary": not settings.QR_BASE_URL_IS_DURABLE,
        },
    )


@require_POST
def label_batch_confirm(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    batch = _batch_for_user(request.user, company, pk)
    try:
        batch = confirm_print_batch(actor=request.user, batch=batch, request=request)
    except ValidationError as exc:
        messages.error(request, "；".join(exc.messages))
        return redirect("assets:label-batch-detail", pk=batch.pk)
    messages.success(request, "打印操作已记录；无需再次确认打印。")
    return redirect("assets:label-batch-print", pk=batch.pk)


@require_POST
def label_batch_cancel(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    batch = _batch_for_user(request.user, company, pk)
    form = PrintResultForm(request.POST)
    if not form.is_valid():
        messages.error(request, "打印取消说明格式无效。")
        return redirect("assets:label-batch-detail", pk=batch.pk)
    try:
        batch = cancel_print_batch(
            actor=request.user,
            batch=batch,
            reason=form.cleaned_data["failure_reason"],
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "；".join(exc.messages))
    else:
        messages.success(request, "批次已取消，二维码仍保持待打印状态。")
    return redirect("assets:label-batch-detail", pk=batch.pk)


@require_http_methods(["GET"])
def label_item_qr_svg(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    item = get_object_or_404(
        AssetLabelPrintItem.objects.select_related(
            "batch", "qr_identity__asset", "qr_identity__company"
        ),
        pk=pk,
        batch__company=company,
    )
    require_label_action(request.user, item.qr_identity.asset)
    if item.batch.status != AssetLabelPrintBatch.Status.PRINTED:
        raise PermissionDenied("该批次当前不可输出打印二维码。")
    if not _item_has_current_printable_identity(item):
        raise PermissionDenied("二维码身份已失效或已非资产当前身份。")
    response = HttpResponse(render_qr_svg(item.qr_identity), content_type="image/svg+xml")
    response["Cache-Control"] = "private, no-store"
    response["Content-Disposition"] = 'inline; filename="qr.svg"'
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_http_methods(["GET"])
def asset_current_qr_svg(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    _require_label_role(request.user)
    asset = get_object_or_404(
        scoped_printable_assets(
            request.user,
            company,
            Asset.objects.select_related("company"),
        ),
        pk=pk,
    )
    identity = get_object_or_404(
        AssetQrIdentity,
        asset=asset,
        status=AssetQrIdentity.Status.ACTIVE,
    )
    response = HttpResponse(render_qr_svg(identity), content_type="image/svg+xml")
    response["Cache-Control"] = "private, no-store"
    response["Content-Disposition"] = 'inline; filename="asset-qr.svg"'
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_POST
def asset_label_print(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    _require_label_role(request.user)
    asset = get_object_or_404(
        scoped_printable_assets(
            request.user,
            company,
            Asset.objects.select_related("company"),
        ),
        pk=pk,
    )
    qr_identity = get_object_or_404(
        AssetQrIdentity,
        asset=asset,
        status=AssetQrIdentity.Status.ACTIVE,
    )
    form = SingleLabelPrintForm(request.POST)
    if not form.is_valid():
        messages.error(request, "打印请求无效，请刷新资产页后重试。")
        return redirect("assets:asset-detail", pk=asset.pk)
    try:
        batch = generate_print_batch(
            actor=request.user,
            assets=[asset],
            idempotency_key=form.cleaned_data["idempotency_key"],
            explicit_reprint=(
                qr_identity.label_status == AssetQrIdentity.LabelStatus.PRINTED
            ),
            request=request,
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, "；".join(getattr(exc, "messages", [str(exc)])))
        return redirect("assets:asset-detail", pk=asset.pk)
    return redirect("assets:label-batch-print", pk=batch.pk)


def _lookup_scanned_identity(token):
    return AssetQrIdentity.objects.filter(public_token=token).only(
        "id", "company_id", "asset_id", "status", "label_status"
    ).first()


def _invalid_scan(request, *, revoked=False):
    template = "assets/qr_scan_invalid.html"
    response = render(
        request,
        template,
        {"revoked": revoked},
        status=410 if revoked else 404,
    )
    return _scan_response(response)


def _scan_asset_or_response(request, token):
    identity_stub = _lookup_scanned_identity(token)
    if identity_stub is None:
        return None, _invalid_scan(request)
    if identity_stub.status == AssetQrIdentity.Status.REVOKED:
        return None, _invalid_scan(request, revoked=True)
    company = _company()
    if identity_stub.company_id != company.pk:
        return None, _invalid_scan(request)
    asset = (
        scoped_scannable_assets(
            request.user,
            company,
            Asset.objects.select_related(
                "company", "category", "department", "responsible_employee", "location"
            ),
        )
        .filter(pk=identity_stub.asset_id)
        .first()
    )
    if asset is None:
        write_business_audit_log(
            company=company,
            user=request.user,
            action="asset_qr.scan_denied",
            object_type="AssetQrIdentity",
            object_id=identity_stub.pk,
            old_data={},
            new_data={"reason": "object_scope_denied"},
            **request_audit_context(request),
        )
        response = render(request, "assets/qr_scan_forbidden.html", status=403)
        return None, _scan_response(response)
    identity = AssetQrIdentity.objects.get(pk=identity_stub.pk)
    return (asset, identity), None


@require_http_methods(["GET"])
def qr_scan(request, token):
    login_response = _require_login(request)
    if login_response:
        return _scan_response(login_response)
    result, response = _scan_asset_or_response(request, token)
    if response is not None:
        return response
    asset, qr_identity = result
    can_p1 = can_view_asset_p1(request.user, asset)
    can_summary = can_view_asset_summary_fields(request.user, asset)
    can_financial = can_view_financial_fields(request.user)
    archived = asset.record_status == Asset.RecordStatus.ARCHIVED
    cover_link = asset.cover_attachment_link if can_p1 and not archived else None
    if cover_link is not None and not can_view_attachment(request.user, cover_link):
        cover_link = None
    finance_summary = None
    if can_financial and not archived:
        finance = getattr(asset, "finance", None)
        if finance is not None:
            actual_depreciation = (
                asset.depreciation_entries.aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )
            finance_summary = {
                "accounting_treatment": finance.get_accounting_treatment_display(),
                "original_cost": finance.original_cost,
                "actual_depreciation": actual_depreciation,
                "impairment": finance.impairment_balance_cache,
                "book_value": (
                    finance.original_cost
                    - actual_depreciation
                    - finance.impairment_balance_cache
                ),
            }
    first_attachment = asset.asset_status == Asset.AssetStatus.PENDING_LABEL
    attachment_form = None
    if (
        not archived
        and can_manage_labels(request.user, asset)
        and qr_identity.label_status == "printed"
    ):
        attachment_form = LabelAttachmentForm(
            first_attachment=first_attachment,
            initial={"scanned_token": token},
        )
    maintenance_context = (
        _maintenance_context(request.user, asset) if not archived else {}
    )
    response = render(
        request,
        "assets/qr_scan.html",
        {
            "asset": asset,
            "qr_identity": qr_identity,
            "can_p1": can_p1,
            "can_summary": can_summary,
            "can_view_full_asset": can_view_asset(request.user, asset),
            "can_manage_labels": (
                not archived and can_manage_labels(request.user, asset)
            ),
            "finance_summary": finance_summary,
            "cover_link": cover_link,
            "location_path": _location_path(asset.location),
            "attachment_form": attachment_form,
            "first_attachment": first_attachment,
            **maintenance_context,
        },
    )
    return _scan_response(response)


@require_http_methods(["GET"])
def qr_scan_cover(request, pk):
    login_response = _require_login(request)
    if login_response:
        return _scan_response(login_response)
    company = _company()
    asset = get_object_or_404(
        scoped_scannable_assets(request.user, company), pk=pk
    )
    if not can_view_asset_p1(request.user, asset):
        raise Http404("未找到可查看的封面照片。")
    link = asset.cover_attachment_link
    if link is None or not can_view_attachment(request.user, link):
        raise Http404("未找到可查看的封面照片。")
    attachment = link.attachment
    if not default_storage.exists(attachment.storage_key):
        raise Http404("封面照片当前不可用。")
    response = FileResponse(
        default_storage.open(attachment.storage_key, "rb"),
        content_type=attachment.mime_type,
    )
    response["Content-Disposition"] = 'inline; filename="asset-cover"'
    return _scan_response(response)


@require_POST
def qr_attach(request, token):
    login_response = _require_login(request)
    if login_response:
        return _scan_response(login_response)
    result, response = _scan_asset_or_response(request, token)
    if response is not None:
        return response
    asset, qr_identity = result
    first_attachment = asset.asset_status == Asset.AssetStatus.PENDING_LABEL
    form = LabelAttachmentForm(request.POST, first_attachment=first_attachment)
    if form.is_valid() and not secrets.compare_digest(
        form.cleaned_data["scanned_token"], token
    ):
        form.add_error("scanned_token", "提交的二维码与当前扫描地址不一致。")
    if form.is_valid():
        try:
            confirm_label_attachment(
                actor=request.user,
                asset=asset,
                scanned_token=token,
                target_status=form.cleaned_data.get("target_status") or None,
                idempotency_key=form.cleaned_data["idempotency_key"],
                confirmation_method=(
                    "scan_opaque_origin"
                    if getattr(
                        request,
                        "qr_opaque_origin_csrf_compatibility",
                        False,
                    )
                    else "scan"
                ),
                request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "现场贴标已确认并记录审计。")
            return _scan_redirect(token)
    finance_summary = None
    if can_view_financial_fields(request.user):
        finance = getattr(asset, "finance", None)
        if finance is not None:
            actual_depreciation = (
                asset.depreciation_entries.aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )
            finance_summary = {
                "accounting_treatment": finance.get_accounting_treatment_display(),
                "original_cost": finance.original_cost,
                "actual_depreciation": actual_depreciation,
                "impairment": finance.impairment_balance_cache,
                "book_value": (
                    finance.original_cost
                    - actual_depreciation
                    - finance.impairment_balance_cache
                ),
            }
    cover_link = asset.cover_attachment_link if can_view_asset_p1(request.user, asset) else None
    if cover_link is not None and not can_view_attachment(request.user, cover_link):
        cover_link = None
    response = render(
        request,
        "assets/qr_scan.html",
        {
            "asset": asset,
            "qr_identity": qr_identity,
            "can_p1": can_view_asset_p1(request.user, asset),
            "can_summary": can_view_asset_summary_fields(request.user, asset),
            "can_view_full_asset": can_view_asset(request.user, asset),
            "can_manage_labels": can_manage_labels(request.user, asset),
            "finance_summary": finance_summary,
            "cover_link": cover_link,
            "location_path": _location_path(asset.location),
            "attachment_form": form,
            "first_attachment": first_attachment,
            **(_maintenance_context(request.user, asset) if asset.record_status != Asset.RecordStatus.ARCHIVED else {}),
        },
        status=400,
    )
    return _scan_response(response)


@require_http_methods(["GET", "POST"])
def qr_web_attach(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    _require_label_role(request.user)
    asset = get_object_or_404(
        scoped_printable_assets(
            request.user,
            company,
            Asset.objects.select_related(
                "company", "category", "department", "responsible_employee", "location"
            ),
        ),
        pk=pk,
    )
    require_label_action(request.user, asset)
    qr_identity = get_object_or_404(
        AssetQrIdentity,
        asset=asset,
        status=AssetQrIdentity.Status.ACTIVE,
    )
    first_attachment = asset.asset_status == Asset.AssetStatus.PENDING_LABEL
    can_confirm = qr_identity.label_status == AssetQrIdentity.LabelStatus.PRINTED
    form = WebLabelAttachmentForm(
        request.POST or None,
        first_attachment=first_attachment,
        qr_identity_id=qr_identity.pk,
    )
    if request.method == "POST":
        form.is_valid()
        if not can_confirm:
            form.add_error(None, "只有已执行打印操作的当前二维码才能在 Web 端确认贴标。")
        if (
            form.is_valid()
            and form.cleaned_data["qr_identity_id"] != qr_identity.pk
        ):
            form.add_error("qr_identity_id", "页面中的二维码已变化，请刷新后重新核对。")
        if form.is_valid():
            try:
                confirm_label_attachment(
                    actor=request.user,
                    asset=asset,
                    scanned_token=qr_identity.public_token,
                    target_status=form.cleaned_data.get("target_status") or None,
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    confirmation_method=(
                        "web_opaque_origin"
                        if getattr(
                            request,
                            "qr_opaque_origin_csrf_compatibility",
                            False,
                        )
                        else "web"
                    ),
                    request=request,
                )
            except (PermissionDenied, ValidationError) as exc:
                _service_error(form, exc)
            else:
                messages.success(request, "Web 端贴标确认已完成，并已记录操作方式和审计。")
                return redirect("assets:asset-detail", pk=asset.pk)
    response = render(
        request,
        "assets/qr_web_attach.html",
        {
            "asset": asset,
            "qr_identity": qr_identity,
            "form": form,
            "first_attachment": first_attachment,
            "can_confirm": can_confirm,
            "location_path": _location_path(asset.location),
        },
        status=400 if request.method == "POST" and form.errors else 200,
    )
    return _scan_response(response)


@require_http_methods(["GET", "POST"])
def qr_rotate(request, pk):
    login_response = _require_login(request)
    if login_response:
        return login_response
    company = _company()
    asset = get_object_or_404(
        Asset.objects.select_related("company"), company=company, pk=pk
    )
    require_label_action(request.user, asset)
    current_identity = get_object_or_404(
        AssetQrIdentity, asset=asset, status=AssetQrIdentity.Status.ACTIVE
    )
    form = TokenRotationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reason = f"{form.cleaned_data['reason']}：{form.cleaned_data['explanation']}"
        try:
            new_identity = rotate_qr_identity(
                actor=request.user,
                asset=asset,
                reason=reason,
                request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(
                request,
                f"换标身份已建立（版本 {new_identity.version}），旧标签已立即失效。",
            )
            return redirect("assets:label-queue")
    return render(
        request,
        "assets/qr_rotate.html",
        {"asset": asset, "qr_identity": current_identity, "form": form},
    )
