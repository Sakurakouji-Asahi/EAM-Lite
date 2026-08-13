"""Permission-scoped Sprint 11 report navigation state."""

from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import current_company, role_names_for


def report_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        company = current_company(include_inactive=True)
    except (OperationalError, ProgrammingError):
        company = None
    roles = role_names_for(user)
    return {
        "reports_nav": {
            "can_view": bool(
                company
                and roles.intersection(
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
            ),
            "default_report_type": (
                "offboarding_unresolved" if roles == {"hr"} else "asset_ledger"
            ),
            "can_view_tplus": bool(company and "finance" in roles),
            "can_view_external_references": bool(
                company and roles.intersection({"finance", "management"})
            ),
            "can_manage_external_references": bool(company and "finance" in roles),
        },
        "audit_nav": {
            "can_view": bool(roles.intersection({"system_admin", "finance", "hr"}))
        },
    }


__all__ = ["report_navigation"]
