from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import (
    can_access_setup,
    can_manage_masterdata,
    can_view_masterdata,
    current_company,
    role_names_for,
)


def masterdata_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        company = current_company(include_inactive=True)
    except (OperationalError, ProgrammingError):
        # Keeps ``manage.py migrate`` and pre-migration error pages renderable.
        company = None
    return {
        "current_company": company,
        "current_role_names": sorted(role_names_for(user)),
        "masterdata_nav": {
            resource: can_view_masterdata(user, resource)
            for resource in (
                "company",
                "department",
                "employee",
                "location",
                "asset_category",
                "coding_scheme",
                "system_setting",
                "user_permissions",
            )
        },
        "masterdata_manage": {
            resource: can_manage_masterdata(user, resource)
            for resource in (
                "company",
                "department",
                "employee",
                "location",
                "asset_category",
                "coding_scheme",
                "system_setting",
                "user_permissions",
            )
        },
        "can_access_setup": can_access_setup(user),
    }
