"""Permission-scoped audit history and explicit undo confirmations."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib import messages
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from apps.audit.forms import AuditLogFilterForm
from apps.audit.permissions import require_view_audit_logs
from apps.audit.query import (
    audit_log_queryset,
    project_audit_log,
    visible_audit_actors,
)
from apps.masterdata.permissions import current_company, role_names_for
from apps.audit.models import OperationUndo
from apps.audit.permissions import scoped_audit_logs
from apps.audit.undo import SUPPORTED, preview_undo, undo_operation


_ALLOWED_QUERY_KEYS = frozenset(
    {
        "start_at",
        "end_at",
        "actor",
        "action",
        "object_type",
        "object_id",
        "correlation_id",
        "page_size",
        "page",
    }
)


@never_cache
@login_required
@require_GET
def audit_log_list(request):
    try:
        require_view_audit_logs(request.user)
    except PermissionDenied:
        response = HttpResponseForbidden("您没有查看操作日志的权限。")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    unexpected = set(request.GET) - _ALLOWED_QUERY_KEYS
    if unexpected:
        response = HttpResponseBadRequest("包含不支持的操作日志筛选参数。")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    company = current_company(include_inactive=True)
    if company is None:
        response = HttpResponseBadRequest("当前没有可用公司。")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    form = AuditLogFilterForm(
        request.GET,
        company=company,
        actor_queryset=visible_audit_actors(user=request.user, company=company),
    )
    if not form.is_valid():
        response = render(
            request,
            "audit/log_list.html",
            {"form": form, "page_obj": None, "filter_query": ""},
            status=400,
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    queryset = audit_log_queryset(
        user=request.user,
        company=company,
        filters=form.cleaned_data,
    )
    paginator = Paginator(queryset, form.cleaned_data["page_size"])
    raw_page = request.GET.get("page", "1")
    try:
        page_obj = paginator.page(raw_page)
    except (PageNotAnInteger, EmptyPage):
        response = HttpResponseBadRequest("页码无效。")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    logs = list(page_obj.object_list)
    undone = set(OperationUndo.objects.filter(original_log_id__in=[log.pk for log in logs]).values_list("original_log_id", flat=True))
    projected = []
    has_business_role = bool(role_names_for(request.user).intersection({"finance", "equipment", "warehouse"}))
    for log in logs:
        item = project_audit_log(log, user=request.user)
        item["can_preview_undo"] = has_business_role and (log.object_type, log.action) in SUPPORTED and (
            log.object_type != "ImportBatch" or log.new_data_json.get("import_type") == "asset_initialization"
        )
        item["is_undone"] = log.pk in undone
        projected.append(item)
    page_obj.object_list = projected

    query_data = form.data.copy()
    query_data.pop("page", None)
    response = render(
        request,
        "audit/log_list.html",
        {
            "form": form,
            "page_obj": page_obj,
            "filter_query": query_data.urlencode(),
        },
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


__all__ = ["audit_log_list"]


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def operation_undo(request, pk):
    require_view_audit_logs(request.user)
    company = current_company()
    log = get_object_or_404(scoped_audit_logs(request.user, company), pk=pk)
    context = {"log": project_audit_log(log, user=request.user)}
    try:
        if request.method == "POST":
            undo_operation(actor=request.user, log=log, reason=request.POST.get("reason"),
                           confirmation=request.POST.get("confirmation"), request=request)
            messages.success(request, "已撤销操作，未使用的编号已释放；原操作日志已保留。")
            return redirect("audit:log-list")
        context.update(preview_undo(actor=request.user, log=log))
    except ValidationError as exc:
        context["error"] = "；".join(exc.messages)
    response = render(request, "audit/undo_confirm.html", context,
                      status=400 if request.method == "POST" and context.get("error") else 200)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
