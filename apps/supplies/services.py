"""Controlled Sprint 13 low-value supplies master-data services."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company

from .models import SupplyCategory, SupplyItem, SupplyItemType, SupplyWarehouse
from .permissions import (
    require_manage_supply_category,
    require_manage_supply_item,
    require_manage_supply_warehouse,
)


CATEGORY_FIELDS = frozenset(
    {"code", "name", "parent", "default_item_type", "remark"}
)
WAREHOUSE_FIELDS = frozenset(
    {"code", "name", "location", "manager_employee", "remark"}
)
ITEM_FIELDS = frozenset(
    {
        "item_code",
        "name",
        "category",
        "item_type",
        "unit",
        "specification",
        "model",
        "brand",
        "minimum_stock_quantity",
        "default_warehouse",
        "remark",
    }
)


def _require_current_company(company):
    active = current_company()
    if (
        company is None
        or active is None
        or getattr(company, "pk", None) != active.pk
    ):
        raise PermissionDenied("目标记录不属于当前启用公司。")
    return active


def _snapshot(instance, fields: Iterable[str]):
    data = {}
    for field in fields:
        value = getattr(instance, field)
        if hasattr(value, "pk"):
            value = value.pk
        data[field] = value
    return data


def _audit(*, actor, action, instance, old=None, new=None, request=None):
    return write_business_audit_log(
        company=instance.company,
        user=actor,
        action=action,
        object_type=instance._meta.object_name,
        object_id=instance.pk,
        old_data=old or {},
        new_data=new or {},
        **request_audit_context(request),
    )


def _apply(instance, data: Mapping, allowed_fields):
    values = dict(data)
    unknown = set(values).difference(allowed_fields)
    if unknown:
        raise ValidationError(
            {field: "不是可编辑的低值物品基础档案字段。" for field in unknown}
        )
    for field, value in values.items():
        setattr(instance, field, value)
    return instance


def _save(instance):
    instance.full_clean()
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError as exc:
        raise ValidationError("保存失败：当前公司已存在相同的规范化编码。") from exc
    return instance


def _lock_category_parent(category):
    if category.parent_id:
        category.parent = SupplyCategory.objects.select_for_update().get(
            pk=category.parent_id
        )


def _advisory_category_lock(company_id):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"supplies:category:company:{company_id}"],
            )


def supply_item_has_business_history(item) -> bool:
    """Single freeze decision point used by all item mutations.

    Sprint 13 intentionally has no posted supply document, stock-ledger or
    custody model, so no item can yet have formal business history. Sprint 14+
    extends this one predicate instead of scattering relation checks in views.
    """

    if item is None or item.pk is None:
        return False
    return False


@transaction.atomic
def create_supply_category(*, actor, company, data, request=None):
    require_manage_supply_category(actor)
    _require_current_company(company)
    _advisory_category_lock(company.pk)
    category = _apply(
        SupplyCategory(company=company, created_by=actor, updated_by=actor),
        data,
        CATEGORY_FIELDS,
    )
    _lock_category_parent(category)
    _save(category)
    _audit(
        actor=actor,
        action="supply_category_create",
        instance=category,
        new=_snapshot(
            category,
            (
                "code",
                "normalized_code",
                "name",
                "parent",
                "default_item_type",
                "is_active",
                "remark",
            ),
        ),
        request=request,
    )
    return category


@transaction.atomic
def update_supply_category(*, actor, category, data, request=None):
    require_manage_supply_category(actor)
    _require_current_company(category.company)
    _advisory_category_lock(category.company_id)
    category = SupplyCategory.objects.select_for_update().select_related(
        "company"
    ).get(pk=category.pk)
    old = _snapshot(
        category,
        (
            "code",
            "normalized_code",
            "name",
            "parent",
            "default_item_type",
            "is_active",
            "remark",
        ),
    )
    _apply(category, data, CATEGORY_FIELDS)
    category.updated_by = actor
    _lock_category_parent(category)
    _save(category)
    _audit(
        actor=actor,
        action="supply_category_update",
        instance=category,
        old=old,
        new=_snapshot(
            category,
            (
                "code",
                "normalized_code",
                "name",
                "parent",
                "default_item_type",
                "is_active",
                "remark",
            ),
        ),
        request=request,
    )
    return category


@transaction.atomic
def deactivate_supply_category(*, actor, category, reason="", request=None):
    require_manage_supply_category(actor)
    _require_current_company(category.company)
    category = SupplyCategory.objects.select_for_update().select_related(
        "company"
    ).get(pk=category.pk)
    if not category.is_active:
        return category
    category.is_active = False
    category.updated_by = actor
    category.save(update_fields=["is_active", "updated_by", "updated_at"])
    _audit(
        actor=actor,
        action="supply_category_deactivate",
        instance=category,
        old={"is_active": True},
        new={"is_active": False, "reason": str(reason or "").strip()},
        request=request,
    )
    return category


@transaction.atomic
def create_supply_warehouse(*, actor, company, data, request=None):
    require_manage_supply_warehouse(actor)
    _require_current_company(company)
    warehouse = _apply(
        SupplyWarehouse(company=company, created_by=actor, updated_by=actor),
        data,
        WAREHOUSE_FIELDS,
    )
    _save(warehouse)
    _audit(
        actor=actor,
        action="supply_warehouse_create",
        instance=warehouse,
        new=_snapshot(
            warehouse,
            (
                "code",
                "normalized_code",
                "name",
                "location",
                "manager_employee",
                "is_active",
                "remark",
            ),
        ),
        request=request,
    )
    return warehouse


@transaction.atomic
def update_supply_warehouse(*, actor, warehouse, data, request=None):
    require_manage_supply_warehouse(actor)
    _require_current_company(warehouse.company)
    warehouse = SupplyWarehouse.objects.select_for_update().select_related(
        "company"
    ).get(pk=warehouse.pk)
    old = _snapshot(
        warehouse,
        (
            "code",
            "normalized_code",
            "name",
            "location",
            "manager_employee",
            "is_active",
            "remark",
        ),
    )
    _apply(warehouse, data, WAREHOUSE_FIELDS)
    warehouse.updated_by = actor
    _save(warehouse)
    _audit(
        actor=actor,
        action="supply_warehouse_update",
        instance=warehouse,
        old=old,
        new=_snapshot(
            warehouse,
            (
                "code",
                "normalized_code",
                "name",
                "location",
                "manager_employee",
                "is_active",
                "remark",
            ),
        ),
        request=request,
    )
    return warehouse


@transaction.atomic
def deactivate_supply_warehouse(*, actor, warehouse, reason="", request=None):
    require_manage_supply_warehouse(actor)
    _require_current_company(warehouse.company)
    warehouse = SupplyWarehouse.objects.select_for_update().select_related(
        "company"
    ).get(pk=warehouse.pk)
    if not warehouse.is_active:
        return warehouse
    warehouse.is_active = False
    warehouse.updated_by = actor
    warehouse.save(update_fields=["is_active", "updated_by", "updated_at"])
    _audit(
        actor=actor,
        action="supply_warehouse_deactivate",
        instance=warehouse,
        old={"is_active": True},
        new={"is_active": False, "reason": str(reason or "").strip()},
        request=request,
    )
    return warehouse


@transaction.atomic
def create_supply_item(*, actor, company, data, request=None):
    item_type = data.get("item_type")
    require_manage_supply_item(actor, item_type)
    _require_current_company(company)
    item = _apply(
        SupplyItem(company=company, created_by=actor, updated_by=actor),
        data,
        ITEM_FIELDS,
    )
    _save(item)
    _audit(
        actor=actor,
        action="supply_item_create",
        instance=item,
        new=_snapshot(
            item,
            (
                "item_code",
                "normalized_item_code",
                "name",
                "category",
                "item_type",
                "unit",
                "specification",
                "model",
                "brand",
                "minimum_stock_quantity",
                "default_warehouse",
                "is_active",
                "remark",
            ),
        ),
        request=request,
    )
    return item


@transaction.atomic
def update_supply_item(*, actor, item, data, request=None):
    _require_current_company(item.company)
    item = SupplyItem.objects.select_for_update().select_related(
        "company", "category"
    ).get(pk=item.pk)
    requested_type = data.get("item_type", item.item_type)
    require_manage_supply_item(actor, item.item_type)
    require_manage_supply_item(actor, requested_type)
    if supply_item_has_business_history(item):
        if "item_code" in data and data["item_code"] != item.item_code:
            raise ValidationError(
                {
                    "item_code": "该物品已发生库存业务，不能修改编码；请停用后新建物品。"
                }
            )
        if "item_type" in data and data["item_type"] != item.item_type:
            raise ValidationError(
                {
                    "item_type": "该物品已发生库存业务，不能修改管理模式；请停用后新建物品。"
                }
            )
    old = _snapshot(
        item,
        (
            "item_code",
            "normalized_item_code",
            "name",
            "category",
            "item_type",
            "unit",
            "specification",
            "model",
            "brand",
            "minimum_stock_quantity",
            "default_warehouse",
            "is_active",
            "remark",
        ),
    )
    _apply(item, data, ITEM_FIELDS)
    item.updated_by = actor
    _save(item)
    _audit(
        actor=actor,
        action="supply_item_update",
        instance=item,
        old=old,
        new=_snapshot(
            item,
            (
                "item_code",
                "normalized_item_code",
                "name",
                "category",
                "item_type",
                "unit",
                "specification",
                "model",
                "brand",
                "minimum_stock_quantity",
                "default_warehouse",
                "is_active",
                "remark",
            ),
        ),
        request=request,
    )
    return item


@transaction.atomic
def deactivate_supply_item(*, actor, item, reason="", request=None):
    _require_current_company(item.company)
    item = SupplyItem.objects.select_for_update().select_related("company").get(
        pk=item.pk
    )
    require_manage_supply_item(actor, item.item_type)
    if not item.is_active:
        return item
    item.is_active = False
    item.updated_by = actor
    item.save(update_fields=["is_active", "updated_by", "updated_at"])
    _audit(
        actor=actor,
        action="supply_item_deactivate",
        instance=item,
        old={"is_active": True},
        new={"is_active": False, "reason": str(reason or "").strip()},
        request=request,
    )
    return item
