"""Backend authorization and company scoping for Sprint 13 supplies."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied

from apps.masterdata.permissions import role_names_for

from .models import SupplyCategory, SupplyItem, SupplyItemType, SupplyWarehouse


SUPPLY_VIEW_ROLES = frozenset(
    {"system_admin", "finance", "warehouse", "equipment", "management"}
)
SUPPLY_FULL_MANAGE_ROLES = frozenset({"system_admin", "finance", "warehouse"})


def can_view_supply_master_data(user) -> bool:
    return bool(role_names_for(user).intersection(SUPPLY_VIEW_ROLES))


def require_view_supply_master_data(user) -> None:
    if not can_view_supply_master_data(user):
        raise PermissionDenied("您没有查看低值物品基础档案的权限。")


def can_manage_supply_category(user) -> bool:
    return bool(role_names_for(user).intersection(SUPPLY_FULL_MANAGE_ROLES))


def require_manage_supply_category(user) -> None:
    if not can_manage_supply_category(user):
        raise PermissionDenied("您没有维护低值物品分类的权限。")


def can_manage_supply_warehouse(user) -> bool:
    return bool(role_names_for(user).intersection(SUPPLY_FULL_MANAGE_ROLES))


def require_manage_supply_warehouse(user) -> None:
    if not can_manage_supply_warehouse(user):
        raise PermissionDenied("您没有维护低值物品仓库的权限。")


def can_manage_supply_item(user, item_type=None) -> bool:
    roles = role_names_for(user)
    if roles.intersection(SUPPLY_FULL_MANAGE_ROLES):
        return True
    return "equipment" in roles and item_type == SupplyItemType.DURABLE_QUANTITY


def require_manage_supply_item(user, item_type=None) -> None:
    if not can_manage_supply_item(user, item_type):
        raise PermissionDenied(
            "您没有维护该管理模式物品的权限；equipment 仅可维护数量型低值耐用品。"
        )


def scoped_supply_categories(user, company, queryset=None):
    queryset = queryset if queryset is not None else SupplyCategory.objects.all()
    queryset = queryset.filter(company=company)
    if not can_view_supply_master_data(user):
        return queryset.none()
    return queryset


def scoped_supply_warehouses(user, company, queryset=None):
    queryset = queryset if queryset is not None else SupplyWarehouse.objects.all()
    queryset = queryset.filter(company=company)
    if not can_view_supply_master_data(user):
        return queryset.none()
    return queryset


def scoped_supply_items(user, company, queryset=None):
    queryset = queryset if queryset is not None else SupplyItem.objects.all()
    queryset = queryset.filter(company=company)
    if not can_view_supply_master_data(user):
        return queryset.none()
    return queryset
