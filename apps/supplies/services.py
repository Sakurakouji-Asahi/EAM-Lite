"""Controlled low-value supplies master-data and Sprint 14 stock services."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company

from .domain import (
    ZERO_COST,
    ZERO_MONEY,
    ZERO_QTY,
    calculate_receipt,
    quantize_quantity,
    quantize_unit_cost,
    validate_zero_cost_reason,
)
from .models import (
    SupplyCategory,
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
        "line_remark",
    }
)
SPRINT14_DOCUMENT_TYPES = frozenset(
    {SupplyDocumentType.OPENING, SupplyDocumentType.RECEIPT}
)
DOCUMENT_PREFIXES = {
    SupplyDocumentType.OPENING: "QC",
    SupplyDocumentType.RECEIPT: "RK",
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
    return item.stock_ledgers.exists() or item.document_lines.filter(
        document__status__in=(
            SupplyDocumentStatus.POSTED,
            SupplyDocumentStatus.REVERSED,
        )
    ).exists()


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


def _prepare_document_lines(*, company, lines):
    prepared = []
    for line_no, source in enumerate(lines or (), 1):
        values = dict(source)
        unknown = set(values).difference(DOCUMENT_LINE_FIELDS)
        if unknown:
            raise ValidationError(
                {
                    field: "不是 Sprint 14 入库单可编辑的明细字段。"
                    for field in unknown
                }
            )
        item = values.get("item")
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
            entered_unit_cost, values.get("line_remark")
        )
        if values.get("adjustment_direction") or values.get("source_issue_line"):
            raise ValidationError(f"第 {line_no} 行包含 Sprint 14 尚未开放的业务字段。")
        prepared.append(
            {
                "line_no": line_no,
                "item": item,
                "quantity": quantity,
                "entered_unit_cost": entered_unit_cost,
                "adjustment_direction": None,
                "source_issue_line": None,
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
        raise ValidationError("Sprint 14 只允许期初入库和日常入库发号。")
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
    if document_type not in SPRINT14_DOCUMENT_TYPES:
        raise ValidationError("Sprint 14 只允许创建期初入库和日常入库单。")
    require_create_supply_document(actor)
    _require_current_company(company)
    unknown = set(values).difference(DOCUMENT_DRAFT_FIELDS | {"idempotency_key"})
    if unknown:
        raise ValidationError(
            {field: "不是 Sprint 14 入库单可编辑字段。" for field in unknown}
        )
    values["business_date"] = _coerce_business_date(values.get("business_date"))
    idempotency_key = str(values.pop("idempotency_key", "") or "").strip()
    if not idempotency_key:
        raise ValidationError({"idempotency_key": "创建幂等键不能为空。"})
    target = values.get("target_warehouse")
    if target is None:
        raise ValidationError({"target_warehouse": "必须选择目标仓库。"})
    if target.company_id != company.pk:
        raise ValidationError({"target_warehouse": "目标仓库不属于当前公司。"})
    if not target.is_active:
        raise ValidationError({"target_warehouse": "停用仓库不能用于新增业务单据。"})
    prepared_lines = _prepare_document_lines(company=company, lines=lines)

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
    prepared_lines = _prepare_document_lines(company=document.company, lines=lines)
    old = _document_snapshot(document)
    values = dict(data)
    if "business_date" in values:
        values["business_date"] = _coerce_business_date(values["business_date"])
    target = values.get("target_warehouse", document.target_warehouse)
    if target is None or target.company_id != document.company_id:
        raise ValidationError({"target_warehouse": "目标仓库必须属于当前公司。"})
    if not target.is_active:
        raise ValidationError({"target_warehouse": "停用仓库不能用于新增业务单据。"})
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
    values = {
        "quantity_on_hand": calculation.quantity_after,
        "amount_on_hand": calculation.amount_after,
        "average_unit_cost": calculation.average_unit_cost_after,
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
    ledger.full_clean()
    try:
        ledger.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("该过账明细已存在库存流水，已阻止重复入库。") from exc
    return ledger


@transaction.atomic
def post_supply_document(
    *, document, actor, idempotency_key=None, request=None
):
    require_post_supply_document(actor)
    _require_current_company(document.company)
    document = (
        SupplyDocument.objects.select_for_update(of=("self",))
        .select_related("company", "target_warehouse")
        .get(pk=document.pk, company=document.company)
    )
    require_post_supply_document(actor)
    if document.status == SupplyDocumentStatus.POSTED:
        return document
    if document.status != SupplyDocumentStatus.DRAFT:
        raise ValidationError("只有草稿单据可以过账；已取消单据不可恢复。")
    if document.document_type not in SPRINT14_DOCUMENT_TYPES:
        raise ValidationError("Sprint 14 仅允许过账期初入库和日常入库。")
    if idempotency_key is not None and not str(idempotency_key).strip():
        raise ValidationError("过账幂等键不能为空。")
    warehouse = document.target_warehouse
    if warehouse is None or warehouse.company_id != document.company_id:
        raise ValidationError("目标仓库缺失或不属于当前公司。")
    if not warehouse.is_active:
        raise ValidationError("目标仓库已停用，不能过账；请取消草稿。")
    lines = list(
        document.lines.select_for_update()
        .select_related("item", "document")
        .order_by("line_no")
    )
    if not lines:
        raise ValidationError("库存单据至少需要一条明细。")
    for line in lines:
        if line.company_id != document.company_id or line.item.company_id != document.company_id:
            raise ValidationError(f"第 {line.line_no} 行公司边界不一致。")
        if not line.item.is_active:
            raise ValidationError(f"第 {line.line_no} 行物品已停用，不能过账。")
        if line.entered_unit_cost is None:
            raise ValidationError(f"第 {line.line_no} 行缺少录入单位成本。")
        validate_zero_cost_reason(line.entered_unit_cost, line.line_remark)
        if line.stock_ledgers.exists():
            raise ValidationError(f"第 {line.line_no} 行已存在库存流水，不能重复过账。")

    balance_keys = sorted(
        {(str(warehouse.pk), str(line.item_id)): line.item for line in lines}.items(),
        key=lambda entry: entry[0],
    )
    balances = {}
    for (_, item_id), item in balance_keys:
        balances[item_id] = _lock_or_create_balance(
            company=document.company,
            warehouse=warehouse,
            item=item,
        )

    old = _document_snapshot(document)
    posted_at = timezone.now()
    movement_type = (
        SupplyStockMovementType.OPENING_IN
        if document.document_type == SupplyDocumentType.OPENING
        else SupplyStockMovementType.RECEIPT_IN
    )
    ledgers = []
    total_amount = ZERO_MONEY
    for line in lines:
        balance = balances[str(line.item_id)]
        quantity_before = balance.quantity_on_hand
        amount_before = balance.amount_on_hand
        average_before = balance.average_unit_cost
        calculation = calculate_receipt(
            quantity_before,
            amount_before,
            line.quantity,
            line.entered_unit_cost,
        )
        _update_balance(
            balance=balance,
            calculation=calculation,
            updated_at=posted_at,
        )
        line.posted_unit_cost = calculation.receipt_unit_cost
        line.posted_amount = calculation.receipt_amount
        line._controlled_posting = True
        line.full_clean()
        line.save(update_fields=("posted_unit_cost", "posted_amount"))
        ledgers.append(
            _create_stock_ledger(
                values={
                    "company": document.company,
                    "warehouse": warehouse,
                    "item": line.item,
                    "document": document,
                    "document_line": line,
                    "movement_type": movement_type,
                    "quantity_delta": calculation.receipt_quantity,
                    "amount_delta": calculation.receipt_amount,
                    "unit_cost": calculation.receipt_unit_cost,
                    "quantity_before": quantity_before,
                    "quantity_after": calculation.quantity_after,
                    "amount_before": amount_before,
                    "amount_after": calculation.amount_after,
                    "average_unit_cost_before": average_before,
                    "average_unit_cost_after": calculation.average_unit_cost_after,
                    "occurred_at": posted_at,
                    "created_by": actor,
                }
            )
        )
        total_amount += calculation.receipt_amount

    document.status = SupplyDocumentStatus.POSTED
    document.posted_by = actor
    document.posted_at = posted_at
    document._controlled_transition = True
    _enable_capability("controlled_supply_document_transition")
    document.full_clean()
    document.save(
        update_fields=("status", "posted_by", "posted_at", "updated_at")
    )
    _audit(
        actor=actor,
        action="supply_document_post",
        instance=document,
        old=old,
        new={
            **_document_snapshot(document),
            "ledger_count": len(ledgers),
            "total_amount": str(total_amount),
        },
        request=request,
    )
    return document
