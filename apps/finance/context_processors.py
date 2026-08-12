"""Navigation state for Sprint 4 finance pages."""

from django.db.utils import OperationalError, ProgrammingError

from apps.finance.permissions import can_manage_finance, can_view_finance
from apps.masterdata.permissions import current_company


def finance_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        company = current_company(include_inactive=True)
    except (OperationalError, ProgrammingError):
        company = None
    return {
        "finance_nav": {
            "company": company,
            "can_view": can_view_finance(user),
            "can_manage": can_manage_finance(user),
        }
    }
