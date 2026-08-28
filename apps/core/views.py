from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.masterdata.models import InitializationSetting
from apps.masterdata.permissions import current_company
from apps.reports.queries import build_dashboard

from .context_processors import build_application_navigation


@never_cache
@require_GET
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("database check failed")
        media_root = Path(settings.MEDIA_ROOT)
        if not media_root.is_dir():
            raise RuntimeError("protected storage is unavailable")
        next(media_root.iterdir(), None)
        status = 200
        payload = {"status": "ok"}
    except Exception:
        status = 503
        payload = {"status": "unavailable"}
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _error_page(request, status_code, title, message):
    return render(
        request,
        "errors/error.html",
        {
            "status_code": status_code,
            "error_title": title,
            "error_message": message,
            "correlation_id": getattr(request, "correlation_id", ""),
        },
        status=status_code,
    )


def error_400(request, exception=None):
    return _error_page(request, 400, "请求无效", "请检查输入内容后重试。")


def error_403(request, exception=None):
    return _error_page(request, 403, "无权访问", "您没有执行此操作或查看此对象的权限。")


def error_404(request, exception=None):
    return _error_page(request, 404, "页面不存在", "该页面不存在，或您无权查看对应对象。")


def error_500(request):
    return _error_page(request, 500, "系统暂时不可用", "请稍后重试，并把关联标识提供给系统管理员。")


@never_cache
@login_required
def home(request):
    company = current_company()
    initialized = bool(
        company
        and InitializationSetting.objects.filter(
            company=company,
            initialization_completed=True,
        ).exists()
    )
    navigation = build_application_navigation(request)
    dashboard = None
    supply_dashboard = None
    dashboard_filters = None
    if initialized:
        dashboard = build_dashboard(actor=request.user, company=company)
        as_of = timezone.localdate()
        month_start = as_of.replace(day=1)
        if month_start.month == 12:
            next_month = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        dashboard_filters = {
            "as_of_date": as_of.isoformat(),
            "month_start": month_start.isoformat(),
            "month_end": (next_month - timedelta(days=1)).isoformat(),
        }
        if navigation.get("home", {}).get("show_supply_overview"):
            from apps.reports.supply_queries import build_supply_dashboard

            supply_dashboard = build_supply_dashboard(
                actor=request.user,
                company=company,
            )
    response = render(
        request,
        "core/home.html",
        {
            "initialized": initialized,
            "dashboard": dashboard,
            "supply_dashboard": supply_dashboard,
            "dashboard_filters": dashboard_filters,
            "app_navigation": navigation,
            # Backward-compatible home context aliases. Values come from the
            # Dashboard DTO, so there remains a single calculation source.
            "maintenance_counts": (
                dashboard.get("maintenance_counts", {})
                if dashboard
                else {"upcoming": 0, "due_today": 0, "overdue": 0}
            ),
            "maintenance_items": (
                dashboard.get("maintenance_items", ()) if dashboard else ()
            ),
            "offboarding_unresolved_count": (
                dashboard.get("pending", {}).get("offboarding_unresolved", 0)
                if dashboard
                else 0
            ),
        },
    )
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@login_required
@require_GET
def task_center(request):
    navigation = build_application_navigation(request)
    if not navigation.get("show_tasks"):
        raise PermissionDenied("您没有可处理的任务中心事项。")
    company = current_company()
    if company is None or not navigation.get("initialized"):
        raise PermissionDenied("系统初始化完成后才可进入任务中心。")

    dashboard = build_dashboard(actor=request.user, company=company)
    supply_dashboard = None
    if navigation.get("home", {}).get("show_supply_overview"):
        from apps.reports.supply_queries import build_supply_dashboard

        supply_dashboard = build_supply_dashboard(
            actor=request.user,
            company=company,
        )
    response = render(
        request,
        "core/task_center.html",
        {
            "app_navigation": navigation,
            "dashboard": dashboard,
            "supply_dashboard": supply_dashboard,
        },
    )
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@login_required
@require_GET
def settings_center(request):
    navigation = build_application_navigation(request)
    if not navigation.get("show_settings"):
        raise PermissionDenied("您没有可访问的系统设置项目。")
    response = render(
        request,
        "core/settings_center.html",
        {"app_navigation": navigation},
    )
    response["Cache-Control"] = "private, no-store"
    return response
