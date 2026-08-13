from django.contrib.auth.decorators import login_required
from django.db.utils import OperationalError, ProgrammingError
from django.shortcuts import render

from apps.maintenance.permissions import can_complete_maintenance
from apps.maintenance.services import due_maintenance_plans
from apps.masterdata.models import InitializationSetting
from apps.masterdata.permissions import current_company
from apps.offboarding.permissions import scoped_clearance_items


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
    labels = {
        "upcoming": "即将到期",
        "due_today": "今日到期",
        "overdue": "逾期",
    }
    maintenance_items = []
    maintenance_counts = {key: 0 for key in labels}
    offboarding_unresolved_count = 0
    if initialized:
        for plan, status in due_maintenance_plans(request.user, company):
            if status not in labels:
                continue
            maintenance_counts[status] += 1
            maintenance_items.append(
                {
                    "plan": plan,
                    "due_status": status,
                    "due_label": labels[status],
                    "can_complete": can_complete_maintenance(request.user, plan),
                }
            )
        try:
            from apps.offboarding.models import EmployeeAssetClearanceItem

            offboarding_unresolved_count = scoped_clearance_items(
                request.user,
                company,
                EmployeeAssetClearanceItem.objects.filter(
                    clearance__status__in=("open", "blocked"),
                    resolution__in=("pending", "disposal_in_progress"),
                ),
            ).count()
        except (ImportError, OperationalError, ProgrammingError):
            offboarding_unresolved_count = 0
    return render(
        request,
        "core/home.html",
        {
            "initialized": initialized,
            "maintenance_items": maintenance_items,
            "maintenance_counts": maintenance_counts,
            "offboarding_unresolved_count": offboarding_unresolved_count,
        },
    )
