from django.db.utils import OperationalError, ProgrammingError

from apps.masterdata.permissions import current_company, role_names_for

from .models import SupplyItemType
from .permissions import (
    can_create_supply_document,
    can_create_supply_count_task,
    can_import_opening_custody,
    can_manage_supply_category,
    can_manage_supply_item,
    can_manage_supply_warehouse,
    can_post_supply_document,
    can_reverse_supply_document,
    can_view_supply_custodies,
    can_view_supply_cost,
    can_view_supply_master_data,
    can_view_supply_module,
    can_view_supply_stock,
)


def supplies_navigation(request):
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}
    try:
        company = current_company()
        can_view = company is not None and can_view_supply_module(user)
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
            "can_manage_documents": bool(
                company is not None and can_create_supply_document(user)
            ),
            "can_import_opening_custody": bool(
                company is not None and can_import_opening_custody(user)
            ),
            "can_view_master_data": bool(
                company is not None and can_view_supply_master_data(user)
            ),
            "can_view_documents": bool(
                company is not None and can_view_supply_module(user)
            ),
            "can_view_stock": bool(
                company is not None and can_view_supply_stock(user)
            ),
            "can_view_custodies": bool(
                company is not None and can_view_supply_custodies(user)
            ),
            "can_view_counts": bool(
                company is not None
                and role_names_for(user).intersection(
                    {
                        "system_admin",
                        "finance",
                        "warehouse",
                        "equipment",
                        "management",
                        "department_manager",
                        "employee",
                    }
                )
            ),
            "can_create_counts": bool(
                company is not None
                and role_names_for(user).intersection(
                    {
                        "system_admin",
                        "finance",
                        "warehouse",
                        "equipment",
                        "department_manager",
                    }
                )
            ),
            "can_post_documents": bool(
                company is not None and can_post_supply_document(user)
            ),
            "can_reverse_documents": bool(
                company is not None and can_reverse_supply_document(user)
            ),
            "can_view_cost": bool(
                company is not None and can_view_supply_cost(user)
            ),
        }
    }
