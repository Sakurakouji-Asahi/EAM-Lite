"""Read-only HTTP endpoint for permission-scoped audit logs."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.audit.forms import AuditLogFilterForm
from apps.audit.permissions import require_view_audit_logs
from apps.audit.query import (
    audit_log_queryset,
    project_audit_log,
    visible_audit_actors,
)
from apps.masterdata.permissions import current_company


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
    page_obj.object_list = [
        project_audit_log(log, user=request.user) for log in page_obj.object_list
    ]

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
