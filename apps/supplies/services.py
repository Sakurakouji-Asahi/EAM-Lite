"""Controlled low-value supplies master-data, stock and custody services."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q, Sum
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company

from .domain import (
    ZERO_COST,
    ZERO_MONEY,
    ZERO_QTY,
    allocate_custody_amount,
    calculate_issue,
    calculate_receipt,
    calculate_receipt_from_amount,
    quantize_money,
    quantize_quantity,
    quantize_unit_cost,
    validate_zero_cost_reason,
)
from .models import (
    EmployeeSupplyClearanceItem,
    EmployeeSupplyClearanceResolution,
    SupplyCategory,
    SupplyCountDomain,
    SupplyCountLine,
    SupplyCountResolutionType,
    SupplyCountStatus,
    SupplyCountTask,
    SupplyCustody,
    SupplyCustodyAction,
    SupplyCustodyMovement,
    SupplyCustodyStatus,
    SupplyDocument,
    SupplyDocumentLine,
    SupplyDocumentSequence,
    SupplyDocumentStatus,
    SupplyDocumentType,
    SupplyItem,
    SupplyItemType,
    SupplyStockBalance,
    SupplyStockLedger,
    SupplyStockMovementType,
    SupplyWarehouse,
)
from .permissions import (
    require_create_supply_count_task,
    require_execute_supply_count_task,
    require_create_supply_document,
    require_manage_supply_category,
    require_manage_supply_item,
    require_manage_supply_custody,
    require_import_opening_custody,
    require_manage_supply_warehouse,
    require_post_supply_document,
    require_record_supply_count,
    require_reverse_supply_document,
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
DOCUMENT_DRAFT_FIELDS = frozenset(
    {
        "business_date",
        "source_warehouse",
        "target_warehouse",
        "department",
        "employee",
        "external_reference",
        "counterparty_name",
        "remark",
    }
)
DOCUMENT_LINE_FIELDS = frozenset(
    {
        "item",
        "quantity",
        "entered_unit_cost",
        "adjustment_direction",
        "source_issue_line",
        "source_custody",
        "line_remark",
    }
)
SPRINT15_DOCUMENT_TYPES = frozenset(
    {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
        SupplyDocumentType.ISSUE,
        SupplyDocumentType.RETURN,
        SupplyDocumentType.TRANSFER,
    }
)
DOCUMENT_PREFIXES = {
    SupplyDocumentType.OPENING: "QC",
    SupplyDocumentType.RECEIPT: "RK",
    SupplyDocumentType.ISSUE: "LY",
    SupplyDocumentType.RETURN: "TH",
    SupplyDocumentType.TRANSFER: "DB",
    SupplyDocumentType.COUNT_ADJUSTMENT: "PD",
    SupplyDocumentType.REVERSAL: "CX",
    "count_task": "PDRW",
}
ACTIVE_SUPPLY_COUNT_STATUSES = frozenset(
    {SupplyCountStatus.IN_PROGRESS, SupplyCountStatus.RECONCILIATION}
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

    A posted line or immutable stock-ledger row freezes the item code and
    management mode.  Future custody history is added to this same predicate.
    """

    if item is None or item.pk is None:
        return False
    return (
        item.stock_ledgers.exists()
        or item.custody_movements.exists()
        or item.document_lines.filter(
            document__status__in=(
                SupplyDocumentStatus.POSTED,
                SupplyDocumentStatus.REVERSED,
            )
        ).exists()
    )


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


def _enable_capability(name):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                [f"eam_lite.{name}", "on"],
            )


def _base_update(model, pk, values, capability):
    _enable_capability(capability)
    updated = QuerySet.update(model._base_manager.filter(pk=pk), **values)
    if updated != 1:
        raise ValidationError("受控库存更新未命中唯一记录。")


def _coerce_business_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError({"business_date": "业务日期必须是有效日期。"}) from exc


def _document_line_snapshot(line):
    return {
        "line_no": line.line_no,
        "item_id": str(line.item_id),
        "quantity": str(line.quantity),
        "entered_unit_cost": (
            str(line.entered_unit_cost)
            if line.entered_unit_cost is not None
            else None
        ),
        "posted_unit_cost": (
            str(line.posted_unit_cost) if line.posted_unit_cost is not None else None
        ),
        "posted_amount": (
            str(line.posted_amount) if line.posted_amount is not None else None
        ),
        "source_issue_line_id": (
            str(line.source_issue_line_id) if line.source_issue_line_id else None
        ),
        "source_custody_id": (
            str(line.source_custody_id) if line.source_custody_id else None
        ),
        "line_remark": line.line_remark,
    }


def _document_snapshot(document):
    return {
        "document_no": document.document_no,
        "document_type": document.document_type,
        "business_date": document.business_date.isoformat(),
        "source_warehouse_id": (
            str(document.source_warehouse_id)
            if document.source_warehouse_id
            else None
        ),
        "target_warehouse_id": (
            str(document.target_warehouse_id)
            if document.target_warehouse_id
            else None
        ),
        "department_id": str(document.department_id) if document.department_id else None,
        "employee_id": str(document.employee_id) if document.employee_id else None,
        "source_count_task_id": (
            str(document.source_count_task_id)
            if document.source_count_task_id
            else None
        ),
        "external_reference": document.external_reference,
        "counterparty_name": document.counterparty_name,
        "remark": document.remark,
        "status": document.status,
        "lines": [
            _document_line_snapshot(line)
            for line in document.lines.select_related("item").order_by("line_no")
        ],
    }


def _prepare_document_lines(*, company, document_type, lines):
    prepared = []
    seen_return_sources = set()
    return_modes = set()
    for line_no, source in enumerate(lines or (), 1):
        values = dict(source)
        unknown = set(values).difference(DOCUMENT_LINE_FIELDS)
        if unknown:
            raise ValidationError(
                {
                    field: "不是当前库存单据可编辑的明细字段。"
                    for field in unknown
                }
            )
        item = values.get("item")
        source_issue_line = values.get("source_issue_line")
        source_custody = values.get("source_custody")
        if document_type == SupplyDocumentType.RETURN:
            if source_custody is not None:
                source_custody = (
                    SupplyCustody.objects.select_related(
                        "item", "origin_issue_line", "department", "employee"
                    )
                    .filter(pk=source_custody.pk, company=company)
                    .first()
                )
                if source_custody is None:
                    raise ValidationError(
                        {"source_custody": f"第 {line_no} 行来源保管不属于当前公司。"}
                    )
                if source_custody.item.item_type != SupplyItemType.DURABLE_QUANTITY:
                    raise ValidationError({"source_custody": "来源保管必须是数量型低值耐用品。"})
                if source_custody.status != SupplyCustodyStatus.OPEN:
                    raise ValidationError({"source_custody": "只有开放保管可以发起归还。"})
                if source_issue_line is not None:
                    source_issue_line = (
                        SupplyDocumentLine.objects.select_related("document", "item")
                        .filter(pk=source_issue_line.pk, company=company)
                        .first()
                    )
                    if (
                        source_issue_line is None
                        or source_custody.origin_issue_line_id
                        != source_issue_line.pk
                    ):
                        raise ValidationError(
                            {"source_issue_line": "原领用明细不是来源保管的直接根来源。"}
                        )
                if item is not None and item.pk != source_custody.item_id:
                    raise ValidationError({"item": "归还物品由来源保管确定，不能替换。"})
                item = source_custody.item
                source_key = ("durable", source_custody.pk)
                return_modes.add("durable")
            else:
                if source_issue_line is None:
                    raise ValidationError(
                        {"source_issue_line": f"第 {line_no} 行必须关联原领用明细或来源保管。"}
                    )
                source_issue_line = (
                    SupplyDocumentLine.objects.select_related("document", "item")
                    .filter(pk=source_issue_line.pk, company=company)
                    .first()
                )
                if source_issue_line is None:
                    raise ValidationError(
                        {"source_issue_line": f"第 {line_no} 行原领用明细不属于当前公司。"}
                    )
                if (
                    source_issue_line.document.document_type
                    != SupplyDocumentType.ISSUE
                    or source_issue_line.document.status
                    != SupplyDocumentStatus.POSTED
                    or source_issue_line.item.item_type != SupplyItemType.CONSUMABLE
                ):
                    raise ValidationError(
                        {"source_issue_line": f"第 {line_no} 行只能关联有效的易耗品领用明细。"}
                    )
                if item is not None and item.pk != source_issue_line.item_id:
                    raise ValidationError({"item": "退回物品由原领用明细确定，不能替换。"})
                item = source_issue_line.item
                source_key = ("consumable", source_issue_line.pk)
                return_modes.add("consumable")
            if source_key in seen_return_sources:
                raise ValidationError("同一退回单不能重复引用同一来源。")
            seen_return_sources.add(source_key)
        elif source_issue_line is not None:
            raise ValidationError(
                {"source_issue_line": f"第 {line_no} 行当前单据类型不得关联原领用明细。"}
            )
        if document_type != SupplyDocumentType.RETURN and source_custody is not None:
            raise ValidationError(
                {"source_custody": f"第 {line_no} 行当前单据类型不得关联来源保管。"}
            )
        if item is None:
            raise ValidationError({"item": f"第 {line_no} 行必须选择物品。"})
        if item.company_id != company.pk:
            raise ValidationError({"item": f"第 {line_no} 行物品不属于当前公司。"})
        if not item.is_active:
            raise ValidationError({"item": f"第 {line_no} 行物品已停用，不能用于新单据。"})
        quantity = quantize_quantity(values.get("quantity"))
        if quantity <= ZERO_QTY:
            raise ValidationError({"quantity": f"第 {line_no} 行数量必须大于 0。"})
        entered_unit_cost = values.get("entered_unit_cost")
        line_remark = str(values.get("line_remark") or "").strip()
        if document_type in {
            SupplyDocumentType.OPENING,
            SupplyDocumentType.RECEIPT,
        }:
            if entered_unit_cost is None:
                raise ValidationError(
                    {"entered_unit_cost": f"第 {line_no} 行必须填写单位成本。"}
                )
            entered_unit_cost = quantize_unit_cost(entered_unit_cost)
            if entered_unit_cost < ZERO_COST:
                raise ValidationError(
                    {"entered_unit_cost": f"第 {line_no} 行单位成本不得小于 0。"}
                )
            line_remark = validate_zero_cost_reason(
                entered_unit_cost, line_remark
            )
        else:
            if entered_unit_cost is not None:
                raise ValidationError(
                    {"entered_unit_cost": f"第 {line_no} 行成本只能由系统计算，用户不得录入。"}
                )
            entered_unit_cost = None
        if document_type == SupplyDocumentType.RETURN and not line_remark:
            raise ValidationError({"line_remark": f"第 {line_no} 行退回原因不能为空。"})
        if values.get("adjustment_direction"):
            raise ValidationError(f"第 {line_no} 行包含本 Sprint 尚未开放的盘点字段。")
        prepared.append(
            {
                "line_no": line_no,
                "item": item,
                "quantity": quantity,
                "entered_unit_cost": entered_unit_cost,
                "adjustment_direction": None,
                "source_issue_line": source_issue_line,
                "source_custody": source_custody,
                "line_remark": line_remark,
            }
        )
    if not prepared:
        raise ValidationError("库存单据至少需要一条有效明细。")
    if document_type == SupplyDocumentType.RETURN:
        if len(return_modes) != 1:
            raise ValidationError("同一退回单不得混合易耗品退回和耐用品保管归还。")
        if "durable" in return_modes and len(prepared) != 1:
            raise ValidationError("一张耐用品归还单只能对应一个来源保管。")
    return prepared


def _create_document_lines(*, document, prepared_lines):
    created = []
    for values in prepared_lines:
        line = SupplyDocumentLine(
            company=document.company,
            document=document,
            **values,
        )
        line.full_clean()
        if document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT:
            _enable_capability("controlled_supply_count_adjustment_line_insert")
        try:
            line.save(force_insert=True)
        except IntegrityError as exc:
            raise ValidationError("单据明细行号或业务字段冲突。") from exc
        created.append(line)
    return created


def _next_supply_document_no(*, company, document_type, business_date):
    from apps.masterdata.models import Company

    if document_type not in DOCUMENT_PREFIXES:
        raise ValidationError("当前单据类型没有启用编号规则。")
    Company.objects.select_for_update().get(pk=company.pk)
    _enable_capability("controlled_supply_sequence_increment")
    sequence, _ = SupplyDocumentSequence.objects.select_for_update().get_or_create(
        company=company,
        sequence_type=document_type,
        year=business_date.year,
        defaults={"current_value": 0},
    )
    sequence.current_value += 1
    sequence.full_clean()
    _enable_capability("controlled_supply_sequence_increment")
    sequence.save(update_fields=["current_value", "updated_at"])
    return f"{DOCUMENT_PREFIXES[document_type]}-{business_date.year}-{sequence.current_value:06d}"


def _assert_idempotent_document_matches(
    *, existing, document_type, data, prepared_lines
):
    expected_header = {
        "document_type": document_type,
        "business_date": data["business_date"],
        "source_warehouse_id": getattr(data.get("source_warehouse"), "pk", None),
        "target_warehouse_id": getattr(data.get("target_warehouse"), "pk", None),
        "department_id": getattr(data.get("department"), "pk", None),
        "employee_id": getattr(data.get("employee"), "pk", None),
        "external_reference": str(data.get("external_reference") or "").strip(),
        "counterparty_name": str(data.get("counterparty_name") or "").strip(),
        "remark": str(data.get("remark") or "").strip(),
    }
    actual_header = {
        "document_type": existing.document_type,
        "business_date": existing.business_date,
        "source_warehouse_id": existing.source_warehouse_id,
        "target_warehouse_id": existing.target_warehouse_id,
        "department_id": existing.department_id,
        "employee_id": existing.employee_id,
        "external_reference": existing.external_reference,
        "counterparty_name": existing.counterparty_name,
        "remark": existing.remark,
    }
    actual_lines = list(existing.lines.order_by("line_no"))
    lines_match = len(actual_lines) == len(prepared_lines) and all(
        actual.item_id == expected["item"].pk
        and actual.quantity == expected["quantity"]
        and actual.entered_unit_cost == expected["entered_unit_cost"]
        and actual.source_issue_line_id
        == getattr(expected.get("source_issue_line"), "pk", None)
        and actual.source_custody_id
        == getattr(expected.get("source_custody"), "pk", None)
        and actual.line_remark == expected["line_remark"]
        for actual, expected in zip(actual_lines, prepared_lines, strict=True)
    )
    if actual_header != expected_header or not lines_match:
        raise ValidationError("同一单据幂等键已用于不同内容。")


@transaction.atomic
def create_supply_document(
    *, actor, company, data, lines, document_type=None, request=None
):
    values = dict(data)
    supplied_type = values.pop("document_type", None)
    document_type = document_type or supplied_type
    if supplied_type and document_type != supplied_type:
        raise ValidationError("单据类型参数相互冲突。")
    if document_type not in SPRINT15_DOCUMENT_TYPES:
        raise ValidationError("当前 Sprint 不允许手工创建该单据类型。")
    _require_current_company(company)
    unknown = set(values).difference(DOCUMENT_DRAFT_FIELDS | {"idempotency_key"})
    if unknown:
        raise ValidationError(
            {field: "不是当前库存单据可编辑字段。" for field in unknown}
        )
    values["business_date"] = _coerce_business_date(values.get("business_date"))
    idempotency_key = str(values.pop("idempotency_key", "") or "").strip()
    if not idempotency_key:
        raise ValidationError({"idempotency_key": "创建幂等键不能为空。"})
    prepared_lines = _prepare_document_lines(
        company=company,
        document_type=document_type,
        lines=lines,
    )
    require_create_supply_document(
        actor,
        document_type=document_type,
        item_types={line["item"].item_type for line in prepared_lines},
        source_custodies={
            line["source_custody"]
            for line in prepared_lines
            if line["source_custody"] is not None
        },
    )

    source = values.get("source_warehouse")
    target = values.get("target_warehouse")
    department = values.get("department")
    employee = values.get("employee")
    for field_name, value in (
        ("source_warehouse", source),
        ("target_warehouse", target),
        ("department", department),
        ("employee", employee),
    ):
        if value is not None and value.company_id != company.pk:
            raise ValidationError({field_name: "所选对象不属于当前公司。"})
    if document_type in {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
    }:
        if target is None:
            raise ValidationError({"target_warehouse": "必须选择目标仓库。"})
        if source is not None or department is not None or employee is not None:
            raise ValidationError("期初和日常入库不得填写来源仓库、部门或员工。")
    elif document_type == SupplyDocumentType.ISSUE:
        if source is None:
            raise ValidationError({"source_warehouse": "领用单必须选择来源仓库。"})
        if target is not None:
            raise ValidationError({"target_warehouse": "领用单不得填写目标仓库。"})
        if department is None:
            raise ValidationError({"department": "领用单必须选择领用部门。"})
        if not department.is_active:
            raise ValidationError({"department": "领用部门必须处于启用状态。"})
        if employee is not None:
            if employee.department_id != department.pk:
                raise ValidationError({"employee": "领用员工不属于所选部门。"})
            if (
                employee.employment_status != "active"
                or not employee.is_active
                or not employee.department.is_active
            ):
                raise ValidationError({"employee": "领用员工必须是在职、启用且属于启用部门。"})
    elif document_type == SupplyDocumentType.RETURN:
        if source is not None:
            raise ValidationError({"source_warehouse": "退回单不得填写来源仓库。"})
        if target is None:
            raise ValidationError({"target_warehouse": "退回单必须选择目标仓库。"})
        source_headers = set()
        for line in prepared_lines:
            if line["source_custody"] is not None:
                source_headers.add(
                    (
                        line["source_custody"].department_id,
                        line["source_custody"].employee_id,
                    )
                )
            else:
                source_headers.add(
                    (
                        line["source_issue_line"].document.department_id,
                        line["source_issue_line"].document.employee_id,
                    )
                )
        if len(source_headers) != 1:
            raise ValidationError("一张退回单的原领用明细必须属于同一部门和员工。")
        expected_department_id, expected_employee_id = source_headers.pop()
        from apps.masterdata.models import Department, Employee

        expected_department = Department.objects.filter(
            pk=expected_department_id, company=company
        ).first()
        expected_employee = (
            Employee.objects.filter(pk=expected_employee_id, company=company).first()
            if expected_employee_id
            else None
        )
        if expected_department is None:
            raise ValidationError("原领用部门快照已不可用，不能建立退回单。")
        if department is not None and department.pk != expected_department_id:
            raise ValidationError({"department": "退回部门必须沿用原领用部门。"})
        if employee is not None and employee.pk != expected_employee_id:
            raise ValidationError({"employee": "退回员工必须沿用原领用员工。"})
        values["department"] = expected_department
        values["employee"] = expected_employee
    elif document_type == SupplyDocumentType.TRANSFER:
        if source is None or target is None:
            raise ValidationError("调拨单必须同时选择来源仓库和目标仓库。")
        if source.pk == target.pk:
            raise ValidationError("来源仓库和目标仓库不能相同。")
        if department is not None or employee is not None:
            raise ValidationError("调拨单不得填写部门或员工。")
    for field_name, warehouse in (
        ("source_warehouse", source),
        ("target_warehouse", target),
    ):
        if warehouse is not None and not warehouse.is_active:
            raise ValidationError({field_name: "停用仓库不能用于新增业务单据。"})

    from apps.masterdata.models import Company

    Company.objects.select_for_update().get(pk=company.pk)
    existing = (
        SupplyDocument.objects.select_for_update()
        .filter(company=company, idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        _assert_idempotent_document_matches(
            existing=existing,
            document_type=document_type,
            data=values,
            prepared_lines=prepared_lines,
        )
        return existing
    document = SupplyDocument(
        company=company,
        document_no=_next_supply_document_no(
            company=company,
            document_type=document_type,
            business_date=values["business_date"],
        ),
        document_type=document_type,
        idempotency_key=idempotency_key,
        created_by=actor,
        **values,
    )
    document.full_clean()
    try:
        document.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("库存单据编号或幂等键冲突。") from exc
    _create_document_lines(document=document, prepared_lines=prepared_lines)
    _audit(
        actor=actor,
        action="supply_document_create",
        instance=document,
        new=_document_snapshot(document),
        request=request,
    )
    return document


@transaction.atomic
def update_draft_document(*, actor, document, data, lines, request=None):
    _require_current_company(document.company)
    document = (
        SupplyDocument.objects.select_for_update(of=("self",))
        .select_related("company", "target_warehouse")
        .get(pk=document.pk, company=document.company)
    )
    require_create_supply_document(actor, document=document)
    if document.status != SupplyDocumentStatus.DRAFT:
        raise ValidationError("该单据已过账或取消，不能编辑。")
    unknown = set(data).difference(DOCUMENT_DRAFT_FIELDS)
    if unknown:
        raise ValidationError({field: "不是可编辑的草稿字段。" for field in unknown})
    prepared_lines = _prepare_document_lines(
        company=document.company,
        document_type=document.document_type,
        lines=lines,
    )
    old = _document_snapshot(document)
    values = dict(data)
    if "business_date" in values:
        values["business_date"] = _coerce_business_date(values["business_date"])
    source = values.get("source_warehouse", document.source_warehouse)
    target = values.get("target_warehouse", document.target_warehouse)
    department = values.get("department", document.department)
    employee = values.get("employee", document.employee)
    for field_name, value in (
        ("source_warehouse", source),
        ("target_warehouse", target),
        ("department", department),
        ("employee", employee),
    ):
        if value is not None and value.company_id != document.company_id:
            raise ValidationError({field_name: "所选对象必须属于当前公司。"})
    if document.document_type in {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
    }:
        if target is None or source is not None or department is not None or employee is not None:
            raise ValidationError("期初和日常入库必须只填写目标仓库。")
    elif document.document_type == SupplyDocumentType.ISSUE:
        if source is None or target is not None or department is None:
            raise ValidationError("领用单必须填写来源仓库和部门，且不得填写目标仓库。")
        if not department.is_active:
            raise ValidationError({"department": "领用部门必须处于启用状态。"})
        if employee is not None and (
            employee.department_id != department.pk
            or employee.employment_status != "active"
            or not employee.is_active
            or not employee.department.is_active
        ):
            raise ValidationError({"employee": "领用员工必须属于所选部门且在职启用。"})
    elif document.document_type == SupplyDocumentType.TRANSFER:
        if source is None or target is None or source.pk == target.pk:
            raise ValidationError("调拨来源、目标仓库必须填写且不能相同。")
        if department is not None or employee is not None:
            raise ValidationError("调拨单不得填写部门或员工。")
    elif document.document_type == SupplyDocumentType.RETURN:
        if source is not None or target is None:
            raise ValidationError("退回单必须只填写目标仓库。")
        source_headers = {
            (
                line["source_issue_line"].document.department_id,
                line["source_issue_line"].document.employee_id,
            )
            for line in prepared_lines
        }
        if len(source_headers) != 1:
            raise ValidationError("一张退回单的原领用明细必须属于同一部门和员工。")
        expected_department_id, expected_employee_id = source_headers.pop()
        if (
            getattr(department, "pk", None) != expected_department_id
            or getattr(employee, "pk", None) != expected_employee_id
        ):
            raise ValidationError("退回部门和员工必须沿用原领用单。")
    for field_name, warehouse in (
        ("source_warehouse", source),
        ("target_warehouse", target),
    ):
        if warehouse is not None and not warehouse.is_active:
            raise ValidationError({field_name: "停用仓库不能用于新增业务单据。"})
    _apply(document, values, DOCUMENT_DRAFT_FIELDS)
    document.full_clean()
    document.save()
    document.lines.all().delete()
    _create_document_lines(document=document, prepared_lines=prepared_lines)
    _audit(
        actor=actor,
        action="supply_document_update",
        instance=document,
        old=old,
        new=_document_snapshot(document),
        request=request,
    )
    return document


@transaction.atomic
def cancel_supply_document(*, actor, document, reason, request=None):
    _require_current_company(document.company)
    document = (
        SupplyDocument.objects.select_for_update(of=("self",))
        .select_related("company")
        .get(pk=document.pk, company=document.company)
    )
    require_create_supply_document(actor, document=document)
    if document.status == SupplyDocumentStatus.CANCELLED:
        return document
    if document.status != SupplyDocumentStatus.DRAFT:
        raise ValidationError("只有草稿单据可以取消；已过账单据不能普通取消。")
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ValidationError({"reason": "取消原因不能为空。"})
    old = _document_snapshot(document)
    document.status = SupplyDocumentStatus.CANCELLED
    document.cancelled_by = actor
    document.cancelled_at = timezone.now()
    document.cancellation_reason = cleaned_reason
    document._controlled_transition = True
    _enable_capability("controlled_supply_document_transition")
    document.full_clean()
    document.save(
        update_fields=(
            "status",
            "cancelled_by",
            "cancelled_at",
            "cancellation_reason",
            "updated_at",
        )
    )
    _audit(
        actor=actor,
        action="supply_document_cancel",
        instance=document,
        old=old,
        new=_document_snapshot(document),
        request=request,
    )
    return document


def _lock_or_create_balance(*, company, warehouse, item):
    balance = (
        SupplyStockBalance.objects.select_for_update()
        .filter(company=company, warehouse=warehouse, item=item)
        .first()
    )
    if balance is not None:
        return balance
    try:
        with transaction.atomic():
            balance = SupplyStockBalance(
                company=company,
                warehouse=warehouse,
                item=item,
                quantity_on_hand=ZERO_QTY,
                amount_on_hand=ZERO_MONEY,
                average_unit_cost=ZERO_COST,
            )
            balance._controlled_mutation = True
            _enable_capability("controlled_supply_balance_mutation")
            balance.full_clean()
            balance.save(force_insert=True)
    except IntegrityError:
        balance = (
            SupplyStockBalance.objects.select_for_update()
            .filter(company=company, warehouse=warehouse, item=item)
            .first()
        )
        if balance is None:
            raise
    return balance


def _update_balance(*, balance, calculation, updated_at):
    _update_balance_values(
        balance=balance,
        quantity=calculation.quantity_after,
        amount=calculation.amount_after,
        average_unit_cost=calculation.average_unit_cost_after,
        updated_at=updated_at,
    )


def _update_balance_values(
    *, balance, quantity, amount, average_unit_cost, updated_at
):
    values = {
        "quantity_on_hand": quantize_quantity(quantity),
        "amount_on_hand": quantize_money(amount),
        "average_unit_cost": quantize_unit_cost(average_unit_cost),
        "updated_at": updated_at,
    }
    _base_update(
        SupplyStockBalance,
        balance.pk,
        values,
        "controlled_supply_balance_mutation",
    )
    for field, value in values.items():
        setattr(balance, field, value)


def _create_stock_ledger(*, values):
    ledger = SupplyStockLedger(**values)
    ledger._controlled_insert = True
    _enable_capability("controlled_supply_ledger_insert")
    # SQLite evaluates Decimal arithmetic in constraint validation through
    # binary numeric affinity and can reject exact values such as
    # 33.37 - 13.35 = 20.02.  Validate the equations with Decimal here; the
    # tracked database constraints still enforce them on PostgreSQL.
    ledger.full_clean(validate_constraints=False)
    if ledger.quantity_after != quantize_quantity(
        ledger.quantity_before + ledger.quantity_delta
    ):
        raise ValidationError("库存流水数量前后快照不满足变动等式。")
    if ledger.amount_after != quantize_money(
        ledger.amount_before + ledger.amount_delta
    ):
        raise ValidationError("库存流水金额前后快照不满足变动等式。")
    if (
        ledger.quantity_before < ZERO_QTY
        or ledger.quantity_after < ZERO_QTY
        or ledger.amount_before < ZERO_MONEY
        or ledger.amount_after < ZERO_MONEY
        or ledger.unit_cost < ZERO_COST
        or ledger.average_unit_cost_before < ZERO_COST
        or ledger.average_unit_cost_after < ZERO_COST
        or ledger.quantity_delta == ZERO_QTY
    ):
        raise ValidationError("库存流水包含非法的负余额、成本或零数量变动。")
    try:
        ledger.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("该过账明细已存在同方向库存流水，已阻止重复过账。") from exc
    return ledger


def _create_custody(*, values):
    custody = SupplyCustody(**values)
    custody._controlled_mutation = True
    _enable_capability("controlled_supply_custody_mutation")
    custody.full_clean()
    try:
        custody.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("该耐用品根来源或保管关系已经建立，不能重复创建。") from exc
    return custody


def _create_custody_movement(*, values):
    idempotency_key = str(values.get("idempotency_key") or "").strip() or None
    values["idempotency_key"] = idempotency_key
    movement = SupplyCustodyMovement(**values)
    movement._controlled_insert = True
    _enable_capability("controlled_supply_custody_movement_insert")
    movement.full_clean()
    try:
        movement.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("该保管动作已经存在，已阻止重复写入。") from exc
    return movement


def _update_custody_values(*, custody, quantity, amount, status, updated_at):
    values = {
        "current_quantity": quantize_quantity(quantity),
        "current_amount": quantize_money(amount),
        "status": status,
        "updated_at": updated_at,
    }
    _base_update(
        SupplyCustody,
        custody.pk,
        values,
        "controlled_supply_custody_mutation",
    )
    for field, value in values.items():
        setattr(custody, field, value)


def _custody_snapshot(custody):
    return {
        "custody_id": str(custody.pk),
        "item_id": str(custody.item_id),
        "department_id": str(custody.department_id),
        "employee_id": str(custody.employee_id) if custody.employee_id else None,
        "current_quantity": str(custody.current_quantity),
        "current_amount": str(custody.current_amount),
        "unit_cost_snapshot": str(custody.unit_cost_snapshot),
        "status": custody.status,
        "origin_issue_line_id": (
            str(custody.origin_issue_line_id) if custody.origin_issue_line_id else None
        ),
        "origin_import_row_id": (
            str(custody.origin_import_row_id) if custody.origin_import_row_id else None
        ),
        "parent_custody_id": (
            str(custody.parent_custody_id) if custody.parent_custody_id else None
        ),
    }


def _required_custody_action_values(*, quantity, business_date, reason, idempotency_key):
    cleaned_reason = str(reason or "").strip()
    cleaned_key = str(idempotency_key or "").strip()
    if not cleaned_reason:
        raise ValidationError({"reason": "保管动作原因不能为空。"})
    if not cleaned_key:
        raise ValidationError({"idempotency_key": "保管动作幂等键不能为空。"})
    if len(cleaned_key) > 128:
        raise ValidationError({"idempotency_key": "保管动作幂等键不能超过 128 个字符。"})
    return (
        quantize_quantity(quantity),
        _coerce_business_date(business_date),
        cleaned_reason,
        cleaned_key,
    )


@transaction.atomic
def return_custody_to_warehouse(
    *,
    custody,
    target_warehouse,
    quantity,
    business_date,
    reason,
    actor,
    idempotency_key,
    count_line=None,
    request=None,
):
    """Create the one-custody return draft used by the normal post service."""

    _require_current_company(custody.company)
    locked_count_line = None
    if count_line is not None:
        count_task, locked_count_line = _lock_supply_count_line(count_line)
        require_execute_supply_count_task(actor, count_task)
    custody = (
        SupplyCustody.objects.select_for_update(of=("self",))
        .select_related(
            "company",
            "item",
            "department",
            "employee",
            "origin_issue_line",
        )
        .get(pk=custody.pk, company=custody.company)
    )
    require_manage_supply_custody(actor, custody, action="return_draft")
    quantity, business_date, reason, idempotency_key = _required_custody_action_values(
        quantity=quantity,
        business_date=business_date,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    _assert_custody_count_action_allowed(
        custody=custody,
        count_line=locked_count_line,
        action_quantity=quantity,
    )
    warehouse = SupplyWarehouse.objects.filter(
        pk=getattr(target_warehouse, "pk", None),
        company=custody.company,
        is_active=True,
    ).first()
    if warehouse is None:
        raise ValidationError({"target_warehouse": "目标仓库不属于当前公司或已经停用。"})
    existing = (
        SupplyDocument.objects.select_for_update()
        .prefetch_related("lines")
        .filter(company=custody.company, idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        lines = list(existing.lines.all())
        if (
            existing.document_type == SupplyDocumentType.RETURN
            and existing.business_date == business_date
            and existing.target_warehouse_id == warehouse.pk
            and existing.department_id == custody.department_id
            and existing.employee_id == custody.employee_id
            and existing.remark == reason
            and len(lines) == 1
            and lines[0].source_custody_id == custody.pk
            and lines[0].quantity == quantity
            and lines[0].line_remark == reason
        ):
            return existing
        raise ValidationError("同一单据幂等键已用于不同内容。")
    if custody.status != SupplyCustodyStatus.OPEN:
        raise ValidationError("只有开放保管可以发起归还。")
    if quantity > custody.current_quantity:
        raise ValidationError(
            f"归还数量超过当前保管数量，当前最多可归还 {custody.current_quantity}。"
        )
    return create_supply_document(
        actor=actor,
        company=custody.company,
        document_type=SupplyDocumentType.RETURN,
        data={
            "business_date": business_date,
            "target_warehouse": warehouse,
            "department": custody.department,
            "employee": custody.employee,
            "remark": reason,
            "idempotency_key": idempotency_key,
        },
        lines=[
            {
                "item": custody.item,
                "quantity": quantity,
                "entered_unit_cost": None,
                "source_issue_line": custody.origin_issue_line,
                "source_custody": custody,
                "line_remark": reason,
            }
        ],
        request=request,
    )


@transaction.atomic
def transfer_custody(
    *,
    custody,
    quantity,
    target_department,
    target_employee=None,
    business_date,
    reason,
    actor,
    idempotency_key,
    count_line=None,
    request=None,
):
    quantity, business_date, reason, idempotency_key = _required_custody_action_values(
        quantity=quantity,
        business_date=business_date,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    _require_current_company(custody.company)
    locked_count_line = None
    if count_line is not None:
        count_task, locked_count_line = _lock_supply_count_line(count_line)
        require_execute_supply_count_task(actor, count_task)
    if target_department is None:
        raise ValidationError({"target_department": "目标部门必填。"})
    from apps.masterdata.models import Department, Employee

    custody = (
        SupplyCustody.objects.select_for_update(of=("self",))
        .select_related("company", "item", "department", "employee")
        .get(pk=custody.pk, company=custody.company)
    )
    existing_queryset = SupplyCustodyMovement.objects.select_for_update()
    if connection.vendor == "postgresql":
        existing_queryset = existing_queryset.select_for_update(of=("self",))
    existing = existing_queryset.select_related(
        "to_custody", "to_custody__department"
    ).filter(company=custody.company, idempotency_key=idempotency_key).first()
    if existing is not None:
        if (
            existing.action != SupplyCustodyAction.TRANSFER
            or existing.to_custody_id is None
        ):
            raise ValidationError("同一保管动作幂等键已用于不同内容。")
        require_manage_supply_custody(
            actor,
            custody,
            action="transfer",
            target_department=existing.to_custody.department,
        )
        if (
            existing.from_custody_id == custody.pk
            and existing.quantity == quantity
            and existing.business_date == business_date
            and existing.reason == reason
            and existing.to_custody.department_id
            == getattr(target_department, "pk", None)
            and existing.to_custody.employee_id
            == getattr(target_employee, "pk", None)
        ):
            return existing.to_custody
        raise ValidationError("同一保管动作幂等键已用于不同内容。")
    department = Department.objects.select_for_update().filter(
        pk=getattr(target_department, "pk", None),
        company=custody.company,
        is_active=True,
    ).first()
    if department is None:
        raise ValidationError({"target_department": "目标部门不属于当前公司或已经停用。"})
    employee = None
    if target_employee is not None:
        employee = Employee.objects.select_for_update().select_related(
            "department"
        ).filter(pk=getattr(target_employee, "pk", None), company=custody.company).first()
        if employee is None or employee.department_id != department.pk:
            raise ValidationError({"target_employee": "目标员工不属于目标部门或当前公司。"})
        if (
            employee.employment_status != "active"
            or not employee.is_active
            or not employee.department.is_active
        ):
            raise ValidationError({"target_employee": "责任员工已离职或停用，不能接收耐用品。"})
    require_manage_supply_custody(
        actor,
        custody,
        action="transfer",
        target_department=department,
    )
    if custody.status != SupplyCustodyStatus.OPEN:
        raise ValidationError("只有开放保管可以执行责任转交。")
    _assert_custody_count_action_allowed(
        custody=custody,
        count_line=locked_count_line,
        action_quantity=quantity,
    )
    if custody.department_id == department.pk and custody.employee_id == getattr(employee, "pk", None):
        raise ValidationError("目标责任部门和员工不能与当前保管完全相同。")

    allocation = allocate_custody_amount(
        current_quantity=custody.current_quantity,
        current_amount=custody.current_amount,
        unit_cost_snapshot=custody.unit_cost_snapshot,
        action_quantity=quantity,
    )
    old = _custody_snapshot(custody)
    now = timezone.now()
    target = _create_custody(
        values={
            "company": custody.company,
            "item": custody.item,
            "origin_issue_line": None,
            "origin_import_row": None,
            "parent_custody": custody,
            "department": department,
            "employee": employee,
            "current_quantity": allocation.action_quantity,
            "current_amount": allocation.action_amount,
            "unit_cost_snapshot": custody.unit_cost_snapshot,
            "started_on": business_date,
            "status": SupplyCustodyStatus.OPEN,
            "remark": reason,
        }
    )
    _update_custody_values(
        custody=custody,
        quantity=allocation.quantity_after,
        amount=allocation.amount_after,
        status=(
            SupplyCustodyStatus.CLOSED
            if allocation.quantity_after == ZERO_QTY
            else SupplyCustodyStatus.OPEN
        ),
        updated_at=now,
    )
    movement = _create_custody_movement(
        values={
            "company": custody.company,
            "item": custody.item,
            "from_custody": custody,
            "to_custody": target,
            "action": SupplyCustodyAction.TRANSFER,
            "quantity": allocation.action_quantity,
            "amount": allocation.action_amount,
            "unit_cost": custody.unit_cost_snapshot,
            "business_date": business_date,
            "reason": reason,
            "created_by": actor,
            "idempotency_key": idempotency_key,
        }
    )
    _audit(
        actor=actor,
        action="supply_custody_transfer",
        instance=movement,
        old=old,
        new={
            **_custody_snapshot(custody),
            "target_custody": _custody_snapshot(target),
            "quantity": str(allocation.action_quantity),
            "amount": str(allocation.action_amount),
            "reason": reason,
        },
        request=request,
    )
    if locked_count_line is not None:
        _resolve_supply_count_line_with_movement(
            count_line=locked_count_line,
            movement=movement,
            actor=actor,
            request=request,
        )
    resolve_supply_clearance_items_for_movement(
        movement=movement,
        request=request,
    )
    return target


@transaction.atomic
def write_off_custody(
    *,
    custody,
    quantity,
    action,
    business_date,
    reason,
    actor,
    idempotency_key,
    count_line=None,
    request=None,
):
    if action not in {SupplyCustodyAction.LOSS, SupplyCustodyAction.SCRAP}:
        raise ValidationError({"action": "保管核销动作只允许报损或报废。"})
    quantity, business_date, reason, idempotency_key = _required_custody_action_values(
        quantity=quantity,
        business_date=business_date,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    _require_current_company(custody.company)
    locked_count_line = None
    if count_line is not None:
        count_task, locked_count_line = _lock_supply_count_line(count_line)
        require_execute_supply_count_task(actor, count_task)
    custody = (
        SupplyCustody.objects.select_for_update(of=("self",))
        .select_related("company", "item", "department", "employee")
        .get(pk=custody.pk, company=custody.company)
    )
    require_manage_supply_custody(actor, custody, action=action)
    existing = SupplyCustodyMovement.objects.select_for_update().filter(
        company=custody.company, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if (
            existing.action == action
            and existing.from_custody_id == custody.pk
            and existing.to_custody_id is None
            and existing.quantity == quantity
            and existing.business_date == business_date
            and existing.reason == reason
        ):
            return existing
        raise ValidationError("同一保管动作幂等键已用于不同内容。")
    if custody.status != SupplyCustodyStatus.OPEN:
        raise ValidationError("只有开放保管可以执行报损或报废。")
    _assert_custody_count_action_allowed(
        custody=custody,
        count_line=locked_count_line,
        action_quantity=quantity,
    )
    allocation = allocate_custody_amount(
        current_quantity=custody.current_quantity,
        current_amount=custody.current_amount,
        unit_cost_snapshot=custody.unit_cost_snapshot,
        action_quantity=quantity,
    )
    old = _custody_snapshot(custody)
    now = timezone.now()
    _update_custody_values(
        custody=custody,
        quantity=allocation.quantity_after,
        amount=allocation.amount_after,
        status=(
            SupplyCustodyStatus.CLOSED
            if allocation.quantity_after == ZERO_QTY
            else SupplyCustodyStatus.OPEN
        ),
        updated_at=now,
    )
    movement = _create_custody_movement(
        values={
            "company": custody.company,
            "item": custody.item,
            "from_custody": custody,
            "to_custody": None,
            "action": action,
            "quantity": allocation.action_quantity,
            "amount": allocation.action_amount,
            "unit_cost": custody.unit_cost_snapshot,
            "business_date": business_date,
            "reason": reason,
            "created_by": actor,
            "idempotency_key": idempotency_key,
        }
    )
    _audit(
        actor=actor,
        action=(
            "supply_custody_loss"
            if action == SupplyCustodyAction.LOSS
            else "supply_custody_scrap"
        ),
        instance=movement,
        old=old,
        new={
            **_custody_snapshot(custody),
            "quantity": str(allocation.action_quantity),
            "amount": str(allocation.action_amount),
            "reason": reason,
        },
        request=request,
    )
    if locked_count_line is not None:
        _resolve_supply_count_line_with_movement(
            count_line=locked_count_line,
            movement=movement,
            actor=actor,
            request=request,
        )
    resolve_supply_clearance_items_for_movement(
        movement=movement,
        request=request,
    )
    return movement


def create_opening_custody_from_import_row(
    *,
    actor,
    import_row,
    item,
    department,
    employee,
    quantity,
    unit_cost,
    started_on,
    remark="",
    request=None,
):
    """Create one immutable opening custody inside import confirmation."""

    require_import_opening_custody(actor)
    company = import_row.batch.company
    _require_current_company(company)
    if (
        import_row.batch.import_type != "opening_custody"
        or import_row.batch.status not in {"validated", "confirmed"}
        or import_row.validation_status not in {"valid", "created"}
    ):
        raise ValidationError("来源行不是可确认的耐用品期初保管导入行。")
    if item.company_id != company.pk or item.item_type != SupplyItemType.DURABLE_QUANTITY:
        raise ValidationError("期初保管物品必须是当前公司数量型低值耐用品。")
    if not item.is_active:
        raise ValidationError("期初保管物品在确认前已经停用。")
    if department.company_id != company.pk or not department.is_active:
        raise ValidationError("期初保管责任部门不属于当前公司或已经停用。")
    if employee is not None and (
        employee.company_id != company.pk
        or employee.department_id != department.pk
        or employee.employment_status != "active"
        or not employee.is_active
        or not employee.department.is_active
    ):
        raise ValidationError("期初保管责任员工必须属于责任部门且在职、启用。")
    quantity = quantize_quantity(quantity)
    unit_cost = quantize_unit_cost(unit_cost)
    started_on = _coerce_business_date(started_on)
    if quantity <= ZERO_QTY:
        raise ValidationError("期初保管数量必须大于 0。")
    if unit_cost < ZERO_COST:
        raise ValidationError("期初保管单位成本不得小于 0。")
    amount = quantize_money(quantity * unit_cost)
    existing = SupplyCustody.objects.filter(origin_import_row=import_row).first()
    if existing is not None:
        return existing
    custody = _create_custody(
        values={
            "company": company,
            "item": item,
            "origin_issue_line": None,
            "origin_import_row": import_row,
            "parent_custody": None,
            "department": department,
            "employee": employee,
            "current_quantity": quantity,
            "current_amount": amount,
            "unit_cost_snapshot": unit_cost,
            "started_on": started_on,
            "status": SupplyCustodyStatus.OPEN,
            "remark": str(remark or "").strip(),
        }
    )
    movement = _create_custody_movement(
        values={
            "company": company,
            "item": item,
            "from_custody": None,
            "to_custody": custody,
            "action": SupplyCustodyAction.OPENING,
            "quantity": quantity,
            "amount": amount,
            "unit_cost": unit_cost,
            "business_date": started_on,
            "reason": str(remark or "").strip(),
            "created_by": actor,
            "idempotency_key": f"opening-custody-import:{import_row.batch_id}:{import_row.pk}",
        }
    )
    _audit(
        actor=actor,
        action="supply_custody_opening_import",
        instance=movement,
        new={**_custody_snapshot(custody), "import_row_id": str(import_row.pk)},
        request=request,
    )
    return custody


def durable_management_totals(*, company):
    """Return separated quantity-managed and individually tracked totals."""

    from apps.assets.models import Asset
    from apps.finance.models import AssetFinance

    stock = SupplyStockBalance.objects.filter(
        company=company,
        item__item_type=SupplyItemType.DURABLE_QUANTITY,
    ).aggregate(quantity=Sum("quantity_on_hand"), amount=Sum("amount_on_hand"))
    custody = SupplyCustody.objects.filter(
        company=company,
        status=SupplyCustodyStatus.OPEN,
        item__item_type=SupplyItemType.DURABLE_QUANTITY,
    ).aggregate(quantity=Sum("current_quantity"), amount=Sum("current_amount"))
    tracked_finances = AssetFinance.objects.filter(
        company=company,
        accounting_treatment=AssetFinance.AccountingTreatment.CONTROLLED_NON_FIXED,
        finance_confirmed_at__isnull=False,
        asset__record_status=Asset.RecordStatus.ACTIVE,
        asset__asset_status__in=(
            Asset.AssetStatus.PENDING_LABEL,
            Asset.AssetStatus.IN_USE,
            Asset.AssetStatus.IDLE,
            Asset.AssetStatus.LOANED,
            Asset.AssetStatus.UNDER_REPAIR,
            Asset.AssetStatus.PENDING_DISPOSAL,
        ),
    )
    tracked = tracked_finances.aggregate(
        quantity=Sum("asset__quantity"), amount=Sum("original_cost")
    )
    stock_quantity = quantize_quantity(stock["quantity"] or ZERO_QTY)
    stock_amount = quantize_money(stock["amount"] or ZERO_MONEY)
    custody_quantity = quantize_quantity(custody["quantity"] or ZERO_QTY)
    custody_amount = quantize_money(custody["amount"] or ZERO_MONEY)
    return {
        "durable_stock_quantity": stock_quantity,
        "durable_stock_amount": stock_amount,
        "durable_open_custody_quantity": custody_quantity,
        "durable_open_custody_amount": custody_amount,
        "durable_managed_amount": quantize_money(stock_amount + custody_amount),
        "controlled_non_fixed_asset_quantity": int(tracked["quantity"] or 0),
        "controlled_non_fixed_original_cost": quantize_money(
            tracked["amount"] or ZERO_MONEY
        ),
    }


def _posting_balance_requests(document, lines):
    requests = {}

    def add(warehouse, item):
        if warehouse is None:
            return
        requests[(str(warehouse.pk), str(item.pk))] = (warehouse, item)

    for line in lines:
        if document.document_type in {
            SupplyDocumentType.OPENING,
            SupplyDocumentType.RECEIPT,
            SupplyDocumentType.RETURN,
        }:
            add(document.target_warehouse, line.item)
        elif document.document_type == SupplyDocumentType.ISSUE:
            add(document.source_warehouse, line.item)
        elif document.document_type == SupplyDocumentType.TRANSFER:
            add(document.source_warehouse, line.item)
            add(document.target_warehouse, line.item)
        elif document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT:
            add(document.source_count_task.warehouse, line.item)
    return requests


def _lock_posting_warehouses(*, document, source_count_task=None):
    warehouses = {
        warehouse.pk: warehouse
        for warehouse in (document.source_warehouse, document.target_warehouse)
        if warehouse is not None
    }
    if document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT:
        if source_count_task is None:
            raise ValidationError("盘点调整单只能由盘点关闭服务过账。")
        warehouses[source_count_task.warehouse_id] = source_count_task.warehouse
    locked = {
        warehouse.pk: warehouse
        for warehouse in SupplyWarehouse.objects.select_for_update()
        .filter(company=document.company, pk__in=sorted(warehouses, key=str))
        .order_by("pk")
    }
    if len(locked) != len(warehouses):
        raise ValidationError("单据涉及的仓库不存在或不属于当前公司。")
    for warehouse in locked.values():
        active = (
            SupplyCountTask.objects.filter(
                company=document.company,
                count_domain=SupplyCountDomain.WAREHOUSE_STOCK,
                warehouse=warehouse,
                status__in=ACTIVE_SUPPLY_COUNT_STATUSES,
            )
            .order_by("pk")
            .first()
        )
        if active is None:
            continue
        if (
            document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT
            and source_count_task is not None
            and active.pk == source_count_task.pk
            and active.status == SupplyCountStatus.RECONCILIATION
            and document.source_count_task_id == active.pk
        ):
            continue
        raise ValidationError(
            "该仓库正在进行低值物品盘点，暂不能过账库存业务。"
            "请在盘点关闭或取消后重试。"
        )
    return locked


def _lock_posting_balances(*, document, lines):
    balances = {}
    for _, (warehouse, item) in sorted(
        _posting_balance_requests(document, lines).items(),
        key=lambda entry: entry[0],
    ):
        balance = _lock_or_create_balance(
            company=document.company,
            warehouse=warehouse,
            item=item,
        )
        balances[(warehouse.pk, item.pk)] = balance
    return balances


def _set_line_posted(*, line, unit_cost, amount):
    line.posted_unit_cost = quantize_unit_cost(unit_cost)
    line.posted_amount = quantize_money(amount)
    line._controlled_posting = True
    line.full_clean()
    line.save(update_fields=("posted_unit_cost", "posted_amount"))


def _ledger_values(
    *,
    document,
    line,
    warehouse,
    movement_type,
    quantity_delta,
    amount_delta,
    unit_cost,
    balance,
    quantity_after,
    amount_after,
    average_after,
    occurred_at,
    actor,
    reverses_ledger=None,
):
    return {
        "company": document.company,
        "warehouse": warehouse,
        "item": line.item,
        "document": document,
        "document_line": line,
        "movement_type": movement_type,
        "quantity_delta": quantize_quantity(quantity_delta),
        "amount_delta": quantize_money(amount_delta),
        "unit_cost": quantize_unit_cost(unit_cost),
        "quantity_before": balance.quantity_on_hand,
        "quantity_after": quantize_quantity(quantity_after),
        "amount_before": balance.amount_on_hand,
        "amount_after": quantize_money(amount_after),
        "average_unit_cost_before": balance.average_unit_cost,
        "average_unit_cost_after": quantize_unit_cost(average_after),
        "occurred_at": occurred_at,
        "created_by": actor,
        "reverses_ledger": reverses_ledger,
    }


def _post_receipts(*, document, lines, balances, actor, posted_at):
    ledgers = []
    movement_type = (
        SupplyStockMovementType.OPENING_IN
        if document.document_type == SupplyDocumentType.OPENING
        else SupplyStockMovementType.RECEIPT_IN
    )
    for line in lines:
        warehouse = document.target_warehouse
        balance = balances[(warehouse.pk, line.item_id)]
        calculation = calculate_receipt(
            balance.quantity_on_hand,
            balance.amount_on_hand,
            line.quantity,
            line.entered_unit_cost,
        )
        values = _ledger_values(
            document=document,
            line=line,
            warehouse=warehouse,
            movement_type=movement_type,
            quantity_delta=calculation.receipt_quantity,
            amount_delta=calculation.receipt_amount,
            unit_cost=calculation.receipt_unit_cost,
            balance=balance,
            quantity_after=calculation.quantity_after,
            amount_after=calculation.amount_after,
            average_after=calculation.average_unit_cost_after,
            occurred_at=posted_at,
            actor=actor,
        )
        _update_balance(balance=balance, calculation=calculation, updated_at=posted_at)
        _set_line_posted(
            line=line,
            unit_cost=calculation.receipt_unit_cost,
            amount=calculation.receipt_amount,
        )
        ledgers.append(_create_stock_ledger(values=values))
    return ledgers, []


def _post_issue(*, document, lines, balances, actor, posted_at, request=None):
    ledgers = []
    custodies = []
    warehouse = document.source_warehouse
    for line in lines:
        balance = balances[(warehouse.pk, line.item_id)]
        if line.quantity > balance.quantity_on_hand:
            raise ValidationError(
                "库存不足：{}在{}可用 {} {}，本次领用 {} {}。".format(
                    line.item.name,
                    warehouse.name,
                    balance.quantity_on_hand,
                    line.item.unit,
                    line.quantity,
                    line.item.unit,
                )
            )
        calculation = calculate_issue(
            balance.quantity_on_hand,
            balance.amount_on_hand,
            line.quantity,
        )
        values = _ledger_values(
            document=document,
            line=line,
            warehouse=warehouse,
            movement_type=SupplyStockMovementType.ISSUE_OUT,
            quantity_delta=-calculation.issue_quantity,
            amount_delta=-calculation.issue_amount,
            unit_cost=calculation.issue_unit_cost,
            balance=balance,
            quantity_after=calculation.quantity_after,
            amount_after=calculation.amount_after,
            average_after=calculation.average_unit_cost_after,
            occurred_at=posted_at,
            actor=actor,
        )
        _update_balance(balance=balance, calculation=calculation, updated_at=posted_at)
        _set_line_posted(
            line=line,
            unit_cost=calculation.issue_unit_cost,
            amount=calculation.issue_amount,
        )
        ledgers.append(_create_stock_ledger(values=values))
        if line.item.item_type == SupplyItemType.DURABLE_QUANTITY:
            custody = _create_custody(
                values={
                    "company": document.company,
                    "item": line.item,
                    "origin_issue_line": line,
                    "department": document.department,
                    "employee": document.employee,
                    "current_quantity": calculation.issue_quantity,
                    "current_amount": calculation.issue_amount,
                    "unit_cost_snapshot": calculation.issue_unit_cost,
                    "started_on": document.business_date,
                    "status": SupplyCustodyStatus.OPEN,
                    "remark": line.line_remark,
                }
            )
            _create_custody_movement(
                values={
                    "company": document.company,
                    "item": line.item,
                    "from_custody": None,
                    "to_custody": custody,
                    "action": SupplyCustodyAction.ISSUE,
                    "quantity": calculation.issue_quantity,
                    "amount": calculation.issue_amount,
                    "unit_cost": calculation.issue_unit_cost,
                    "business_date": document.business_date,
                    "source_document_line": line,
                    "reason": line.line_remark,
                    "created_by": actor,
                    "idempotency_key": f"document-post:{document.pk}:issue:{line.pk}",
                }
            )
            _audit(
                actor=actor,
                action="supply_custody_create",
                instance=custody,
                new={
                    "document_no": document.document_no,
                    "origin_issue_line_id": str(line.pk),
                    "item_id": str(line.item_id),
                    "department_id": str(document.department_id),
                    "employee_id": str(document.employee_id) if document.employee_id else None,
                    "current_quantity": str(custody.current_quantity),
                    "current_amount": str(custody.current_amount),
                },
                request=request,
            )
            custodies.append(custody)
    return ledgers, custodies


def _post_consumable_return(*, document, lines, balances, actor, posted_at):
    ledgers = []
    warehouse = document.target_warehouse
    for line in lines:
        source = line.source_issue_line
        if (
            source.document.document_type != SupplyDocumentType.ISSUE
            or source.document.status != SupplyDocumentStatus.POSTED
            or source.item_id != line.item_id
            or source.item.item_type != SupplyItemType.CONSUMABLE
        ):
            raise ValidationError(f"第 {line.line_no} 行原领用明细已失效或不属于易耗品。")
        returned = source.return_lines.filter(
            document__document_type=SupplyDocumentType.RETURN,
            document__status=SupplyDocumentStatus.POSTED,
        ).aggregate(quantity=Sum("quantity"), amount=Sum("posted_amount"))
        returned_quantity = quantize_quantity(returned["quantity"] or ZERO_QTY)
        returned_amount = quantize_money(returned["amount"] or ZERO_MONEY)
        remaining_quantity = quantize_quantity(source.quantity - returned_quantity)
        if line.quantity > remaining_quantity:
            raise ValidationError(
                "退回数量超过原领用未退数量：当前最多可退 {} {}。".format(
                    remaining_quantity, line.item.unit
                )
            )
        if line.quantity == remaining_quantity:
            return_amount = quantize_money(source.posted_amount - returned_amount)
        else:
            return_amount = quantize_money(line.quantity * source.posted_unit_cost)
        if return_amount < ZERO_MONEY:
            raise ValidationError("原领用剩余可退金额异常，请先执行余额核对。")
        balance = balances[(warehouse.pk, line.item_id)]
        calculation = calculate_receipt_from_amount(
            balance.quantity_on_hand,
            balance.amount_on_hand,
            line.quantity,
            return_amount,
        )
        values = _ledger_values(
            document=document,
            line=line,
            warehouse=warehouse,
            movement_type=SupplyStockMovementType.RETURN_IN,
            quantity_delta=calculation.receipt_quantity,
            amount_delta=return_amount,
            unit_cost=source.posted_unit_cost,
            balance=balance,
            quantity_after=calculation.quantity_after,
            amount_after=calculation.amount_after,
            average_after=calculation.average_unit_cost_after,
            occurred_at=posted_at,
            actor=actor,
        )
        _update_balance(balance=balance, calculation=calculation, updated_at=posted_at)
        _set_line_posted(
            line=line,
            unit_cost=source.posted_unit_cost,
            amount=return_amount,
        )
        ledgers.append(_create_stock_ledger(values=values))
    return ledgers, []


def _post_durable_return(
    *, document, lines, balances, locked_custodies, actor, posted_at, request=None
):
    if len(lines) != 1:
        raise ValidationError("一张耐用品归还单只能对应一个来源保管。")
    line = lines[0]
    custody = locked_custodies.get(line.source_custody_id)
    if custody is None:
        raise ValidationError("来源保管不存在或不属于当前公司。")
    if (
        line.item.item_type != SupplyItemType.DURABLE_QUANTITY
        or custody.item_id != line.item_id
        or custody.status != SupplyCustodyStatus.OPEN
    ):
        raise ValidationError("来源保管已结清、物品不一致或不是数量型低值耐用品。")
    allocation = allocate_custody_amount(
        current_quantity=custody.current_quantity,
        current_amount=custody.current_amount,
        unit_cost_snapshot=custody.unit_cost_snapshot,
        action_quantity=line.quantity,
    )
    warehouse = document.target_warehouse
    balance = balances[(warehouse.pk, line.item_id)]
    receipt = calculate_receipt_from_amount(
        balance.quantity_on_hand,
        balance.amount_on_hand,
        allocation.action_quantity,
        allocation.action_amount,
    )
    ledger_values = _ledger_values(
        document=document,
        line=line,
        warehouse=warehouse,
        movement_type=SupplyStockMovementType.RETURN_IN,
        quantity_delta=allocation.action_quantity,
        amount_delta=allocation.action_amount,
        unit_cost=custody.unit_cost_snapshot,
        balance=balance,
        quantity_after=receipt.quantity_after,
        amount_after=receipt.amount_after,
        average_after=receipt.average_unit_cost_after,
        occurred_at=posted_at,
        actor=actor,
    )
    old = _custody_snapshot(custody)
    _update_custody_values(
        custody=custody,
        quantity=allocation.quantity_after,
        amount=allocation.amount_after,
        status=(
            SupplyCustodyStatus.CLOSED
            if allocation.quantity_after == ZERO_QTY
            else SupplyCustodyStatus.OPEN
        ),
        updated_at=posted_at,
    )
    _update_balance(balance=balance, calculation=receipt, updated_at=posted_at)
    _set_line_posted(
        line=line,
        unit_cost=custody.unit_cost_snapshot,
        amount=allocation.action_amount,
    )
    ledger = _create_stock_ledger(values=ledger_values)
    movement = _create_custody_movement(
        values={
            "company": document.company,
            "item": line.item,
            "from_custody": custody,
            "to_custody": None,
            "action": SupplyCustodyAction.RETURN,
            "quantity": allocation.action_quantity,
            "amount": allocation.action_amount,
            "unit_cost": custody.unit_cost_snapshot,
            "business_date": document.business_date,
            "source_document_line": line,
            "reason": line.line_remark,
            "created_by": actor,
            "idempotency_key": f"document-post:{document.pk}:custody-return",
        }
    )
    _audit(
        actor=actor,
        action="supply_custody_return",
        instance=movement,
        old=old,
        new={
            **_custody_snapshot(custody),
            "document_no": document.document_no,
            "target_warehouse_id": str(warehouse.pk),
            "quantity": str(allocation.action_quantity),
            "amount": str(allocation.action_amount),
            "reason": line.line_remark,
        },
        request=request,
    )
    return [ledger], [custody]


def _post_transfer(*, document, lines, balances, actor, posted_at):
    ledgers = []
    source_warehouse = document.source_warehouse
    target_warehouse = document.target_warehouse
    for line in lines:
        source_balance = balances[(source_warehouse.pk, line.item_id)]
        if line.quantity > source_balance.quantity_on_hand:
            raise ValidationError(
                "库存不足：{}在{}可用 {} {}，本次调拨 {} {}。".format(
                    line.item.name,
                    source_warehouse.name,
                    source_balance.quantity_on_hand,
                    line.item.unit,
                    line.quantity,
                    line.item.unit,
                )
            )
        issue = calculate_issue(
            source_balance.quantity_on_hand,
            source_balance.amount_on_hand,
            line.quantity,
        )
        source_values = _ledger_values(
            document=document,
            line=line,
            warehouse=source_warehouse,
            movement_type=SupplyStockMovementType.TRANSFER_OUT,
            quantity_delta=-issue.issue_quantity,
            amount_delta=-issue.issue_amount,
            unit_cost=issue.issue_unit_cost,
            balance=source_balance,
            quantity_after=issue.quantity_after,
            amount_after=issue.amount_after,
            average_after=issue.average_unit_cost_after,
            occurred_at=posted_at,
            actor=actor,
        )
        _update_balance(balance=source_balance, calculation=issue, updated_at=posted_at)
        ledgers.append(_create_stock_ledger(values=source_values))

        target_balance = balances[(target_warehouse.pk, line.item_id)]
        receipt = calculate_receipt_from_amount(
            target_balance.quantity_on_hand,
            target_balance.amount_on_hand,
            line.quantity,
            issue.issue_amount,
        )
        target_values = _ledger_values(
            document=document,
            line=line,
            warehouse=target_warehouse,
            movement_type=SupplyStockMovementType.TRANSFER_IN,
            quantity_delta=receipt.receipt_quantity,
            amount_delta=issue.issue_amount,
            unit_cost=issue.issue_unit_cost,
            balance=target_balance,
            quantity_after=receipt.quantity_after,
            amount_after=receipt.amount_after,
            average_after=receipt.average_unit_cost_after,
            occurred_at=posted_at,
            actor=actor,
        )
        _update_balance(balance=target_balance, calculation=receipt, updated_at=posted_at)
        ledgers.append(_create_stock_ledger(values=target_values))
        _set_line_posted(
            line=line,
            unit_cost=issue.issue_unit_cost,
            amount=issue.issue_amount,
        )
    return ledgers, []


def _post_count_adjustment(*, document, lines, balances, actor, posted_at):
    ledgers = []
    warehouse = document.source_count_task.warehouse
    for line in lines:
        balance = balances[(warehouse.pk, line.item_id)]
        if line.adjustment_direction == "increase":
            calculation = calculate_receipt(
                balance.quantity_on_hand,
                balance.amount_on_hand,
                line.quantity,
                line.entered_unit_cost,
            )
            movement_type = SupplyStockMovementType.COUNT_GAIN
            quantity_delta = calculation.receipt_quantity
            amount_delta = calculation.receipt_amount
            posted_unit_cost = calculation.receipt_unit_cost
        elif line.adjustment_direction == "decrease":
            calculation = calculate_issue(
                balance.quantity_on_hand,
                balance.amount_on_hand,
                line.quantity,
            )
            movement_type = SupplyStockMovementType.COUNT_LOSS
            quantity_delta = -calculation.issue_quantity
            amount_delta = -calculation.issue_amount
            posted_unit_cost = calculation.issue_unit_cost
        else:
            raise ValidationError(
                f"第 {line.line_no} 行盘点调整方向无效。"
            )
        values = _ledger_values(
            document=document,
            line=line,
            warehouse=warehouse,
            movement_type=movement_type,
            quantity_delta=quantity_delta,
            amount_delta=amount_delta,
            unit_cost=posted_unit_cost,
            balance=balance,
            quantity_after=calculation.quantity_after,
            amount_after=calculation.amount_after,
            average_after=calculation.average_unit_cost_after,
            occurred_at=posted_at,
            actor=actor,
        )
        _update_balance(balance=balance, calculation=calculation, updated_at=posted_at)
        _set_line_posted(
            line=line,
            unit_cost=posted_unit_cost,
            amount=abs(amount_delta),
        )
        ledgers.append(_create_stock_ledger(values=values))
    return ledgers, []


@transaction.atomic
def _post_supply_document_internal(
    *,
    document,
    actor,
    idempotency_key=None,
    request=None,
    source_count_task=None,
    source_count_line=None,
):
    _require_current_company(document.company)
    document = (
        SupplyDocument.objects.select_for_update(of=("self",))
        .select_related(
            "company",
            "source_warehouse",
            "target_warehouse",
            "department",
            "employee",
            "employee__department",
            "source_count_task__warehouse",
        )
        .get(pk=document.pk, company=document.company)
    )
    if document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT:
        if source_count_line is not None:
            raise ValidationError("库存盘点调整不得使用保管盘点解决上下文。")
        if (
            source_count_task is None
            or document.source_count_task_id != source_count_task.pk
            or source_count_task.status != SupplyCountStatus.RECONCILIATION
            or source_count_task.count_domain
            != SupplyCountDomain.WAREHOUSE_STOCK
        ):
            raise ValidationError("盘点调整单只能由对应差异处理中任务的关闭服务过账。")
        require_execute_supply_count_task(actor, source_count_task)
    else:
        if source_count_task is not None:
            raise ValidationError("普通库存单据不得使用盘点关闭上下文。")
        require_post_supply_document(actor, document=document)
        if source_count_line is not None and document.document_type != SupplyDocumentType.RETURN:
            raise ValidationError("保管盘点解决上下文只能用于耐用品归还。")
    if document.status == SupplyDocumentStatus.POSTED:
        return document
    if document.status != SupplyDocumentStatus.DRAFT:
        raise ValidationError("只有草稿单据可以过账；已取消或已冲销单据不可恢复。")
    if document.document_type not in (
        SPRINT15_DOCUMENT_TYPES | {SupplyDocumentType.COUNT_ADJUSTMENT}
    ):
        raise ValidationError("当前 Sprint 不允许过账该单据类型。")
    if idempotency_key is not None and not str(idempotency_key).strip():
        raise ValidationError("过账幂等键不能为空。")
    document.full_clean()
    for warehouse in (document.source_warehouse, document.target_warehouse):
        if warehouse is not None and (
            warehouse.company_id != document.company_id or not warehouse.is_active
        ):
            raise ValidationError("单据仓库已停用或不属于当前公司，不能过账；请取消草稿。")
    lines = list(
        document.lines.select_for_update(of=("self",))
        .select_related(
            "item",
            "document",
            "source_issue_line",
            "source_issue_line__item",
            "source_issue_line__document",
            "source_custody",
        )
        .order_by("line_no")
    )
    if not lines:
        raise ValidationError("库存单据至少需要一条明细。")
    _lock_posting_warehouses(
        document=document,
        source_count_task=source_count_task,
    )
    locked_custodies = {}
    if document.document_type == SupplyDocumentType.RETURN:
        source_document_ids = sorted(
            {
                line.source_issue_line.document_id
                for line in lines
                if line.source_issue_line_id
            },
            key=str,
        )
        list(
            SupplyDocument.objects.select_for_update()
            .filter(
                pk__in=source_document_ids,
                company=document.company,
                document_type=SupplyDocumentType.ISSUE,
            )
            .order_by("pk")
        )
        source_ids = sorted(
            {line.source_issue_line_id for line in lines if line.source_issue_line_id},
            key=str,
        )
        locked_sources = {
            source.pk: source
            for source in SupplyDocumentLine.objects.select_for_update()
            .select_related("item", "document")
            .filter(pk__in=source_ids, company=document.company)
            .order_by("pk")
        }
        if len(locked_sources) != len(source_ids):
            raise ValidationError("原领用明细不存在或不属于当前公司。")
        for line in lines:
            line.source_issue_line = locked_sources.get(line.source_issue_line_id)
        custody_ids = sorted(
            {line.source_custody_id for line in lines if line.source_custody_id},
            key=str,
        )
        locked_custodies = {
            custody.pk: custody
            for custody in SupplyCustody.objects.select_for_update(of=("self",))
            .select_related(
                "item",
                "department",
                "employee",
                "origin_issue_line",
                "origin_import_row__batch",
                "parent_custody",
            )
            .filter(pk__in=custody_ids, company=document.company)
            .order_by("pk")
        }
        if len(locked_custodies) != len(custody_ids):
            raise ValidationError("来源保管不存在或不属于当前公司。")
        for line in lines:
            if line.source_custody_id:
                line.source_custody = locked_custodies.get(line.source_custody_id)
        return_modes = {
            "durable" if line.source_custody_id else "consumable" for line in lines
        }
        if len(return_modes) != 1:
            raise ValidationError("同一退回单不得混合易耗品退回和耐用品保管归还。")
        if "durable" in return_modes and len(lines) != 1:
            raise ValidationError("一张耐用品归还单只能对应一个来源保管。")
        if "durable" in return_modes:
            locked_count_line = None
            if source_count_line is not None:
                count_task, locked_count_line = _lock_supply_count_line(
                    source_count_line
                )
                require_execute_supply_count_task(actor, count_task)
            _assert_custody_count_action_allowed(
                custody=locked_custodies[lines[0].source_custody_id],
                count_line=locked_count_line,
                action_quantity=lines[0].quantity,
            )
            source_count_line = locked_count_line
        elif source_count_line is not None:
            raise ValidationError("易耗品退回不能解决耐用品保管盘点差异。")
    for line in lines:
        if line.company_id != document.company_id or line.item.company_id != document.company_id:
            raise ValidationError(f"第 {line.line_no} 行公司边界不一致。")
        if not line.item.is_active:
            raise ValidationError(f"第 {line.line_no} 行物品已停用，不能过账。")
        line.full_clean()
        if line.stock_ledgers.exists():
            raise ValidationError(f"第 {line.line_no} 行已存在库存流水，不能重复过账。")

    balances = _lock_posting_balances(document=document, lines=lines)
    old = _document_snapshot(document)
    posted_at = timezone.now()
    if document.document_type in {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
    }:
        ledgers, custodies = _post_receipts(
            document=document,
            lines=lines,
            balances=balances,
            actor=actor,
            posted_at=posted_at,
        )
    elif document.document_type == SupplyDocumentType.ISSUE:
        ledgers, custodies = _post_issue(
            document=document,
            lines=lines,
            balances=balances,
            actor=actor,
            posted_at=posted_at,
            request=request,
        )
    elif document.document_type == SupplyDocumentType.RETURN:
        if lines[0].source_custody_id:
            ledgers, custodies = _post_durable_return(
                document=document,
                lines=lines,
                balances=balances,
                locked_custodies=locked_custodies,
                actor=actor,
                posted_at=posted_at,
                request=request,
            )
            movement = SupplyCustodyMovement.objects.select_related(
                "from_custody", "to_custody"
            ).get(
                company=document.company,
                source_document_line=lines[0],
                action=SupplyCustodyAction.RETURN,
            )
            if source_count_line is not None:
                _resolve_supply_count_line_with_movement(
                    count_line=source_count_line,
                    movement=movement,
                    actor=actor,
                    request=request,
                )
            resolve_supply_clearance_items_for_movement(
                movement=movement,
                request=request,
            )
        else:
            ledgers, custodies = _post_consumable_return(
                document=document,
                lines=lines,
                balances=balances,
                actor=actor,
                posted_at=posted_at,
            )
    elif document.document_type == SupplyDocumentType.TRANSFER:
        ledgers, custodies = _post_transfer(
            document=document,
            lines=lines,
            balances=balances,
            actor=actor,
            posted_at=posted_at,
        )
    else:
        ledgers, custodies = _post_count_adjustment(
            document=document,
            lines=lines,
            balances=balances,
            actor=actor,
            posted_at=posted_at,
        )

    document.status = SupplyDocumentStatus.POSTED
    document.posted_by = actor
    document.posted_at = posted_at
    document._controlled_transition = True
    _enable_capability("controlled_supply_document_transition")
    document.full_clean()
    document.save(update_fields=("status", "posted_by", "posted_at", "updated_at"))
    _audit(
        actor=actor,
        action="supply_document_post",
        instance=document,
        old=old,
        new={
            **_document_snapshot(document),
            "ledger_count": len(ledgers),
            "custody_count": len(custodies),
            "total_amount": str(sum((line.posted_amount for line in lines), ZERO_MONEY)),
        },
        request=request,
    )
    return document


@transaction.atomic
def post_supply_document(
    *, document, actor, idempotency_key=None, request=None
):
    return _post_supply_document_internal(
        document=document,
        actor=actor,
        idempotency_key=idempotency_key,
        request=request,
    )


def _latest_ledger_for_balance(*, company, warehouse_id, item_id):
    return (
        SupplyStockLedger.objects.filter(
            company=company,
            warehouse_id=warehouse_id,
            item_id=item_id,
        )
        .select_related("document", "document_line")
        .order_by(
            "-occurred_at",
            "-document__posted_at",
            "-document_line__line_no",
            "-document__document_no",
            "-movement_type",
        )
        .first()
    )


@transaction.atomic
def reverse_supply_document(
    *, document, actor, idempotency_key, reason, request=None
):
    _require_current_company(document.company)
    cleaned_key = str(idempotency_key or "").strip()
    cleaned_reason = str(reason or "").strip()
    if not cleaned_key:
        raise ValidationError({"idempotency_key": "冲销幂等键不能为空。"})
    if not cleaned_reason:
        raise ValidationError({"reason": "冲销原因不能为空。"})
    document = (
        SupplyDocument.objects.select_for_update(of=("self",))
        .select_related(
            "company",
            "source_warehouse",
            "target_warehouse",
            "department",
            "employee",
        )
        .get(pk=document.pk, company=document.company)
    )
    require_reverse_supply_document(actor, document=document)
    if document.status == SupplyDocumentStatus.REVERSED:
        existing = SupplyDocument.objects.filter(
            company=document.company,
            reversal_of=document,
        ).first()
        if existing is None:
            raise ValidationError("原单已标记冲销但未找到冲销单，请先执行数据核对。")
        return existing
    if document.status != SupplyDocumentStatus.POSTED:
        raise ValidationError("只允许冲销已过账单据；草稿、已取消或已冲销单据不可冲销。")
    if document.document_type == SupplyDocumentType.REVERSAL:
        raise ValidationError("冲销单不能再次冲销。")
    if document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT:
        raise ValidationError("盘点调整单已与关闭任务勾稽，不能通过普通冲销改写。")
    existing_key = SupplyDocument.objects.select_for_update().filter(
        company=document.company,
        idempotency_key=cleaned_key,
    ).first()
    if existing_key is not None:
        if existing_key.reversal_of_id == document.pk:
            return existing_key
        raise ValidationError("同一冲销幂等键已用于其他单据。")

    original_lines = list(
        document.lines.select_for_update(of=("self",))
        .select_related("item", "source_issue_line", "source_custody")
        .order_by("line_no")
    )
    source_issue_ids = sorted(
        {
            line.source_issue_line_id
            for line in original_lines
            if line.source_issue_line_id
        },
        key=str,
    )
    if source_issue_ids:
        list(
            SupplyDocumentLine.objects.select_for_update()
            .filter(pk__in=source_issue_ids)
            .order_by("pk")
        )
    if document.document_type == SupplyDocumentType.ISSUE:
        if SupplyDocumentLine.objects.filter(
            source_issue_line__document=document,
            document__document_type=SupplyDocumentType.RETURN,
            document__status=SupplyDocumentStatus.POSTED,
        ).exists():
            raise ValidationError("原领用行已经发生退回，不能冲销领用单。")

    original_ledgers = list(
        SupplyStockLedger.objects.select_for_update()
        .select_related("warehouse", "item", "document_line")
        .filter(document=document)
        .order_by("document_line__line_no", "warehouse_id", "movement_type")
    )
    if not original_ledgers:
        raise ValidationError("原单没有库存流水，不能执行完整冲销。")
    if any(hasattr(ledger, "reversal_ledger") for ledger in original_ledgers):
        raise ValidationError("原单流水已经被冲销，不能重复执行。")
    _lock_posting_warehouses(document=document)

    durable_return_entries = {}
    if document.document_type == SupplyDocumentType.RETURN:
        durable_lines = [
            line for line in original_lines if line.source_custody_id is not None
        ]
        if durable_lines and len(durable_lines) != len(original_lines):
            raise ValidationError("退回单混合了易耗品和耐用品来源，不能冲销。")
        custody_ids = sorted(
            {line.source_custody_id for line in durable_lines}, key=str
        )
        locked_return_custodies = {
            custody.pk: custody
            for custody in SupplyCustody.objects.select_for_update(of=("self",))
            .select_related("item", "department", "employee")
            .filter(pk__in=custody_ids, company=document.company)
            .order_by("pk")
        }
        if len(locked_return_custodies) != len(custody_ids):
            raise ValidationError("耐用品归还来源保管已不存在，不能冲销。")
        for line in durable_lines:
            custody = locked_return_custodies[line.source_custody_id]
            _assert_custody_count_action_allowed(custody=custody)
            related = list(
                SupplyCustodyMovement.objects.select_for_update()
                .filter(Q(from_custody=custody) | Q(to_custody=custody))
                .order_by("created_at", "pk")
            )
            returns = [
                movement
                for movement in related
                if movement.action == SupplyCustodyAction.RETURN
                and movement.from_custody_id == custody.pk
                and movement.to_custody_id is None
                and movement.source_document_line_id == line.pk
            ]
            if len(returns) != 1:
                raise ValidationError("未找到唯一的耐用品归还保管流水，不能冲销。")
            original_movement = returns[0]
            if EmployeeSupplyClearanceItem.objects.filter(
                custody_movement=original_movement
            ).exists():
                raise ValidationError("该归还流水已作为离职清退证据，不能冲销。")
            if hasattr(original_movement, "reversal_movement"):
                raise ValidationError("耐用品归还保管流水已经被冲销。")
            if any(
                movement.pk != original_movement.pk
                and movement.created_at > original_movement.created_at
                for movement in related
            ):
                raise ValidationError(
                    "该保管归还后已经发生转交、再次归还、报损、报废或其他动作，不能冲销。"
                )
            expected_quantity = ZERO_QTY
            expected_amount = ZERO_MONEY
            for movement in related:
                if movement.to_custody_id == custody.pk:
                    expected_quantity = quantize_quantity(
                        expected_quantity + movement.quantity
                    )
                    expected_amount = quantize_money(expected_amount + movement.amount)
                if movement.from_custody_id == custody.pk:
                    expected_quantity = quantize_quantity(
                        expected_quantity - movement.quantity
                    )
                    expected_amount = quantize_money(expected_amount - movement.amount)
            expected_status = (
                SupplyCustodyStatus.CLOSED
                if expected_quantity == ZERO_QTY and expected_amount == ZERO_MONEY
                else SupplyCustodyStatus.OPEN
            )
            if (
                custody.current_quantity != expected_quantity
                or custody.current_amount != expected_amount
                or custody.status != expected_status
            ):
                raise ValidationError("当前保管余额与不可变流水不一致，不能冲销。")
            durable_return_entries[line.pk] = (custody, original_movement)

    grouped = defaultdict(list)
    balance_requests = {}
    for ledger in original_ledgers:
        key = (ledger.warehouse_id, ledger.item_id)
        grouped[key].append(ledger)
        balance_requests[(str(ledger.warehouse_id), str(ledger.item_id))] = (
            ledger.warehouse,
            ledger.item,
        )
    balances = {}
    for _, (warehouse, item) in sorted(balance_requests.items(), key=lambda entry: entry[0]):
        balances[(warehouse.pk, item.pk)] = _lock_or_create_balance(
            company=document.company,
            warehouse=warehouse,
            item=item,
        )

    for key, group in grouped.items():
        last_original = max(group, key=lambda ledger: ledger.document_line.line_no)
        latest = _latest_ledger_for_balance(
            company=document.company,
            warehouse_id=key[0],
            item_id=key[1],
        )
        if latest is None or latest.pk != last_original.pk:
            raise ValidationError(
                "该单据之后已经发生库存业务，不能直接冲销；请按当前日期制作更正单据。"
            )
        balance = balances[key]
        if (
            balance.quantity_on_hand != last_original.quantity_after
            or balance.amount_on_hand != last_original.amount_after
            or balance.average_unit_cost != last_original.average_unit_cost_after
        ):
            raise ValidationError("当前库存余额与原流水快照不一致，不能冲销，请先执行余额核对。")

    custody_entries = {}
    if document.document_type == SupplyDocumentType.ISSUE:
        durable_line_ids = [
            line.pk
            for line in original_lines
            if line.item.item_type == SupplyItemType.DURABLE_QUANTITY
        ]
        locked_custodies = {
            custody.origin_issue_line_id: custody
            for custody in SupplyCustody.objects.select_for_update(of=("self",))
            .select_related("item", "department", "employee")
            .filter(origin_issue_line_id__in=durable_line_ids)
            .order_by("origin_issue_line_id")
        }
        if len(locked_custodies) != len(durable_line_ids):
            raise ValidationError("耐用品领用缺少对应保管记录，不能冲销。")
        for line_id in durable_line_ids:
            line = next(value for value in original_lines if value.pk == line_id)
            custody = locked_custodies[line_id]
            _assert_custody_count_action_allowed(custody=custody)
            movements = list(
                SupplyCustodyMovement.objects.select_for_update()
                .filter(Q(from_custody=custody) | Q(to_custody=custody))
                .order_by("created_at")
            )
            issue_movements = [
                movement
                for movement in movements
                if movement.action == SupplyCustodyAction.ISSUE
                and movement.from_custody_id is None
                and movement.to_custody_id == custody.pk
                and movement.source_document_line_id == line.pk
            ]
            if len(movements) != 1 or len(issue_movements) != 1:
                raise ValidationError("该耐用品保管已经发生后续动作，不能直接冲销原领用单。")
            if (
                custody.status != SupplyCustodyStatus.OPEN
                or custody.current_quantity != line.quantity
                or custody.current_amount != line.posted_amount
            ):
                raise ValidationError("该耐用品保管余额与原领用快照不一致，不能冲销。")
            custody_entries[line.pk] = (custody, issue_movements[0])

    reversed_at = timezone.now()
    reversal = SupplyDocument(
        company=document.company,
        document_no=_next_supply_document_no(
            company=document.company,
            document_type=SupplyDocumentType.REVERSAL,
            business_date=timezone.localdate(),
        ),
        document_type=SupplyDocumentType.REVERSAL,
        business_date=timezone.localdate(),
        source_warehouse=document.source_warehouse,
        target_warehouse=document.target_warehouse,
        department=document.department,
        employee=document.employee,
        external_reference=f"冲销 {document.document_no}",
        counterparty_name=document.counterparty_name,
        remark=cleaned_reason,
        status=SupplyDocumentStatus.POSTED,
        idempotency_key=cleaned_key,
        reversal_of=document,
        created_by=actor,
        posted_by=actor,
        posted_at=reversed_at,
    )
    reversal._controlled_transition = True
    reversal.full_clean()
    try:
        reversal.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("冲销单编号、幂等键或原单关系冲突。") from exc

    reversal_lines = {}
    for original_line in original_lines:
        line = SupplyDocumentLine(
            company=document.company,
            document=reversal,
            line_no=original_line.line_no,
            item=original_line.item,
            quantity=original_line.quantity,
            entered_unit_cost=None,
            posted_unit_cost=original_line.posted_unit_cost,
            posted_amount=original_line.posted_amount,
            adjustment_direction=None,
            source_issue_line=original_line.source_issue_line,
            source_custody=original_line.source_custody,
            line_remark=cleaned_reason,
        )
        line._controlled_posting = True
        line.full_clean()
        line.save(force_insert=True)
        reversal_lines[original_line.pk] = line

    reversal_ledgers = []
    for key in sorted(grouped, key=lambda value: (str(value[0]), str(value[1]))):
        balance = balances[key]
        for original in sorted(
            grouped[key],
            key=lambda ledger: ledger.document_line.line_no,
            reverse=True,
        ):
            if (
                balance.quantity_on_hand != original.quantity_after
                or balance.amount_on_hand != original.amount_after
                or balance.average_unit_cost != original.average_unit_cost_after
            ):
                raise ValidationError("当前库存余额与原流水快照不一致，不能冲销，请先执行余额核对。")
            reversal_line = reversal_lines[original.document_line_id]
            values = _ledger_values(
                document=reversal,
                line=reversal_line,
                warehouse=original.warehouse,
                movement_type=SupplyStockMovementType.REVERSAL,
                quantity_delta=-original.quantity_delta,
                amount_delta=-original.amount_delta,
                unit_cost=original.unit_cost,
                balance=balance,
                quantity_after=original.quantity_before,
                amount_after=original.amount_before,
                average_after=original.average_unit_cost_before,
                occurred_at=reversed_at,
                actor=actor,
                reverses_ledger=original,
            )
            _update_balance_values(
                balance=balance,
                quantity=original.quantity_before,
                amount=original.amount_before,
                average_unit_cost=original.average_unit_cost_before,
                updated_at=reversed_at,
            )
            reversal_ledgers.append(_create_stock_ledger(values=values))

    for original_line_id, (custody, issue_movement) in custody_entries.items():
        _create_custody_movement(
            values={
                "company": document.company,
                "item": custody.item,
                "from_custody": custody,
                "to_custody": None,
                "action": SupplyCustodyAction.REVERSAL,
                "quantity": custody.current_quantity,
                "amount": custody.current_amount,
                "unit_cost": custody.unit_cost_snapshot,
                "business_date": reversal.business_date,
                "source_document_line": reversal_lines[original_line_id],
                "reason": cleaned_reason,
                "created_by": actor,
                "reverses_movement": issue_movement,
                "idempotency_key": f"document-reversal:{reversal.pk}:issue:{original_line_id}",
            }
        )
        old_custody = {
            "current_quantity": str(custody.current_quantity),
            "current_amount": str(custody.current_amount),
            "status": custody.status,
        }
        _update_custody_values(
            custody=custody,
            quantity=ZERO_QTY,
            amount=ZERO_MONEY,
            status=SupplyCustodyStatus.CLOSED,
            updated_at=reversed_at,
        )
        _audit(
            actor=actor,
            action="supply_custody_reverse",
            instance=custody,
            old=old_custody,
            new={
                "current_quantity": str(custody.current_quantity),
                "current_amount": str(custody.current_amount),
                "status": custody.status,
                "reversal_document_no": reversal.document_no,
            },
            request=request,
        )

    for original_line_id, (custody, return_movement) in durable_return_entries.items():
        old_custody = _custody_snapshot(custody)
        restored_quantity = quantize_quantity(
            custody.current_quantity + return_movement.quantity
        )
        restored_amount = quantize_money(
            custody.current_amount + return_movement.amount
        )
        reversal_movement = _create_custody_movement(
            values={
                "company": document.company,
                "item": custody.item,
                "from_custody": None,
                "to_custody": custody,
                "action": SupplyCustodyAction.REVERSAL,
                "quantity": return_movement.quantity,
                "amount": return_movement.amount,
                "unit_cost": return_movement.unit_cost,
                "business_date": reversal.business_date,
                "source_document_line": reversal_lines[original_line_id],
                "reason": cleaned_reason,
                "created_by": actor,
                "reverses_movement": return_movement,
                "idempotency_key": (
                    f"document-reversal:{reversal.pk}:return:{original_line_id}"
                ),
            }
        )
        _update_custody_values(
            custody=custody,
            quantity=restored_quantity,
            amount=restored_amount,
            status=SupplyCustodyStatus.OPEN,
            updated_at=reversed_at,
        )
        _audit(
            actor=actor,
            action="supply_custody_return_reverse",
            instance=reversal_movement,
            old=old_custody,
            new={
                **_custody_snapshot(custody),
                "reversal_document_no": reversal.document_no,
                "restored_quantity": str(return_movement.quantity),
                "restored_amount": str(return_movement.amount),
                "reason": cleaned_reason,
            },
            request=request,
        )

    old_document = _document_snapshot(document)
    document.status = SupplyDocumentStatus.REVERSED
    document.reversed_by = actor
    document.reversed_at = reversed_at
    document._controlled_transition = True
    _enable_capability("controlled_supply_document_transition")
    document.save(update_fields=("status", "reversed_by", "reversed_at", "updated_at"))
    _audit(
        actor=actor,
        action="supply_document_reverse",
        instance=document,
        old=old_document,
        new={
            **_document_snapshot(document),
            "reversal_document_id": str(reversal.pk),
            "reversal_document_no": reversal.document_no,
            "reason": cleaned_reason,
            "ledger_count": len(reversal_ledgers),
            "custody_count": len(custody_entries) + len(durable_return_entries),
        },
        request=request,
    )
    return reversal


def _coerce_count_date(value, field_name, label):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError({field_name: f"{label}必须是有效日期。"}) from exc


def _count_task_snapshot(task):
    return {
        "task_no": task.task_no,
        "name": task.name,
        "count_domain": task.count_domain,
        "warehouse_id": str(task.warehouse_id) if task.warehouse_id else None,
        "department_id": str(task.department_id) if task.department_id else None,
        "employee_id": str(task.employee_id) if task.employee_id else None,
        "planned_start": task.planned_start,
        "planned_end": task.planned_end,
        "snapshot_at": task.snapshot_at,
        "status": task.status,
    }


def _count_line_snapshot(line, *, include_cost=True):
    values = {
        "line_id": str(line.pk),
        "item_id": str(line.item_id),
        "custody_id": str(line.custody_id) if line.custody_id else None,
        "counted_quantity": (
            str(line.counted_quantity)
            if line.counted_quantity is not None
            else None
        ),
        "difference_quantity": (
            str(line.difference_quantity)
            if line.difference_quantity is not None
            else None
        ),
        "remark": line.remark,
        "resolution_type": line.resolution_type,
        "adjustment_document_line_id": (
            str(line.adjustment_document_line_id)
            if line.adjustment_document_line_id
            else None
        ),
        "resolution_custody_movement_id": (
            str(line.resolution_custody_movement_id)
            if line.resolution_custody_movement_id
            else None
        ),
    }
    if include_cost:
        values.update(
            {
                "expected_amount": str(line.expected_amount),
                "expected_unit_cost": str(line.expected_unit_cost),
                "adjustment_unit_cost": (
                    str(line.adjustment_unit_cost)
                    if line.adjustment_unit_cost is not None
                    else None
                ),
                "zero_cost_reason": line.zero_cost_reason,
            }
        )
    return values


def _lock_supply_count_task(task):
    task_id = getattr(task, "pk", task)
    raw = SupplyCountTask.objects.select_related("company").get(pk=task_id)
    _require_current_company(raw.company)
    queryset = SupplyCountTask.objects.select_for_update()
    if connection.vendor == "postgresql":
        queryset = queryset.select_for_update(of=("self",))
    return queryset.select_related(
        "company",
        "warehouse",
        "department",
        "employee__department",
    ).get(pk=task_id, company=raw.company)


def _lock_supply_count_line(line):
    line_id = getattr(line, "pk", line)
    raw = SupplyCountLine.objects.values("count_task_id").get(pk=line_id)
    task = _lock_supply_count_task(raw["count_task_id"])
    queryset = SupplyCountLine.objects.select_for_update()
    if connection.vendor == "postgresql":
        queryset = queryset.select_for_update(of=("self",))
    line = queryset.select_related(
        "company",
        "count_task",
        "item",
        "stock_balance",
        "custody__department",
        "custody__employee",
    ).get(pk=line_id, count_task=task)
    line.count_task = task
    return task, line


def _create_supply_count_line(**values):
    line = SupplyCountLine(**values)
    line._controlled_insert = True
    _enable_capability("controlled_supply_count_line_insert")
    line.full_clean()
    try:
        line.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("盘点快照中已存在相同物品或保管记录。") from exc
    return line


@transaction.atomic
def create_supply_count_task(*, actor, company, data, request=None):
    from apps.masterdata.models import Company

    _require_current_company(company)
    values = dict(data)
    allowed = {
        "name",
        "count_domain",
        "warehouse",
        "department",
        "employee",
        "planned_start",
        "planned_end",
        "idempotency_key",
        "remark",
    }
    unknown = set(values).difference(allowed)
    if unknown:
        raise ValidationError(
            {field: "不是盘点任务可编辑字段。" for field in unknown}
        )
    count_domain = str(values.get("count_domain") or "").strip()
    name = str(values.get("name") or "").strip()
    key = str(values.get("idempotency_key") or "").strip()
    if count_domain not in SupplyCountDomain.values:
        raise ValidationError({"count_domain": "盘点域无效。"})
    if not name:
        raise ValidationError({"name": "盘点任务名称不能为空。"})
    if not key:
        raise ValidationError({"idempotency_key": "盘点任务幂等键不能为空。"})
    warehouse = values.get("warehouse")
    department = values.get("department")
    employee = values.get("employee")
    require_create_supply_count_task(
        actor,
        company=company,
        count_domain=count_domain,
        department=department,
    )
    for field_name, value in (
        ("warehouse", warehouse),
        ("department", department),
        ("employee", employee),
    ):
        if value is not None and value.company_id != company.pk:
            raise ValidationError({field_name: "盘点范围对象不属于当前公司。"})
    planned_start = _coerce_count_date(
        values.get("planned_start"), "planned_start", "计划开始日期"
    )
    planned_end = _coerce_count_date(
        values.get("planned_end"), "planned_end", "计划结束日期"
    )
    if planned_end < planned_start:
        raise ValidationError({"planned_end": "计划结束日期不得早于计划开始日期。"})
    Company.objects.select_for_update().get(pk=company.pk)
    existing = SupplyCountTask.objects.select_for_update().filter(
        company=company, idempotency_key=key
    ).first()
    expected = {
        "name": name,
        "count_domain": count_domain,
        "warehouse_id": getattr(warehouse, "pk", None),
        "department_id": getattr(department, "pk", None),
        "employee_id": getattr(employee, "pk", None),
        "planned_start": planned_start,
        "planned_end": planned_end,
        "remark": str(values.get("remark") or "").strip(),
    }
    if existing is not None:
        actual = {field: getattr(existing, field) for field in expected}
        if actual != expected:
            raise ValidationError("同一盘点任务幂等键已用于不同内容。")
        return existing
    task = SupplyCountTask(
        company=company,
        task_no=_next_supply_document_no(
            company=company,
            document_type="count_task",
            business_date=planned_start,
        ),
        name=name,
        count_domain=count_domain,
        warehouse=warehouse,
        department=department,
        employee=employee,
        planned_start=planned_start,
        planned_end=planned_end,
        status=SupplyCountStatus.DRAFT,
        idempotency_key=key,
        created_by=actor,
        remark=expected["remark"],
    )
    task._controlled_insert = True
    _enable_capability("controlled_supply_count_task_insert")
    task.full_clean()
    try:
        task.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("盘点任务编号或幂等键冲突。") from exc
    _audit(
        actor=actor,
        action="supply_count_task_create",
        instance=task,
        new=_count_task_snapshot(task),
        request=request,
    )
    return task


@transaction.atomic
def publish_supply_count_task(*, task, actor, request=None):
    from apps.masterdata.models import Department, Employee

    task = _lock_supply_count_task(task)
    require_execute_supply_count_task(actor, task)
    if task.status == SupplyCountStatus.IN_PROGRESS:
        return task
    if task.status != SupplyCountStatus.DRAFT:
        raise ValidationError("只有草稿盘点任务可以发布。")
    if task.lines.exists():
        raise ValidationError("草稿盘点任务存在异常快照行，不能发布。")
    now = timezone.now()
    if task.count_domain == SupplyCountDomain.WAREHOUSE_STOCK:
        warehouse = SupplyWarehouse.objects.select_for_update().get(
            pk=task.warehouse_id,
            company=task.company,
            is_active=True,
        )
        active = SupplyCountTask.objects.filter(
            company=task.company,
            count_domain=SupplyCountDomain.WAREHOUSE_STOCK,
            warehouse=warehouse,
            status__in=ACTIVE_SUPPLY_COUNT_STATUSES,
        ).exclude(pk=task.pk)
        if active.exists():
            raise ValidationError("该仓库已有进行中或差异处理中的低值物品盘点任务。")
        balances = list(
            SupplyStockBalance.objects.select_for_update()
            .select_related("item")
            .filter(company=task.company, warehouse=warehouse)
            .order_by("item_id", "pk")
        )
        for balance in balances:
            _create_supply_count_line(
                company=task.company,
                count_task=task,
                item=balance.item,
                stock_balance=balance,
                custody=None,
                item_code_snapshot=balance.item.item_code,
                item_name_snapshot=balance.item.name,
                department_snapshot="",
                employee_snapshot="",
                expected_quantity=balance.quantity_on_hand,
                expected_amount=balance.amount_on_hand,
                expected_unit_cost=balance.average_unit_cost,
                adjustment_unit_cost=(
                    balance.average_unit_cost
                    if balance.quantity_on_hand > ZERO_QTY
                    else None
                ),
            )
    else:
        Department.objects.select_for_update().get(
            pk=task.department_id, company=task.company
        )
        if task.employee_id:
            Employee.objects.select_for_update().get(
                pk=task.employee_id,
                company=task.company,
                department_id=task.department_id,
            )
        overlap = SupplyCountTask.objects.filter(
            company=task.company,
            count_domain=SupplyCountDomain.CUSTODY,
            department_id=task.department_id,
            status__in=ACTIVE_SUPPLY_COUNT_STATUSES,
        ).exclude(pk=task.pk)
        if task.employee_id:
            overlap = overlap.filter(
                Q(employee__isnull=True) | Q(employee_id=task.employee_id)
            )
        if overlap.exists():
            raise ValidationError("该部门或员工已被另一张活动耐用品保管盘点覆盖。")
        custody_qs = (
            SupplyCustody.objects.select_for_update(of=("self",))
            if connection.vendor == "postgresql"
            else SupplyCustody.objects.select_for_update()
        )
        custody_qs = custody_qs.select_related(
            "item", "department", "employee"
        ).filter(
            company=task.company,
            department_id=task.department_id,
            status=SupplyCustodyStatus.OPEN,
            current_quantity__gt=ZERO_QTY,
        )
        if task.employee_id:
            custody_qs = custody_qs.filter(employee_id=task.employee_id)
        custodies = list(custody_qs.order_by("pk"))
        active_custody_ids = set(
            SupplyCountLine.objects.filter(
                company=task.company,
                custody_id__in=[custody.pk for custody in custodies],
                count_task__status__in=ACTIVE_SUPPLY_COUNT_STATUSES,
            ).values_list("custody_id", flat=True)
        )
        if active_custody_ids:
            raise ValidationError("盘点范围内存在已被另一张活动任务快照占用的保管记录。")
        for custody in custodies:
            _create_supply_count_line(
                company=task.company,
                count_task=task,
                item=custody.item,
                stock_balance=None,
                custody=custody,
                item_code_snapshot=custody.item.item_code,
                item_name_snapshot=custody.item.name,
                department_snapshot=custody.department.name,
                employee_snapshot=(
                    custody.employee.name if custody.employee_id else "部门保管"
                ),
                expected_quantity=custody.current_quantity,
                expected_amount=custody.current_amount,
                expected_unit_cost=custody.unit_cost_snapshot,
                adjustment_unit_cost=None,
            )
    old = _count_task_snapshot(task)
    _base_update(
        SupplyCountTask,
        task.pk,
        {
            "snapshot_at": now,
            "status": SupplyCountStatus.IN_PROGRESS,
            "published_by_id": actor.pk,
            "published_at": now,
        },
        "controlled_supply_count_task_mutation",
    )
    task.refresh_from_db()
    _audit(
        actor=actor,
        action="supply_count_task_publish",
        instance=task,
        old=old,
        new={**_count_task_snapshot(task), "line_count": task.lines.count()},
        request=request,
    )
    return task


@transaction.atomic
def add_supply_count_item(*, task, item, actor, request=None):
    task = _lock_supply_count_task(task)
    require_execute_supply_count_task(actor, task)
    if (
        task.count_domain != SupplyCountDomain.WAREHOUSE_STOCK
        or task.status != SupplyCountStatus.IN_PROGRESS
    ):
        raise ValidationError("只有进行中的仓库盘点可以新增零库存盘盈物品。")
    SupplyWarehouse.objects.select_for_update().get(
        pk=task.warehouse_id, company=task.company
    )
    item = SupplyItem.objects.select_for_update().filter(
        pk=getattr(item, "pk", None), company=task.company, is_active=True
    ).first()
    if item is None:
        raise ValidationError({"item": "物品不属于当前公司或已经停用。"})
    existing = SupplyCountLine.objects.filter(count_task=task, item=item).first()
    if existing is not None:
        return existing
    line = _create_supply_count_line(
        company=task.company,
        count_task=task,
        item=item,
        stock_balance=None,
        custody=None,
        item_code_snapshot=item.item_code,
        item_name_snapshot=item.name,
        department_snapshot="",
        employee_snapshot="",
        expected_quantity=ZERO_QTY,
        expected_amount=ZERO_MONEY,
        expected_unit_cost=ZERO_COST,
        adjustment_unit_cost=None,
    )
    _audit(
        actor=actor,
        action="supply_count_item_add",
        instance=line,
        new={"task_no": task.task_no, "item_id": str(item.pk)},
        request=request,
    )
    return line


@transaction.atomic
def record_supply_count(
    *,
    line,
    counted_quantity,
    remark,
    actor,
    adjustment_unit_cost=None,
    zero_cost_reason="",
    request=None,
):
    task, line = _lock_supply_count_line(line)
    require_record_supply_count(actor, line)
    if task.status != SupplyCountStatus.IN_PROGRESS:
        raise ValidationError("只有进行中的盘点任务可以录入实盘数量。")
    counted = quantize_quantity(counted_quantity)
    if counted < ZERO_QTY:
        raise ValidationError({"counted_quantity": "实盘数量不得小于 0。"})
    difference = quantize_quantity(counted - line.expected_quantity)
    cleaned_remark = str(remark or "").strip()
    if difference != ZERO_QTY and not cleaned_remark:
        raise ValidationError({"remark": "存在盘点差异时必须填写原因。"})
    cost = line.adjustment_unit_cost
    zero_reason = str(zero_cost_reason or "").strip()
    if task.count_domain == SupplyCountDomain.WAREHOUSE_STOCK:
        if line.expected_quantity > ZERO_QTY:
            if adjustment_unit_cost is not None and quantize_unit_cost(
                adjustment_unit_cost
            ) != line.expected_unit_cost:
                raise ValidationError("非零库存盘点成本来自发布快照，不能手工修改。")
            cost = line.expected_unit_cost
            zero_reason = ""
        elif difference > ZERO_QTY:
            if adjustment_unit_cost is not None:
                cost = quantize_unit_cost(adjustment_unit_cost)
                if cost < ZERO_COST:
                    raise ValidationError({"adjustment_unit_cost": "盘盈单位成本不得小于 0。"})
            if cost == ZERO_COST and not zero_reason:
                raise ValidationError({"zero_cost_reason": "零库存盘盈使用 0 成本时必须填写明确原因。"})
        else:
            cost = None
            zero_reason = ""
    else:
        if adjustment_unit_cost is not None or zero_reason:
            raise ValidationError("保管盘点不得录入仓库调整成本。")
        cost = None
    old = _count_line_snapshot(line, include_cost=False)
    now = timezone.now()
    _base_update(
        SupplyCountLine,
        line.pk,
        {
            "counted_quantity": counted,
            "difference_quantity": difference,
            "adjustment_unit_cost": cost,
            "zero_cost_reason": zero_reason,
            "remark": cleaned_remark,
            "counted_by_id": actor.pk,
            "counted_at": now,
        },
        "controlled_supply_count_line_mutation",
    )
    line.refresh_from_db()
    _audit(
        actor=actor,
        action="supply_count_record",
        instance=line,
        old=old,
        new={
            **_count_line_snapshot(line, include_cost=False),
            "task_no": task.task_no,
        },
        request=request,
    )
    return line


@transaction.atomic
def stop_supply_count_entry(*, task, actor, request=None):
    task = _lock_supply_count_task(task)
    require_execute_supply_count_task(actor, task)
    if task.status == SupplyCountStatus.RECONCILIATION:
        return task
    if task.status != SupplyCountStatus.IN_PROGRESS:
        raise ValidationError("只有进行中的盘点任务可以停止录入。")
    lines = list(
        SupplyCountLine.objects.select_for_update()
        .filter(count_task=task)
        .order_by("pk")
    )
    missing = [line.item_code_snapshot for line in lines if line.counted_quantity is None]
    if missing:
        raise ValidationError(
            "所有盘点行必须明确录入实盘数量（包括 0）：" + "、".join(missing[:10])
        )
    for line in lines:
        difference = quantize_quantity(
            line.counted_quantity - line.expected_quantity
        )
        if difference != ZERO_QTY and not str(line.remark or "").strip():
            raise ValidationError(
                {"remark": f"物品 {line.item_code_snapshot} 存在差异，必须填写原因。"}
            )
        if line.difference_quantity != difference:
            _base_update(
                SupplyCountLine,
                line.pk,
                {"difference_quantity": difference},
                "controlled_supply_count_line_mutation",
            )
    old = _count_task_snapshot(task)
    now = timezone.now()
    _base_update(
        SupplyCountTask,
        task.pk,
        {
            "status": SupplyCountStatus.RECONCILIATION,
            "stopped_by_id": actor.pk,
            "stopped_at": now,
        },
        "controlled_supply_count_task_mutation",
    )
    task.refresh_from_db()
    _audit(
        actor=actor,
        action="supply_count_stop",
        instance=task,
        old=old,
        new={
            **_count_task_snapshot(task),
            "difference_line_count": sum(
                1 for line in lines if line.difference_quantity != ZERO_QTY
            ),
        },
        request=request,
    )
    return task


@transaction.atomic
def set_supply_count_adjustment_cost(
    *, line, unit_cost, zero_cost_reason="", actor, request=None
):
    task, line = _lock_supply_count_line(line)
    require_execute_supply_count_task(actor, task)
    if (
        task.status != SupplyCountStatus.RECONCILIATION
        or task.count_domain != SupplyCountDomain.WAREHOUSE_STOCK
        or line.expected_quantity != ZERO_QTY
        or line.difference_quantity is None
        or line.difference_quantity <= ZERO_QTY
    ):
        raise ValidationError("只有差异处理中的零库存盘盈行可以维护调整成本。")
    cost = quantize_unit_cost(unit_cost)
    if cost < ZERO_COST:
        raise ValidationError({"unit_cost": "盘盈单位成本不得小于 0。"})
    reason = str(zero_cost_reason or "").strip()
    if cost == ZERO_COST and not reason:
        raise ValidationError({"zero_cost_reason": "0 成本盘盈必须填写明确的零成本原因。"})
    _base_update(
        SupplyCountLine,
        line.pk,
        {"adjustment_unit_cost": cost, "zero_cost_reason": reason},
        "controlled_supply_count_line_mutation",
    )
    line.refresh_from_db()
    _audit(
        actor=actor,
        action="supply_count_adjustment_cost",
        instance=line,
        new={
            "task_no": task.task_no,
            "item_id": str(line.item_id),
            "unit_cost": str(cost),
            "zero_cost_reason": reason,
        },
        request=request,
    )
    return line


@transaction.atomic
def cancel_supply_count_task(*, task, actor, reason, request=None):
    task = _lock_supply_count_task(task)
    require_execute_supply_count_task(actor, task)
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ValidationError({"reason": "取消盘点必须填写原因。"})
    if task.status == SupplyCountStatus.CANCELLED:
        if task.cancellation_reason != cleaned_reason:
            raise ValidationError("该盘点任务已按另一原因取消。")
        return task
    if task.status == SupplyCountStatus.CLOSED:
        raise ValidationError("已关闭盘点任务不得取消。")
    if task.status not in {
        SupplyCountStatus.DRAFT,
        SupplyCountStatus.IN_PROGRESS,
        SupplyCountStatus.RECONCILIATION,
    }:
        raise ValidationError("当前盘点任务状态不能取消。")
    if task.warehouse_id:
        SupplyWarehouse.objects.select_for_update().get(
            pk=task.warehouse_id, company=task.company
        )
    old = _count_task_snapshot(task)
    now = timezone.now()
    _base_update(
        SupplyCountTask,
        task.pk,
        {
            "status": SupplyCountStatus.CANCELLED,
            "cancelled_by_id": actor.pk,
            "cancelled_at": now,
            "cancellation_reason": cleaned_reason,
        },
        "controlled_supply_count_task_mutation",
    )
    task.refresh_from_db()
    _audit(
        actor=actor,
        action="supply_count_cancel",
        instance=task,
        old=old,
        new={**_count_task_snapshot(task), "reason": cleaned_reason},
        request=request,
    )
    return task


def _validate_count_lines_ready(lines):
    for line in lines:
        if line.counted_quantity is None or line.difference_quantity is None:
            raise ValidationError(
                f"物品 {line.item_code_snapshot} 尚未录入实盘数量。"
            )
        expected_difference = quantize_quantity(
            line.counted_quantity - line.expected_quantity
        )
        if line.difference_quantity != expected_difference:
            raise ValidationError(
                f"物品 {line.item_code_snapshot} 的固定差异与实盘数量不一致。"
            )
        if expected_difference != ZERO_QTY and not str(line.remark or "").strip():
            raise ValidationError(
                f"物品 {line.item_code_snapshot} 存在差异但未填写原因。"
            )


def _create_count_adjustment_document(*, task, lines, actor):
    existing = SupplyDocument.objects.select_for_update().filter(
        source_count_task=task
    ).first()
    if existing is not None:
        return existing
    document = SupplyDocument(
        company=task.company,
        document_no=_next_supply_document_no(
            company=task.company,
            document_type=SupplyDocumentType.COUNT_ADJUSTMENT,
            business_date=timezone.localdate(),
        ),
        document_type=SupplyDocumentType.COUNT_ADJUSTMENT,
        business_date=timezone.localdate(),
        source_warehouse=None,
        target_warehouse=None,
        department=None,
        employee=None,
        status=SupplyDocumentStatus.DRAFT,
        idempotency_key=f"supply-count-close:{task.pk}",
        source_count_task=task,
        created_by=actor,
        remark=f"由盘点任务 {task.task_no} 自动生成",
    )
    document.full_clean()
    _enable_capability("controlled_supply_count_adjustment_insert")
    try:
        document.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("该盘点任务的调整单已经存在。") from exc
    prepared = []
    mapped_lines = []
    for count_line in lines:
        difference = count_line.difference_quantity
        if difference == ZERO_QTY:
            continue
        direction = (
            "increase" if difference > ZERO_QTY else "decrease"
        )
        entered_cost = None
        line_remark = str(count_line.remark or "").strip()
        if direction == "increase":
            if count_line.expected_quantity > ZERO_QTY:
                entered_cost = count_line.expected_unit_cost
            else:
                if count_line.adjustment_unit_cost is None:
                    raise ValidationError(
                        f"零库存盘盈物品 {count_line.item_code_snapshot} 必须填写单位成本。"
                    )
                entered_cost = count_line.adjustment_unit_cost
                if entered_cost == ZERO_COST:
                    zero_reason = str(count_line.zero_cost_reason or "").strip()
                    if not zero_reason:
                        raise ValidationError(
                            f"零库存盘盈物品 {count_line.item_code_snapshot} 使用 0 成本时必须填写原因。"
                        )
                    line_remark = f"{line_remark}；零成本原因：{zero_reason}"
        prepared.append(
            {
                "line_no": len(prepared) + 1,
                "item": count_line.item,
                "quantity": abs(difference),
                "entered_unit_cost": entered_cost,
                "adjustment_direction": direction,
                "source_issue_line": None,
                "source_custody": None,
                "line_remark": line_remark,
            }
        )
        mapped_lines.append(count_line)
    document_lines = _create_document_lines(
        document=document, prepared_lines=prepared
    )
    document._count_line_map = list(zip(mapped_lines, document_lines, strict=True))
    return document


def _close_warehouse_supply_count(*, task, lines, actor, request=None):
    current_balances = list(
        SupplyStockBalance.objects.select_for_update()
        .select_related("item")
        .filter(company=task.company, warehouse_id=task.warehouse_id)
        .order_by("item_id", "pk")
    )
    line_by_item = {line.item_id: line for line in lines}
    unexpected = [
        balance.item.item_code
        for balance in current_balances
        if balance.item_id not in line_by_item
    ]
    if unexpected:
        raise ValidationError(
            "当前仓库余额与盘点快照不一致，不能自动关闭。"
            "请先执行库存余额核对并检查是否存在绕过冻结的业务。"
        )
    balance_by_item = {balance.item_id: balance for balance in current_balances}
    for line in lines:
        balance = balance_by_item.get(line.item_id)
        current_quantity = balance.quantity_on_hand if balance else ZERO_QTY
        current_amount = balance.amount_on_hand if balance else ZERO_MONEY
        current_cost = balance.average_unit_cost if balance else ZERO_COST
        if (
            current_quantity != line.expected_quantity
            or current_amount != line.expected_amount
            or current_cost != line.expected_unit_cost
        ):
            raise ValidationError(
                "当前仓库余额与盘点快照不一致，不能自动关闭。"
                "请先执行库存余额核对并检查是否存在绕过冻结的业务。"
            )
        if balance is None:
            balance = _lock_or_create_balance(
                company=task.company,
                warehouse=task.warehouse,
                item=line.item,
            )
            balance_by_item[line.item_id] = balance
    differences = [
        line for line in lines if line.difference_quantity != ZERO_QTY
    ]
    if not differences:
        return None
    document = _create_count_adjustment_document(
        task=task,
        lines=lines,
        actor=actor,
    )
    if document.status == SupplyDocumentStatus.DRAFT:
        _post_supply_document_internal(
            document=document,
            actor=actor,
            request=request,
            source_count_task=task,
        )
    mapping = getattr(document, "_count_line_map", None)
    if mapping is None:
        document_lines = list(document.lines.order_by("line_no"))
        mapping = list(zip(differences, document_lines, strict=True))
    resolved_at = timezone.now()
    for count_line, document_line in mapping:
        _base_update(
            SupplyCountLine,
            count_line.pk,
            {
                "adjustment_document_line_id": document_line.pk,
                "resolved_by_id": actor.pk,
                "resolved_at": resolved_at,
            },
            "controlled_supply_count_line_mutation",
        )
    _audit(
        actor=actor,
        action="supply_count_adjustment_posted",
        instance=document,
        new={
            "task_no": task.task_no,
            "document_no": document.document_no,
            "difference_line_count": len(differences),
            "difference_quantity": str(
                sum((line.difference_quantity for line in differences), ZERO_QTY)
            ),
        },
        request=request,
    )
    return document


def _resolution_type_for_movement(movement):
    return {
        SupplyCustodyAction.RETURN: SupplyCountResolutionType.RETURN,
        SupplyCustodyAction.TRANSFER: SupplyCountResolutionType.TRANSFER,
        SupplyCustodyAction.LOSS: SupplyCountResolutionType.LOSS,
        SupplyCustodyAction.SCRAP: SupplyCountResolutionType.SCRAP,
        SupplyCustodyAction.CORRECTION: SupplyCountResolutionType.CORRECTION,
    }.get(movement.action)


def _validate_custody_count_resolution(line):
    movement = line.resolution_custody_movement
    difference = line.difference_quantity
    if difference == ZERO_QTY:
        if movement is not None or line.resolution_type is not None:
            raise ValidationError("无差异保管盘点行不得伪造解决证据。")
        return
    if movement is None:
        raise ValidationError(
            f"物品 {line.item_code_snapshot} 的保管差异尚未关联真实解决动作。"
        )
    if movement.created_at < line.count_task.stopped_at:
        raise ValidationError("保管差异解决动作必须发生在停止录入之后。")
    if movement.quantity != abs(difference):
        raise ValidationError("保管差异解决动作数量必须精确等于差异绝对值。")
    expected_type = _resolution_type_for_movement(movement)
    if expected_type != line.resolution_type:
        raise ValidationError("保管解决流水动作与盘点解决方式不一致。")
    if difference < ZERO_QTY:
        if movement.from_custody_id != line.custody_id:
            raise ValidationError("盘亏解决动作必须从发布快照的来源保管减少。")
        if movement.action == SupplyCustodyAction.CORRECTION:
            if movement.to_custody_id is not None:
                raise ValidationError("负向盘点更正流水方向错误。")
        elif movement.action == SupplyCustodyAction.TRANSFER:
            if movement.to_custody_id is None:
                raise ValidationError("转交解决必须关联目标保管记录。")
        elif movement.to_custody_id is not None:
            raise ValidationError("归还、报损或报废解决流水方向错误。")
    else:
        if (
            movement.action != SupplyCustodyAction.CORRECTION
            or movement.from_custody_id is not None
            or movement.to_custody_id != line.custody_id
        ):
            raise ValidationError("保管盘盈只能由指向原保管的正向盘点更正解决。")
    custody = line.custody
    if custody.current_quantity != line.counted_quantity:
        raise ValidationError("当前保管数量与盘点解决后的实盘数量不一致。")
    if line.counted_quantity == ZERO_QTY:
        if custody.status != SupplyCustodyStatus.CLOSED or custody.current_amount != ZERO_MONEY:
            raise ValidationError("实盘为 0 的保管记录必须已结清且金额归零。")
    elif custody.status != SupplyCustodyStatus.OPEN:
        raise ValidationError("实盘仍有数量的保管记录必须保持在管。")


@transaction.atomic
def close_supply_count_task(*, task, actor, request=None):
    task = _lock_supply_count_task(task)
    require_execute_supply_count_task(actor, task)
    if task.status == SupplyCountStatus.CLOSED:
        return task
    if task.status != SupplyCountStatus.RECONCILIATION:
        raise ValidationError("只有差异处理中的盘点任务可以关闭。")
    if task.count_domain == SupplyCountDomain.WAREHOUSE_STOCK:
        task.warehouse = SupplyWarehouse.objects.select_for_update().get(
            pk=task.warehouse_id, company=task.company
        )
    line_queryset = SupplyCountLine.objects.select_for_update()
    if connection.vendor == "postgresql":
        line_queryset = line_queryset.select_for_update(of=("self",))
    lines = list(
        line_queryset.select_related(
            "item",
            "custody",
            "resolution_custody_movement",
            "adjustment_document_line",
        )
        .filter(count_task=task)
        .order_by("item_id", "pk")
    )
    for line in lines:
        line.count_task = task
    _validate_count_lines_ready(lines)
    adjustment_document = None
    if task.count_domain == SupplyCountDomain.WAREHOUSE_STOCK:
        active = SupplyCountTask.objects.filter(
            pk=task.pk,
            warehouse_id=task.warehouse_id,
            status=SupplyCountStatus.RECONCILIATION,
        ).exists()
        if not active:
            raise ValidationError("当前仓库不再由该盘点任务冻结，不能关闭。")
        adjustment_document = _close_warehouse_supply_count(
            task=task,
            lines=lines,
            actor=actor,
            request=request,
        )
    else:
        custody_ids = sorted(
            [line.custody_id for line in lines if line.custody_id], key=str
        )
        locked_custodies = {
            custody.pk: custody
            for custody in SupplyCustody.objects.select_for_update()
            .filter(company=task.company, pk__in=custody_ids)
            .order_by("pk")
        }
        for line in lines:
            line.custody = locked_custodies.get(line.custody_id)
            if line.custody is None:
                raise ValidationError("盘点快照中的保管记录不存在或公司不一致。")
            _validate_custody_count_resolution(line)
    old = _count_task_snapshot(task)
    now = timezone.now()
    _base_update(
        SupplyCountTask,
        task.pk,
        {
            "status": SupplyCountStatus.CLOSED,
            "closed_by_id": actor.pk,
            "closed_at": now,
        },
        "controlled_supply_count_task_mutation",
    )
    task.refresh_from_db()
    _audit(
        actor=actor,
        action="supply_count_close",
        instance=task,
        old=old,
        new={
            **_count_task_snapshot(task),
            "adjustment_document_id": (
                str(adjustment_document.pk) if adjustment_document else None
            ),
            "adjustment_document_no": (
                adjustment_document.document_no if adjustment_document else None
            ),
        },
        request=request,
    )
    return task


def _active_count_line_for_custody(custody):
    return (
        SupplyCountLine.objects.select_related("count_task")
        .filter(
            company=custody.company,
            custody=custody,
            count_task__status__in=ACTIVE_SUPPLY_COUNT_STATUSES,
        )
        .order_by("pk")
        .first()
    )


def _assert_custody_count_action_allowed(
    *, custody, count_line=None, action_quantity=None
):
    active_line = _active_count_line_for_custody(custody)
    if active_line is None:
        if count_line is not None:
            raise ValidationError("指定盘点行当前未冻结该保管记录。")
        return None
    if active_line.count_task.status == SupplyCountStatus.IN_PROGRESS:
        raise ValidationError("该保管记录正在进行耐用品盘点，停止录入前不能执行保管动作。")
    if count_line is None or active_line.pk != count_line.pk:
        raise ValidationError(
            "该保管记录正在处理盘点差异，普通保管动作已冻结；"
            "请从对应盘点差异行发起解决。"
        )
    if active_line.count_task.status != SupplyCountStatus.RECONCILIATION:
        raise ValidationError("当前盘点任务不在差异处理状态。")
    if active_line.difference_quantity is None or active_line.difference_quantity >= ZERO_QTY:
        raise ValidationError("普通归还、转交、报损或报废只能解决保管盘亏差异。")
    if active_line.resolution_custody_movement_id:
        raise ValidationError("该盘点差异已经关联解决动作。")
    if action_quantity is not None and quantize_quantity(action_quantity) != abs(
        active_line.difference_quantity
    ):
        raise ValidationError("保管差异解决数量必须精确等于差异绝对值。")
    return active_line


def _resolve_supply_count_line_with_movement(
    *, count_line, movement, actor, request=None
):
    task, line = _lock_supply_count_line(count_line)
    require_execute_supply_count_task(actor, task)
    if task.status != SupplyCountStatus.RECONCILIATION:
        raise ValidationError("只有差异处理中的保管盘点行可以关联解决流水。")
    if task.count_domain != SupplyCountDomain.CUSTODY or not line.custody_id:
        raise ValidationError("目标不是耐用品保管盘点行。")
    if line.resolution_custody_movement_id:
        if line.resolution_custody_movement_id == movement.pk:
            return line
        raise ValidationError("该盘点差异已经由另一保管流水解决。")
    if movement.company_id != line.company_id or movement.item_id != line.item_id:
        raise ValidationError("保管解决流水不属于盘点公司或物品。")
    if movement.quantity != abs(line.difference_quantity or ZERO_QTY):
        raise ValidationError("保管解决流水数量必须精确等于差异绝对值。")
    resolution_type = _resolution_type_for_movement(movement)
    if resolution_type is None:
        raise ValidationError("该保管流水动作不能作为盘点差异解决证据。")
    if line.difference_quantity < ZERO_QTY:
        if movement.from_custody_id != line.custody_id:
            raise ValidationError("盘亏解决流水必须从盘点来源保管转出。")
    elif line.difference_quantity > ZERO_QTY:
        if (
            movement.action != SupplyCustodyAction.CORRECTION
            or movement.from_custody_id is not None
            or movement.to_custody_id != line.custody_id
        ):
            raise ValidationError("保管盘盈只能使用正向盘点更正解决。")
    else:
        raise ValidationError("无差异盘点行不得关联解决流水。")
    resolved_at = timezone.now()
    _base_update(
        SupplyCountLine,
        line.pk,
        {
            "resolution_type": resolution_type,
            "resolution_custody_movement_id": movement.pk,
            "resolved_by_id": actor.pk,
            "resolved_at": resolved_at,
        },
        "controlled_supply_count_line_mutation",
    )
    line.refresh_from_db()
    _audit(
        actor=actor,
        action="supply_count_custody_resolve",
        instance=line,
        new={
            "task_no": task.task_no,
            "resolution_type": resolution_type,
            "movement_id": str(movement.pk),
            "quantity": str(movement.quantity),
        },
        request=request,
    )
    return line


@transaction.atomic
def correct_custody_for_count(
    *, count_line, actor, reason, idempotency_key, request=None
):
    task, line = _lock_supply_count_line(count_line)
    require_execute_supply_count_task(actor, task)
    if (
        task.count_domain != SupplyCountDomain.CUSTODY
        or task.status != SupplyCountStatus.RECONCILIATION
        or line.custody_id is None
    ):
        raise ValidationError("盘点专用更正只能处理差异处理中的耐用品保管行。")
    if line.resolution_custody_movement_id:
        existing = line.resolution_custody_movement
        if (
            existing.action == SupplyCustodyAction.CORRECTION
            and existing.idempotency_key == str(idempotency_key or "").strip()
        ):
            return existing
        raise ValidationError("该盘点差异已经解决。")
    difference = line.difference_quantity
    if difference is None or difference == ZERO_QTY:
        raise ValidationError("只有有差异的保管盘点行可以执行更正。")
    cleaned_reason = str(reason or "").strip()
    key = str(idempotency_key or "").strip()
    if not cleaned_reason:
        raise ValidationError({"reason": "盘点更正原因不能为空。"})
    if not key:
        raise ValidationError({"idempotency_key": "盘点更正幂等键不能为空。"})
    custody = (
        SupplyCustody.objects.select_for_update(of=("self",))
        if connection.vendor == "postgresql"
        else SupplyCustody.objects.select_for_update()
    ).select_related("item", "department", "employee").get(
        pk=line.custody_id, company=task.company
    )
    existing = SupplyCustodyMovement.objects.select_for_update().filter(
        company=task.company, idempotency_key=key
    ).first()
    if existing is not None:
        if (
            existing.action == SupplyCustodyAction.CORRECTION
            and existing.quantity == abs(difference)
            and line.custody_id
            in {existing.from_custody_id, existing.to_custody_id}
            and existing.reason == cleaned_reason
        ):
            _resolve_supply_count_line_with_movement(
                count_line=line,
                movement=existing,
                actor=actor,
                request=request,
            )
            return existing
        raise ValidationError("同一保管动作幂等键已用于不同内容。")
    _assert_custody_count_action_allowed(
        custody=custody,
        count_line=line,
        action_quantity=abs(difference),
    ) if difference < ZERO_QTY else None
    old = _custody_snapshot(custody)
    now = timezone.now()
    if difference > ZERO_QTY:
        quantity = quantize_quantity(difference)
        amount = quantize_money(quantity * custody.unit_cost_snapshot)
        quantity_after = quantize_quantity(custody.current_quantity + quantity)
        amount_after = quantize_money(custody.current_amount + amount)
        from_custody = None
        to_custody = custody
        status = SupplyCustodyStatus.OPEN
    else:
        allocation = allocate_custody_amount(
            current_quantity=custody.current_quantity,
            current_amount=custody.current_amount,
            unit_cost_snapshot=custody.unit_cost_snapshot,
            action_quantity=abs(difference),
        )
        quantity = allocation.action_quantity
        amount = allocation.action_amount
        quantity_after = allocation.quantity_after
        amount_after = allocation.amount_after
        from_custody = custody
        to_custody = None
        status = (
            SupplyCustodyStatus.CLOSED
            if quantity_after == ZERO_QTY
            else SupplyCustodyStatus.OPEN
        )
    _update_custody_values(
        custody=custody,
        quantity=quantity_after,
        amount=amount_after,
        status=status,
        updated_at=now,
    )
    movement = _create_custody_movement(
        values={
            "company": task.company,
            "item": custody.item,
            "from_custody": from_custody,
            "to_custody": to_custody,
            "action": SupplyCustodyAction.CORRECTION,
            "quantity": quantity,
            "amount": amount,
            "unit_cost": custody.unit_cost_snapshot,
            "business_date": timezone.localdate(),
            "reason": cleaned_reason,
            "created_by": actor,
            "idempotency_key": key,
        }
    )
    _resolve_supply_count_line_with_movement(
        count_line=line,
        movement=movement,
        actor=actor,
        request=request,
    )
    _audit(
        actor=actor,
        action="supply_count_custody_correction",
        instance=movement,
        old=old,
        new={
            **_custody_snapshot(custody),
            "task_no": task.task_no,
            "direction": "increase" if difference > ZERO_QTY else "decrease",
            "quantity": str(quantity),
            "amount": str(amount),
            "reason": cleaned_reason,
        },
        request=request,
    )
    return movement


@transaction.atomic
def return_custody_for_count(
    *,
    count_line,
    target_warehouse,
    business_date,
    reason,
    actor,
    idempotency_key,
    request=None,
):
    task, line = _lock_supply_count_line(count_line)
    require_execute_supply_count_task(actor, task)
    if (
        task.status != SupplyCountStatus.RECONCILIATION
        or task.count_domain != SupplyCountDomain.CUSTODY
        or line.custody_id is None
        or line.difference_quantity is None
        or line.difference_quantity >= ZERO_QTY
    ):
        raise ValidationError("归还只能解决差异处理中的保管盘亏行。")
    document = return_custody_to_warehouse(
        custody=line.custody,
        target_warehouse=target_warehouse,
        quantity=abs(line.difference_quantity),
        business_date=business_date,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
        count_line=line,
        request=request,
    )
    _post_supply_document_internal(
        document=document,
        actor=actor,
        request=request,
        source_count_line=line,
    )
    return SupplyCustodyMovement.objects.get(
        company=task.company,
        source_document_line__document=document,
        source_document_line__source_custody_id=line.custody_id,
        action=SupplyCustodyAction.RETURN,
    )


def _create_employee_supply_clearance_item(*, clearance, custody, actor, request=None):
    item = EmployeeSupplyClearanceItem(
        clearance=clearance,
        company=clearance.company,
        custody=custody,
        item_code_snapshot=custody.item.item_code,
        item_name_snapshot=custody.item.name,
        quantity_snapshot=custody.current_quantity,
        amount_snapshot=custody.current_amount,
        department_snapshot=custody.department.name,
        employee_snapshot=custody.employee.name,
        resolution=EmployeeSupplyClearanceResolution.PENDING,
    )
    item._controlled_insert = True
    _enable_capability("controlled_employee_supply_clearance_item_insert")
    item.full_clean()
    try:
        item.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("该保管记录已经纳入本离职清退单。") from exc
    _audit(
        actor=actor,
        action="employee_supply_clearance_item_create",
        instance=item,
        new={
            "clearance_id": str(clearance.pk),
            "custody_id": str(custody.pk),
            "item_code": item.item_code_snapshot,
            "quantity": str(item.quantity_snapshot),
            "amount": str(item.amount_snapshot),
        },
        request=request,
    )
    return item


@transaction.atomic
def create_supply_clearance_items(
    *, clearance, actor, historical_only=False, request=None
):
    """Add authoritative open personal durable custodies to one locked clearance."""

    custody_qs = (
        SupplyCustody.objects.select_for_update(of=("self",))
        if connection.vendor == "postgresql"
        else SupplyCustody.objects.select_for_update()
    )
    custodies = list(
        custody_qs.select_related("item", "department", "employee")
        .filter(
            company=clearance.company,
            employee=clearance.employee,
            status=SupplyCustodyStatus.OPEN,
            current_quantity__gt=ZERO_QTY,
            item__item_type=SupplyItemType.DURABLE_QUANTITY,
        )
        .order_by("pk")
    )
    existing_ids = set(
        EmployeeSupplyClearanceItem.objects.select_for_update()
        .filter(clearance=clearance)
        .values_list("custody_id", flat=True)
    )
    if clearance.supplements_clearance_id:
        existing_ids.update(
            EmployeeSupplyClearanceItem.objects.filter(
                clearance=clearance.supplements_clearance
            ).values_list("custody_id", flat=True)
        )
    created = []
    post_initiation = []
    for custody in custodies:
        if custody.pk in existing_ids:
            continue
        association_cutoff = clearance.initiated_at
        if clearance.supplements_clearance_id:
            association_cutoff = clearance.supplements_clearance.initiated_at
        association_date = timezone.localtime(association_cutoff).date()
        is_post_initiation = (
            custody.created_at > association_cutoff
            and custody.started_on > association_date
        )
        if historical_only and is_post_initiation:
            post_initiation.append(str(custody.pk))
            continue
        created.append(
            _create_employee_supply_clearance_item(
                clearance=clearance,
                custody=custody,
                actor=actor,
                request=request,
            )
        )
    if post_initiation:
        raise ValidationError(
            "存在清退发起后新增给离职员工的数量型低值耐用品保管关系，"
            "必须先纠正后才能继续：" + "、".join(post_initiation)
        )
    return created


@transaction.atomic
def resolve_supply_clearance_items_for_movement(*, movement, request=None):
    if movement.action not in {
        SupplyCustodyAction.RETURN,
        SupplyCustodyAction.TRANSFER,
        SupplyCustodyAction.LOSS,
        SupplyCustodyAction.SCRAP,
    } or movement.from_custody_id is None:
        return []
    actor = movement.created_by
    if actor is None:
        raise ValidationError("保管动作缺少操作人，不能作为离职清退证据。")
    custody_qs = (
        SupplyCustody.objects.select_for_update(of=("self",))
        if connection.vendor == "postgresql"
        else SupplyCustody.objects.select_for_update()
    )
    source = custody_qs.select_related("employee", "item").get(
        pk=movement.from_custody_id,
        company=movement.company,
    )
    if source.status != SupplyCustodyStatus.CLOSED or source.current_quantity != ZERO_QTY:
        return []
    if movement.action == SupplyCustodyAction.TRANSFER:
        target = movement.to_custody
        if target is None or target.employee_id == source.employee_id:
            return []
    resolution = {
        SupplyCustodyAction.RETURN: EmployeeSupplyClearanceResolution.RETURNED,
        SupplyCustodyAction.TRANSFER: EmployeeSupplyClearanceResolution.TRANSFERRED,
        SupplyCustodyAction.LOSS: EmployeeSupplyClearanceResolution.LOST,
        SupplyCustodyAction.SCRAP: EmployeeSupplyClearanceResolution.SCRAPPED,
    }[movement.action]
    items = list(
        EmployeeSupplyClearanceItem.objects.select_for_update()
        .select_related("clearance")
        .filter(
            company=movement.company,
            custody=source,
            resolution=EmployeeSupplyClearanceResolution.PENDING,
            clearance__status__in=("open", "blocked"),
        )
        .order_by("pk")
    )
    clearances = {}
    for item in items:
        if movement.action == SupplyCustodyAction.TRANSFER:
            target = movement.to_custody
            if target.employee_id == item.clearance.employee_id:
                continue
        _enable_capability("controlled_employee_supply_clearance_item_resolution")
        _base_update(
            EmployeeSupplyClearanceItem,
            item.pk,
            {
                "resolution": resolution,
                "resolved_by_id": actor.pk,
                "resolved_at": movement.created_at,
                "custody_movement_id": movement.pk,
            },
            "controlled_employee_supply_clearance_item_resolution",
        )
        item.refresh_from_db()
        clearances[item.clearance_id] = item.clearance
        _audit(
            actor=actor,
            action="employee_supply_clearance_item_resolve",
            instance=item,
            old={"resolution": "pending"},
            new={
                "resolution": resolution,
                "movement_id": str(movement.pk),
                "custody_id": str(source.pk),
            },
            request=request,
        )
    if clearances:
        from apps.offboarding.services import _recount

        for clearance in clearances.values():
            _recount(clearance)
    return items
