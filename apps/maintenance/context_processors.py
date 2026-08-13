"""Permission-scoped navigation state for preventive maintenance."""

from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import current_company, role_names_for


def maintenance_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        company = current_company()
        from apps.masterdata.models import InitializationSetting

        initialized = bool(
            company
            and InitializationSetting.objects.filter(
                company=company, initialization_completed=True
            ).exists()
        )
    except (OperationalError, ProgrammingError):
        initialized = False
    roles = role_names_for(user)
    can_view = bool(
        roles.intersection(
            {
                "finance",
                "equipment",
                "department_manager",
                "employee",
                "warehouse",
                "management",
            }
        )
    )
    return {
        "maintenance_nav": {
            "can_view": initialized and can_view,
            "can_manage": initialized and "equipment" in roles,
        }
    }
