from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import current_company

from .models import SupplyItemType
from .permissions import (
    can_manage_supply_category,
    can_manage_supply_item,
    can_manage_supply_warehouse,
    can_view_supply_master_data,
)


def supplies_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        company = current_company()
        can_view = company is not None and can_view_supply_master_data(user)
    except (OperationalError, ProgrammingError):
        company = None
        can_view = False
    return {
        "supplies_nav": {
            "can_view": can_view,
            "can_manage_categories": bool(
                company is not None and can_manage_supply_category(user)
            ),
            "can_manage_warehouses": bool(
                company is not None and can_manage_supply_warehouse(user)
            ),
            "can_manage_items": bool(
                company is not None
                and can_manage_supply_item(
                    user, SupplyItemType.DURABLE_QUANTITY
                )
            ),
        }
    }
