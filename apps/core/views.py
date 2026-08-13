from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from apps.masterdata.models import InitializationSetting
from apps.masterdata.permissions import current_company
from apps.reports.queries import build_dashboard


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
    dashboard = None
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
    response = render(
        request,
        "core/home.html",
        {
            "initialized": initialized,
            "dashboard": dashboard,
            "dashboard_filters": dashboard_filters,
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
