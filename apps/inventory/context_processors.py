"""Navigation state for the Sprint 8 inventory pages."""

from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import (
    current_company,
    resolve_department_ids,
    role_names_for,
)


def inventory_navigation(request):
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
                "finance",
                "equipment",
                "department_manager",
                "employee",
                "warehouse",
                "management",
            }
        )
    )
    can_create = bool(roles.intersection({"finance", "equipment"}))
    if company is not None and "department_manager" in roles:
        can_create = can_create or bool(resolve_department_ids(user, company))
    return {
        "inventory_nav": {
            "initialized": initialized,
            "can_view": initialized and can_view,
            "can_create": initialized and can_create,
        }
    }
