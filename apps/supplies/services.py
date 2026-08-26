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
    calculate_issue,
    calculate_receipt,
    calculate_receipt_from_amount,
    quantize_money,
    quantize_quantity,
    quantize_unit_cost,
    validate_zero_cost_reason,
)
from .models import (
    SupplyCategory,
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
    require_create_supply_document,
    require_manage_supply_category,
    require_manage_supply_item,
    require_manage_supply_warehouse,
    require_post_supply_document,
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
    SupplyDocumentType.REVERSAL: "CX",
}


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
            if source_issue_line is None:
                raise ValidationError(
                    {"source_issue_line": f"第 {line_no} 行必须关联原领用明细。"}
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
            ):
                raise ValidationError(
                    {"source_issue_line": f"第 {line_no} 行只能关联有效的已过账领用明细。"}
                )
            if source_issue_line.item.item_type != SupplyItemType.CONSUMABLE:
                raise ValidationError(
                    {"source_issue_line": "Sprint 15 只允许低值易耗品退回；数量型耐用品归还尚未开放。"}
                )
            if source_issue_line.pk in seen_return_sources:
                raise ValidationError("同一退回单不能重复引用同一原领用明细。")
            seen_return_sources.add(source_issue_line.pk)
            if item is not None and item.pk != source_issue_line.item_id:
                raise ValidationError({"item": "退回物品由原领用明细确定，不能替换。"})
            item = source_issue_line.item
        elif source_issue_line is not None:
            raise ValidationError(
                {"source_issue_line": f"第 {line_no} 行当前单据类型不得关联原领用明细。"}
            )
        if source_custody is not None:
            raise ValidationError(
                {"source_custody": "Sprint 15 尚未开放数量型耐用品归还。"}
            )
        if item is None:
            raise ValidationError({"item": f"第 {line_no} 行必须选择物品。"})
        if item.company_id != company.pk:
            raise ValidationError({"item": f"第 {line_no} 行物品不属于当前公司。"})
        if document_type != SupplyDocumentType.RETURN and not item.is_active:
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
            raise ValidationError(f"第 {line_no} 行包含 Sprint 15 尚未开放的盘点字段。")
        prepared.append(
            {
                "line_no": line_no,
                "item": item,
                "quantity": quantity,
                "entered_unit_cost": entered_unit_cost,
                "adjustment_direction": None,
                "source_issue_line": source_issue_line,
                "source_custody": None,
                "line_remark": line_remark,
            }
        )
    if not prepared:
        raise ValidationError("库存单据至少需要一条有效明细。")
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
    require_create_supply_document(actor)
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
    require_create_supply_document(actor)
    _require_current_company(document.company)
    document = (
        SupplyDocument.objects.select_for_update(of=("self",))
        .select_related("company", "target_warehouse")
        .get(pk=document.pk, company=document.company)
    )
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
    require_create_supply_document(actor)
    _require_current_company(document.company)
    document = (
        SupplyDocument.objects.select_for_update(of=("self",))
        .select_related("company")
        .get(pk=document.pk, company=document.company)
    )
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
        raise ValidationError("该耐用品领用明细已经建立保管记录。") from exc
    return custody


def _create_custody_movement(*, values):
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
    return requests


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


@transaction.atomic
def post_supply_document(
    *, document, actor, idempotency_key=None, request=None
):
    require_post_supply_document(actor)
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
        )
        .get(pk=document.pk, company=document.company)
    )
    if document.status == SupplyDocumentStatus.POSTED:
        return document
    if document.status != SupplyDocumentStatus.DRAFT:
        raise ValidationError("只有草稿单据可以过账；已取消或已冲销单据不可恢复。")
    if document.document_type not in SPRINT15_DOCUMENT_TYPES:
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
    for line in lines:
        if line.company_id != document.company_id or line.item.company_id != document.company_id:
            raise ValidationError(f"第 {line.line_no} 行公司边界不一致。")
        if document.document_type != SupplyDocumentType.RETURN and not line.item.is_active:
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
        ledgers, custodies = _post_consumable_return(
            document=document,
            lines=lines,
            balances=balances,
            actor=actor,
            posted_at=posted_at,
        )
    else:
        ledgers, custodies = _post_transfer(
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
    require_reverse_supply_document(actor)
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
            "custody_count": len(custody_entries),
        },
        request=request,
    )
    return reversal
