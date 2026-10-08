"""Permission-scoped navigation state for employee asset clearance."""

from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import (
    company_initialized,
    current_company,
    role_names_for,
)


_VIEW_ROLES = frozenset(
    {
        "finance",
        "equipment",
        "department_manager",
        "employee",
        "warehouse",
        "hr",
        "management",
    }
)


def offboarding_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        initialized = company_initialized(current_company())
    except (OperationalError, ProgrammingError):
        initialized = False
    roles = role_names_for(user)
    return {
        "offboarding_nav": {
            "can_view": initialized and bool(roles.intersection(_VIEW_ROLES)),
            "can_initiate": initialized and "hr" in roles,
        }
    }
