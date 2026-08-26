"""Backend authorization and company scoping for Sprint 13 supplies."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import resolve_department_ids, role_names_for

from .models import (
    SupplyCategory,
    SupplyCustody,
    SupplyDocument,
    SupplyItem,
    SupplyItemType,
    SupplyStockBalance,
    SupplyStockLedger,
    SupplyWarehouse,
)


SUPPLY_VIEW_ROLES = frozenset(
    {"system_admin", "finance", "warehouse", "equipment", "management"}
)
SUPPLY_RELATION_VIEW_ROLES = frozenset(
    {"department_manager", "employee"}
)
SUPPLY_FULL_MANAGE_ROLES = frozenset({"system_admin", "finance", "warehouse"})
SUPPLY_DOCUMENT_MANAGE_ROLES = frozenset(
    {"system_admin", "finance", "warehouse"}
)
SUPPLY_CUSTODY_ACTION_ROLES = frozenset(
    {"system_admin", "finance", "warehouse", "equipment"}
)
SUPPLY_COST_VIEW_ROLES = frozenset(
    {"system_admin", "finance", "warehouse", "equipment", "management"}
)


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


def can_view_supply_documents(user) -> bool:
    return bool(
        role_names_for(user).intersection(
            SUPPLY_VIEW_ROLES | SUPPLY_RELATION_VIEW_ROLES
        )
    )


def require_view_supply_documents(user) -> None:
    if not can_view_supply_documents(user):
        raise PermissionDenied("您没有查看低值物品库存单据的权限。")


def _durable_return(document=None, *, document_type=None, item_types=None) -> bool:
    if document is not None:
        if document.document_type != "return":
            return False
        lines = list(document.lines.select_related("item"))
        return bool(lines) and all(
            line.item.item_type == SupplyItemType.DURABLE_QUANTITY
            and line.source_custody_id is not None
            for line in lines
        )
    return document_type == "return" and set(item_types or ()) == {
        SupplyItemType.DURABLE_QUANTITY
    }


def can_create_supply_document(
    user,
    *,
    document=None,
    document_type=None,
    item_types=None,
    source_custodies=None,
) -> bool:
    roles = role_names_for(user)
    if roles.intersection(SUPPLY_DOCUMENT_MANAGE_ROLES):
        return True
    if "equipment" in roles and _durable_return(
        document, document_type=document_type, item_types=item_types
    ):
        return True
    if (
        "department_manager" in roles
        and document is not None
        and _durable_return(document)
    ):
        custodies = tuple(
            line.source_custody
            for line in document.lines.select_related("source_custody")
        )
        return bool(custodies) and all(
            can_manage_supply_custody(user, custody, action="return_draft")
            for custody in custodies
        )
    if "department_manager" in roles and document_type == "return":
        custodies = tuple(source_custodies or ())
        return bool(custodies) and all(
            can_manage_supply_custody(user, custody, action="return_draft")
            for custody in custodies
        )
    return False


def require_create_supply_document(
    user,
    *,
    document=None,
    document_type=None,
    item_types=None,
    source_custodies=None,
) -> None:
    if not can_create_supply_document(
        user,
        document=document,
        document_type=document_type,
        item_types=item_types,
        source_custodies=source_custodies,
    ):
        raise PermissionDenied("您没有创建、编辑或取消低值物品库存单据的权限。")


def can_post_supply_document(user, *, document=None) -> bool:
    roles = role_names_for(user)
    if roles.intersection(SUPPLY_DOCUMENT_MANAGE_ROLES):
        return True
    return "equipment" in roles and _durable_return(document)


def require_post_supply_document(user, *, document=None) -> None:
    if not can_post_supply_document(user, document=document):
        raise PermissionDenied("您没有过账低值物品库存单据的权限。")


def can_reverse_supply_document(user, *, document=None) -> bool:
    roles = role_names_for(user)
    if roles.intersection(SUPPLY_DOCUMENT_MANAGE_ROLES):
        return True
    return "equipment" in roles and _durable_return(document)


def require_reverse_supply_document(user, *, document=None) -> None:
    if not can_reverse_supply_document(user, document=document):
        raise PermissionDenied("您没有冲销低值物品库存单据的权限。")


def can_manage_supply_custody(
    user, custody, *, action=None, target_department=None
) -> bool:
    roles = role_names_for(user)
    if roles.intersection(SUPPLY_CUSTODY_ACTION_ROLES):
        return True
    if "department_manager" not in roles:
        return False
    scoped_ids = resolve_department_ids(user, custody.company)
    if custody.department_id not in scoped_ids:
        return False
    if action == "return_post":
        return False
    if action == "transfer" and target_department is not None:
        return target_department.pk in scoped_ids
    return action in {"return_draft", "transfer", "loss", "scrap"}


def require_manage_supply_custody(
    user, custody, *, action=None, target_department=None
) -> None:
    if not can_manage_supply_custody(
        user,
        custody,
        action=action,
        target_department=target_department,
    ):
        raise PermissionDenied("您没有在当前公司和部门范围内执行该耐用品保管动作的权限。")


def can_import_opening_custody(user) -> bool:
    return bool(role_names_for(user).intersection(SUPPLY_CUSTODY_ACTION_ROLES))


def require_import_opening_custody(user) -> None:
    if not can_import_opening_custody(user):
        raise PermissionDenied("您没有导入耐用品期初保管的权限。")


def can_view_supply_stock(user) -> bool:
    return bool(role_names_for(user).intersection(SUPPLY_VIEW_ROLES))


def require_view_supply_stock(user) -> None:
    if not can_view_supply_stock(user):
        raise PermissionDenied("您没有查看公司仓库库存或流水的权限。")


def can_view_supply_custodies(user) -> bool:
    return can_view_supply_documents(user)


def require_view_supply_custodies(user) -> None:
    if not can_view_supply_custodies(user):
        raise PermissionDenied("您没有查看数量型低值耐用品保管的权限。")


def can_view_supply_module(user) -> bool:
    return can_view_supply_master_data(user) or can_view_supply_documents(user)


def require_view_supply_module(user) -> None:
    if not can_view_supply_module(user):
        raise PermissionDenied("您没有查看低值物品模块的权限。")


def can_view_supply_cost(user) -> bool:
    return bool(role_names_for(user).intersection(SUPPLY_COST_VIEW_ROLES))


def require_view_supply_cost(user) -> None:
    if not can_view_supply_cost(user):
        raise PermissionDenied("您没有查看低值物品库存成本的权限。")


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


def scoped_supply_documents(user, company, queryset=None):
    queryset = queryset if queryset is not None else SupplyDocument.objects.all()
    queryset = queryset.filter(company=company)
    roles = role_names_for(user)
    if roles.intersection(SUPPLY_VIEW_ROLES):
        return queryset
    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(
            document_type__in=("issue", "return"),
            department_id__in=resolve_department_ids(user, company),
        )
    if "employee" in roles:
        filters |= Q(
            document_type__in=("issue", "return"),
            employee__user=user,
        )
    return queryset.filter(filters).distinct()


def scoped_supply_stock_balances(user, company, queryset=None):
    queryset = queryset if queryset is not None else SupplyStockBalance.objects.all()
    queryset = queryset.filter(company=company)
    if not can_view_supply_stock(user):
        return queryset.none()
    return queryset


def scoped_supply_stock_ledgers(user, company, queryset=None):
    queryset = queryset if queryset is not None else SupplyStockLedger.objects.all()
    queryset = queryset.filter(company=company)
    if not can_view_supply_stock(user):
        return queryset.none()
    return queryset


def scoped_supply_custodies(user, company, queryset=None):
    queryset = queryset if queryset is not None else SupplyCustody.objects.all()
    queryset = queryset.filter(company=company)
    roles = role_names_for(user)
    if roles.intersection(SUPPLY_VIEW_ROLES):
        return queryset
    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(department_id__in=resolve_department_ids(user, company))
    if "employee" in roles:
        filters |= Q(employee__user=user)
    return queryset.filter(filters).distinct()
