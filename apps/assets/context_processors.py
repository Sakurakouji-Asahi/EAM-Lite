"""Navigation state for the Sprint 3 asset pages."""

from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import current_company, role_names_for


def asset_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        company = current_company()
        if company is None:
            initialized = False
        else:
            from apps.masterdata.models import InitializationSetting

            initialized = InitializationSetting.objects.filter(
                company=company, initialization_completed=True
            ).exists()
    except (OperationalError, ProgrammingError):
        company = None
        initialized = False
    roles = role_names_for(user)
    can_view = bool(
        roles.intersection(
            {
                "system_admin",
                "finance",
                "equipment",
                "department_manager",
                "employee",
                "warehouse",
                "hr",
                "management",
            }
        )
    )
    can_create = bool(
        roles.intersection({"finance", "equipment", "warehouse", "department_manager"})
    )
    return {
        "asset_nav": {
            "initialized": initialized,
            "can_view": can_view,
            "can_create": can_create,
        }
    }
