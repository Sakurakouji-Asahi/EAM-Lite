"""Shared, permission-scoped query plans for Sprint 18 supply reports."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db.models import (
    BooleanField,
    Case,
    CharField,
    DateField,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.assets.permissions import can_view_financial_fields
from apps.masterdata.normalization import normalize_identifier
from apps.masterdata.permissions import role_names_for
from apps.reports.queries import MANAGED_STATUSES, ReportDataset, ReportValidationError
from apps.reports.schemas import get_report_definition, visible_report_definition
from apps.supplies.domain import ZERO_MONEY, ZERO_QTY, quantize_money, quantize_quantity
from apps.supplies.models import (
    EmployeeSupplyClearanceItem,
    SupplyCountLine,
    SupplyCountStatus,
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyCustodyStatus,
    SupplyDocument,
    SupplyDocumentLine,
    SupplyItem,
    SupplyItemType,
    SupplyStockBalance,
    SupplyStockLedger,
    SupplyStockMovementType,
)
from apps.supplies.permissions import (
    can_view_supply_cost,
    scoped_employee_supply_clearance_items,
    scoped_supply_count_lines,
    scoped_supply_count_tasks,
    scoped_supply_custodies,
    scoped_supply_custody_movements,
    scoped_supply_documents,
    scoped_supply_stock_balances,
    scoped_supply_stock_ledgers,
)


QTY_FIELD = DecimalField(max_digits=18, decimal_places=4)
MONEY_FIELD = DecimalField(max_digits=18, decimal_places=2)
ACTIVE_COUNT_STATUSES = (
    SupplyCountStatus.IN_PROGRESS,
    SupplyCountStatus.RECONCILIATION,
)


class ReportRowSource:
    """Countable, sliceable row stream used by both pagination and XLSX."""

    def __init__(self, count: int, factory: Callable[[int | None, int | None], Iterator[dict]]):
        self._count = int(count)
        self._factory = factory

    def count(self):
        return self._count

    def __len__(self):
        return self._count

    def __iter__(self):
        return self._factory(None, None)

    def __getitem__(self, value):
        if isinstance(value, slice):
            start = value.start or 0
            stop = value.stop
            if value.step not in (None, 1):
                return list(self._factory(start, stop))[:: value.step]
            return list(self._factory(start, stop))
        if value < 0:
            value += self._count
        rows = list(self._factory(value, value + 1))
        if not rows:
            raise IndexError(value)
        return rows[0]


def _query_source(queryset, mapper, *, chunk_size=1000):
    count = queryset.count()

    def factory(start, stop):
        rows = queryset
        if start is not None or stop is not None:
            rows = rows[start or 0 : stop]
        for row in rows.iterator(chunk_size=chunk_size):
            yield mapper(row)

    return ReportRowSource(count, factory)


def _batched_source(queryset, mapper, *, chunk_size=500):
    count = queryset.count()

    def factory(start, stop):
        rows = queryset
        if start is not None or stop is not None:
            rows = rows[start or 0 : stop]
        batch = []
        for row in rows.iterator(chunk_size=chunk_size):
            batch.append(row)
            if len(batch) >= chunk_size:
                yield from mapper(batch)
                batch = []
        if batch:
            yield from mapper(batch)

    return ReportRowSource(count, factory)


def _validated_filters(*, actor, company, report_key, filters):
    from apps.reports.supply_forms import SupplyReportFilterForm

    raw = dict(filters or {})
    internal = {key: value for key, value in raw.items() if str(key).startswith("_")}
    form = SupplyReportFilterForm(
        raw,
        actor=actor,
        company=company,
        report_key=report_key,
    )
    if not form.is_valid():
        errors = []
        for field_errors in form.errors.values():
            errors.extend(str(error) for error in field_errors)
        raise ReportValidationError(errors or ("报表筛选条件无效。",))
    return {**form.as_filters(), **internal}


def _definition(actor, report_key):
    return visible_report_definition(
        report_key,
        include_supply_cost=can_view_supply_cost(actor),
        include_asset_finance=can_view_financial_fields(actor),
    )


def _dataset(actor, report_key, filters, rows, *, warnings=()):
    return ReportDataset(
        definition=_definition(actor, report_key),
        rows=rows,
        filters=filters,
        data_snapshot_at=timezone.now(),
        warnings=tuple(warnings),
    )


def _item_filters(queryset, filters, *, prefix="item__"):
    if filters.get("category"):
        queryset = queryset.filter(**{f"{prefix}category_id": filters["category"]})
    if filters.get("item_code"):
        queryset = queryset.filter(
            **{
                f"{prefix}normalized_item_code": normalize_identifier(
                    filters["item_code"]
                )
            }
        )
    if filters.get("management_mode"):
        queryset = queryset.filter(
            **{f"{prefix}item_type": filters["management_mode"]}
        )
    return queryset


def _stock_balance_rows(*, actor, company, filters):
    include_cost = can_view_supply_cost(actor)
    last_date = (
        SupplyStockLedger.objects.filter(
            company=company,
            warehouse_id=OuterRef("warehouse_id"),
            item_id=OuterRef("item_id"),
        )
        .order_by("-document__business_date", "-occurred_at", "-pk")
        .values("document__business_date")[:1]
    )
    queryset = scoped_supply_stock_balances(
        actor,
        company,
        SupplyStockBalance.objects.select_related(
            "warehouse", "item", "item__category", "item__default_warehouse"
        ).annotate(last_ledger_date=Subquery(last_date, output_field=DateField())),
    )
    queryset = _item_filters(queryset, filters)
    if filters.get("warehouse"):
        queryset = queryset.filter(warehouse_id=filters["warehouse"])
    if not filters.get("include_zero"):
        queryset = queryset.exclude(quantity_on_hand=ZERO_QTY)
    low_q = Q(
        item__is_active=True,
        item__minimum_stock_quantity__gt=ZERO_QTY,
        item__default_warehouse_id=F("warehouse_id"),
        quantity_on_hand__lt=F("item__minimum_stock_quantity"),
    )
    if filters.get("low_stock") == "yes":
        queryset = queryset.filter(low_q)
    elif filters.get("low_stock") == "no":
        queryset = queryset.exclude(low_q)
    if not include_cost:
        queryset = queryset.defer("amount_on_hand", "average_unit_cost")
    queryset = queryset.order_by(
        "warehouse__normalized_code", "item__normalized_item_code", "pk"
    )

    def mapper(balance):
        low = bool(
            balance.item.is_active
            and balance.item.minimum_stock_quantity > ZERO_QTY
            and balance.item.default_warehouse_id == balance.warehouse_id
            and balance.quantity_on_hand < balance.item.minimum_stock_quantity
        )
        row = {
            "warehouse_code": balance.warehouse.code,
            "warehouse_name": balance.warehouse.name,
            "item_code": balance.item.item_code,
            "item_name": balance.item.name,
            "category": balance.item.category.name,
            "management_mode": balance.item.get_item_type_display(),
            "unit": balance.item.unit,
            "current_quantity": balance.quantity_on_hand,
            "minimum_stock": balance.item.minimum_stock_quantity,
            "default_warehouse": (
                balance.item.default_warehouse.name
                if balance.item.default_warehouse_id
                else ""
            ),
            "is_low_stock": low,
            "shortage_quantity": quantize_quantity(
                balance.item.minimum_stock_quantity - balance.quantity_on_hand
            )
            if low
            else ZERO_QTY,
            "last_ledger_date": balance.last_ledger_date,
            "item_active": balance.item.is_active,
        }
        if include_cost:
            row.update(
                average_unit_cost=balance.average_unit_cost,
                current_amount=balance.amount_on_hand,
            )
        return row

    return _query_source(queryset, mapper)


def _effective_type_q(movement_type):
    return Q(movement_type=movement_type) | Q(
        movement_type=SupplyStockMovementType.REVERSAL,
        reverses_ledger__movement_type=movement_type,
    )


def _low_stock_rows(*, actor, company, filters):
    balance_quantity = SupplyStockBalance.objects.filter(
        company=company,
        warehouse_id=OuterRef("default_warehouse_id"),
        item_id=OuterRef("pk"),
    ).values("quantity_on_hand")[:1]
    receipt_date = (
        SupplyStockLedger.objects.filter(
            company=company,
            item_id=OuterRef("pk"),
            warehouse_id=OuterRef("default_warehouse_id"),
        )
        .filter(
            _effective_type_q(SupplyStockMovementType.OPENING_IN)
            | _effective_type_q(SupplyStockMovementType.RECEIPT_IN)
        )
        .order_by("-document__business_date", "-occurred_at")
        .values("document__business_date")[:1]
    )
    issue_date = (
        SupplyStockLedger.objects.filter(
            company=company,
            item_id=OuterRef("pk"),
            warehouse_id=OuterRef("default_warehouse_id"),
        )
        .filter(_effective_type_q(SupplyStockMovementType.ISSUE_OUT))
        .order_by("-document__business_date", "-occurred_at")
        .values("document__business_date")[:1]
    )
    queryset = SupplyItem.objects.filter(
        company=company,
        is_active=True,
        minimum_stock_quantity__gt=ZERO_QTY,
    ).select_related("category", "default_warehouse")
    if filters.get("category"):
        queryset = queryset.filter(category_id=filters["category"])
    if filters.get("item_code"):
        queryset = queryset.filter(
            normalized_item_code=normalize_identifier(filters["item_code"])
        )
    if filters.get("management_mode"):
        queryset = queryset.filter(item_type=filters["management_mode"])
    queryset = queryset.annotate(
        current_quantity=Coalesce(
            Subquery(balance_quantity, output_field=QTY_FIELD),
            Value(ZERO_QTY, output_field=QTY_FIELD),
        ),
        last_receipt_date=Subquery(receipt_date, output_field=DateField()),
        last_issue_date=Subquery(issue_date, output_field=DateField()),
    )
    scope = filters.get("low_stock_scope") or "formal"
    if scope == "formal":
        queryset = queryset.filter(
            default_warehouse__isnull=False,
            current_quantity__lt=F("minimum_stock_quantity"),
        )
    else:
        queryset = queryset.filter(default_warehouse__isnull=True)
    queryset = queryset.order_by("normalized_item_code", "pk")

    def mapper(item):
        configured = item.default_warehouse_id is not None
        shortage = (
            quantize_quantity(item.minimum_stock_quantity - item.current_quantity)
            if configured and item.current_quantity < item.minimum_stock_quantity
            else ZERO_QTY
        )
        return {
            "item_code": item.item_code,
            "item_name": item.name,
            "category": item.category.name,
            "unit": item.unit,
            "default_warehouse": item.default_warehouse.name if configured else "",
            "current_quantity": item.current_quantity if configured else ZERO_QTY,
            "minimum_stock": item.minimum_stock_quantity,
            "shortage_quantity": shortage,
            "last_receipt_date": item.last_receipt_date,
            "last_issue_date": item.last_issue_date,
            "item_active": item.is_active,
            "configuration_status": "正式预警" if configured else "未配置默认仓库",
        }

    return _query_source(queryset, mapper)


def _sum(expression, condition, output_field):
    return Coalesce(
        Sum(expression, filter=condition),
        Value(Decimal("0"), output_field=output_field),
        output_field=output_field,
    )


def _stock_movement_rows(*, actor, company, filters):
    start = filters["date_from"]
    end = filters["date_to"]
    include_cost = can_view_supply_cost(actor)
    queryset = scoped_supply_stock_ledgers(actor, company).filter(
        document__business_date__lte=end
    )
    queryset = _item_filters(queryset, filters)
    if filters.get("warehouse"):
        queryset = queryset.filter(warehouse_id=filters["warehouse"])
    period = Q(document__business_date__gte=start, document__business_date__lte=end)
    before = Q(document__business_date__lt=start)
    through_end = Q(document__business_date__lte=end)
    values = (
        "warehouse_id",
        "warehouse__code",
        "warehouse__name",
        "item_id",
        "item__item_code",
        "item__name",
        "item__unit",
    )
    annotations = {
        "opening_quantity": _sum(F("quantity_delta"), before, QTY_FIELD),
        "ending_quantity": _sum(F("quantity_delta"), through_end, QTY_FIELD),
    }
    bucket_types = {
        "receipt": (
            SupplyStockMovementType.OPENING_IN,
            SupplyStockMovementType.RECEIPT_IN,
        ),
        "return": (SupplyStockMovementType.RETURN_IN,),
        "transfer_in": (SupplyStockMovementType.TRANSFER_IN,),
        "count_gain": (SupplyStockMovementType.COUNT_GAIN,),
        "issue": (SupplyStockMovementType.ISSUE_OUT,),
        "transfer_out": (SupplyStockMovementType.TRANSFER_OUT,),
        "count_loss": (SupplyStockMovementType.COUNT_LOSS,),
    }
    for key, types in bucket_types.items():
        type_condition = Q(pk__in=[])
        for movement_type in types:
            type_condition |= _effective_type_q(movement_type)
        expression = (
            ExpressionWrapper(-F("quantity_delta"), output_field=QTY_FIELD)
            if key in {"issue", "transfer_out", "count_loss"}
            else F("quantity_delta")
        )
        annotations[f"{key}_quantity"] = _sum(
            expression, period & type_condition, QTY_FIELD
        )
    if include_cost:
        annotations.update(
            opening_amount=_sum(F("amount_delta"), before, MONEY_FIELD),
            ending_amount=_sum(F("amount_delta"), through_end, MONEY_FIELD),
        )
        for key, types in bucket_types.items():
            type_condition = Q(pk__in=[])
            for movement_type in types:
                type_condition |= _effective_type_q(movement_type)
            expression = (
                ExpressionWrapper(-F("amount_delta"), output_field=MONEY_FIELD)
                if key in {"issue", "transfer_out", "count_loss"}
                else F("amount_delta")
            )
            annotations[f"{key}_amount"] = _sum(
                expression, period & type_condition, MONEY_FIELD
            )
    queryset = (
        queryset.values(*values)
        .annotate(**annotations)
        .order_by("warehouse__code", "item__item_code", "item_id")
    )

    def mapper(row):
        result = {
            "warehouse": f"{row['warehouse__code']} / {row['warehouse__name']}",
            "item_code": row["item__item_code"],
            "item_name": row["item__name"],
            "unit": row["item__unit"],
        }
        for key in (
            "opening", "receipt", "return", "transfer_in", "count_gain",
            "issue", "transfer_out", "count_loss", "ending",
        ):
            result[f"{key}_quantity"] = quantize_quantity(
                row[f"{key}_quantity"]
            )
            if include_cost:
                result[f"{key}_amount"] = quantize_money(row[f"{key}_amount"])
        return result

    return _query_source(queryset, mapper)


def _stock_ledger_rows(*, actor, company, filters):
    include_cost = can_view_supply_cost(actor)
    queryset = scoped_supply_stock_ledgers(
        actor,
        company,
        SupplyStockLedger.objects.select_related(
            "warehouse",
            "item",
            "item__category",
            "document",
            "document__source_count_task",
            "document_line",
            "created_by",
            "reverses_ledger",
            "reverses_ledger__document",
            "reversal_ledger",
        ),
    )
    queryset = _item_filters(queryset, filters)
    if filters.get("warehouse"):
        queryset = queryset.filter(warehouse_id=filters["warehouse"])
    if filters.get("date_from"):
        queryset = queryset.filter(
            document__business_date__gte=filters["date_from"],
            document__business_date__lte=filters["date_to"],
        )
    if filters.get("document_type"):
        queryset = queryset.filter(document__document_type=filters["document_type"])
    if filters.get("document_status"):
        queryset = queryset.filter(document__status=filters["document_status"])
    if filters.get("movement_type"):
        queryset = queryset.filter(movement_type=filters["movement_type"])
    if not include_cost:
        queryset = queryset.defer(
            "amount_delta",
            "unit_cost",
            "amount_before",
            "amount_after",
            "average_unit_cost_before",
            "average_unit_cost_after",
        )
    queryset = queryset.order_by("-occurred_at", "-pk")

    def mapper(ledger):
        original = ledger.reverses_ledger if ledger.reverses_ledger_id else ledger
        row = {
            "business_date": ledger.document.business_date,
            "created_at": ledger.occurred_at,
            "document_no": ledger.document.document_no,
            "original_document_type": original.document.get_document_type_display(),
            "movement_type": ledger.get_movement_type_display(),
            "warehouse": f"{ledger.warehouse.code} / {ledger.warehouse.name}",
            "item": f"{ledger.item.item_code} / {ledger.item.name}",
            "unit": ledger.item.unit,
            "quantity_delta": ledger.quantity_delta,
            "quantity_before": ledger.quantity_before,
            "quantity_after": ledger.quantity_after,
            "original_ledger": str(ledger.reverses_ledger_id or ""),
            "reversed_by_ledger": str(
                getattr(getattr(ledger, "reversal_ledger", None), "pk", "")
            ),
            "operator": getattr(ledger.created_by, "username", ""),
            "count_task": (
                ledger.document.source_count_task.task_no
                if ledger.document.source_count_task_id
                else ""
            ),
        }
        if include_cost:
            row.update(
                amount_delta=ledger.amount_delta,
                amount_before=ledger.amount_before,
                amount_after=ledger.amount_after,
                posting_unit_cost=ledger.unit_cost,
            )
        return row

    return _query_source(queryset, mapper)


def _issue_scope_filter(actor, company):
    roles = role_names_for(actor)
    if roles.intersection(
        {"system_admin", "finance", "warehouse", "equipment", "management"}
    ):
        return Q()
    if "department_manager" in roles:
        from apps.masterdata.permissions import resolve_department_ids

        department_ids = resolve_department_ids(actor, company)
        return Q(document__department_id__in=department_ids) | Q(
            movement_type=SupplyStockMovementType.REVERSAL,
            reverses_ledger__document__department_id__in=department_ids,
        )
    if "employee" in roles:
        return Q(document__employee__user=actor) | Q(
            movement_type=SupplyStockMovementType.REVERSAL,
            reverses_ledger__document__employee__user=actor,
        )
    return Q(pk__in=[])


def _issue_ledger_queryset(*, actor, company, filters):
    effective = (
        _effective_type_q(SupplyStockMovementType.ISSUE_OUT)
        | _effective_type_q(SupplyStockMovementType.RETURN_IN)
    )
    queryset = SupplyStockLedger.objects.filter(company=company).filter(effective)
    queryset = queryset.filter(_issue_scope_filter(actor, company))
    queryset = _item_filters(queryset, filters)
    if filters.get("date_from"):
        queryset = queryset.filter(
            document__business_date__gte=filters["date_from"],
            document__business_date__lte=filters["date_to"],
        )
    if filters.get("department"):
        department_id = filters["department"]
        queryset = queryset.filter(
            Q(document__department_id=department_id)
            | Q(
                movement_type=SupplyStockMovementType.REVERSAL,
                reverses_ledger__document__department_id=department_id,
            )
        )
    if filters.get("employee"):
        employee_id = filters["employee"]
        queryset = queryset.filter(
            Q(document__employee_id=employee_id)
            | Q(
                movement_type=SupplyStockMovementType.REVERSAL,
                reverses_ledger__document__employee_id=employee_id,
            )
        )
    return queryset


def _root_issue_line_id(ledger):
    original = ledger.reverses_ledger if ledger.reverses_ledger_id else ledger
    if original.movement_type == SupplyStockMovementType.ISSUE_OUT:
        return original.document_line_id
    return original.document_line.source_issue_line_id


def _net_issue_by_root(*, company, root_ids, include_cost):
    totals = defaultdict(lambda: [ZERO_QTY, ZERO_MONEY])
    if not root_ids:
        return totals
    queryset = SupplyStockLedger.objects.filter(company=company).filter(
        Q(
            movement_type=SupplyStockMovementType.ISSUE_OUT,
            document_line_id__in=root_ids,
        )
        | Q(
            movement_type=SupplyStockMovementType.RETURN_IN,
            document_line__source_issue_line_id__in=root_ids,
        )
        | Q(
            movement_type=SupplyStockMovementType.REVERSAL,
            reverses_ledger__movement_type=SupplyStockMovementType.ISSUE_OUT,
            reverses_ledger__document_line_id__in=root_ids,
        )
        | Q(
            movement_type=SupplyStockMovementType.REVERSAL,
            reverses_ledger__movement_type=SupplyStockMovementType.RETURN_IN,
            reverses_ledger__document_line__source_issue_line_id__in=root_ids,
        )
    )
    value_fields = [
        "movement_type",
        "quantity_delta",
        "document_line_id",
        "document_line__source_issue_line_id",
        "reverses_ledger__movement_type",
        "reverses_ledger__document_line_id",
        "reverses_ledger__document_line__source_issue_line_id",
    ]
    if include_cost:
        value_fields.append("amount_delta")
    for ledger in queryset.values(*value_fields).iterator(chunk_size=1000):
        if ledger["movement_type"] == SupplyStockMovementType.REVERSAL:
            effective_type = ledger["reverses_ledger__movement_type"]
            root_id = (
                ledger["reverses_ledger__document_line_id"]
                if effective_type == SupplyStockMovementType.ISSUE_OUT
                else ledger[
                    "reverses_ledger__document_line__source_issue_line_id"
                ]
            )
        else:
            root_id = (
                ledger["document_line_id"]
                if ledger["movement_type"] == SupplyStockMovementType.ISSUE_OUT
                else ledger["document_line__source_issue_line_id"]
            )
        totals[root_id][0] = quantize_quantity(
            totals[root_id][0] - ledger["quantity_delta"]
        )
        if include_cost:
            totals[root_id][1] = quantize_money(
                totals[root_id][1] - ledger["amount_delta"]
            )
    return totals


def _issue_detail_rows(*, actor, company, filters):
    include_cost = can_view_supply_cost(actor)
    queryset = _issue_ledger_queryset(
        actor=actor, company=company, filters=filters
    ).select_related(
        "document",
        "document__department",
        "document__employee",
        "document_line",
        "document_line__source_issue_line",
        "document_line__source_issue_line__document",
        "item",
        "reverses_ledger",
        "reverses_ledger__document",
        "reverses_ledger__document__department",
        "reverses_ledger__document__employee",
        "reverses_ledger__document_line",
        "reverses_ledger__document_line__source_issue_line",
        "reverses_ledger__document_line__source_issue_line__document",
    ).order_by("-document__business_date", "-occurred_at", "pk")
    if not include_cost:
        queryset = queryset.defer(
            "amount_delta",
            "unit_cost",
            "amount_before",
            "amount_after",
            "average_unit_cost_before",
            "average_unit_cost_after",
            "reverses_ledger__amount_delta",
            "reverses_ledger__unit_cost",
            "reverses_ledger__amount_before",
            "reverses_ledger__amount_after",
            "reverses_ledger__average_unit_cost_before",
            "reverses_ledger__average_unit_cost_after",
            "document_line__entered_unit_cost",
            "document_line__posted_unit_cost",
            "document_line__posted_amount",
            "document_line__source_issue_line__entered_unit_cost",
            "document_line__source_issue_line__posted_unit_cost",
            "document_line__source_issue_line__posted_amount",
            "reverses_ledger__document_line__entered_unit_cost",
            "reverses_ledger__document_line__posted_unit_cost",
            "reverses_ledger__document_line__posted_amount",
            "reverses_ledger__document_line__source_issue_line__entered_unit_cost",
            "reverses_ledger__document_line__source_issue_line__posted_unit_cost",
            "reverses_ledger__document_line__source_issue_line__posted_amount",
        )

    def map_batch(ledgers):
        roots = {_root_issue_line_id(ledger) for ledger in ledgers}
        totals = _net_issue_by_root(
            company=company, root_ids=roots, include_cost=include_cost
        )
        for ledger in ledgers:
            original = ledger.reverses_ledger if ledger.reverses_ledger_id else ledger
            root_line = (
                original.document_line
                if original.movement_type == SupplyStockMovementType.ISSUE_OUT
                else original.document_line.source_issue_line
            )
            business_document = original.document
            sign = Decimal("-1") if ledger.movement_type == SupplyStockMovementType.REVERSAL else Decimal("1")
            root_total = totals[_root_issue_line_id(ledger)]
            row = {
                "business_date": ledger.document.business_date,
                "document_no": ledger.document.document_no,
                "business_type": (
                    "冲销"
                    if ledger.movement_type == SupplyStockMovementType.REVERSAL
                    else "领用"
                    if original.movement_type == SupplyStockMovementType.ISSUE_OUT
                    else "退回"
                ),
                "department": getattr(business_document.department, "name", ""),
                "employee": getattr(business_document.employee, "name", ""),
                "item": f"{ledger.item.item_code} / {ledger.item.name}",
                "management_mode": ledger.item.get_item_type_display(),
                "unit": ledger.item.unit,
                "quantity": quantize_quantity(abs(ledger.quantity_delta) * sign),
                "original_issue_document": root_line.document.document_no,
                "current_net_quantity": root_total[0],
            }
            if include_cost:
                row.update(
                    unit_cost=ledger.unit_cost,
                    amount=quantize_money(abs(ledger.amount_delta) * sign),
                    current_net_amount=root_total[1],
                )
            yield row

    return _batched_source(queryset, map_batch)


def _issue_summary_rows(*, actor, company, filters, employee):
    include_cost = can_view_supply_cost(actor)
    queryset = _issue_ledger_queryset(actor=actor, company=company, filters=filters)
    original_department_id = Case(
        When(
            movement_type=SupplyStockMovementType.REVERSAL,
            then=F("reverses_ledger__document__department_id"),
        ),
        default=F("document__department_id"),
    )
    original_department_name = Case(
        When(
            movement_type=SupplyStockMovementType.REVERSAL,
            then=F("reverses_ledger__document__department__name"),
        ),
        default=F("document__department__name"),
        output_field=CharField(),
    )
    original_employee_id = Case(
        When(
            movement_type=SupplyStockMovementType.REVERSAL,
            then=F("reverses_ledger__document__employee_id"),
        ),
        default=F("document__employee_id"),
    )
    original_employee_name = Case(
        When(
            movement_type=SupplyStockMovementType.REVERSAL,
            then=F("reverses_ledger__document__employee__name"),
        ),
        default=F("document__employee__name"),
        output_field=CharField(),
    )
    queryset = queryset.annotate(
        report_department_id=original_department_id,
        report_department=original_department_name,
        report_employee_id=original_employee_id,
        report_employee=original_employee_name,
    )
    group_fields = [
        "report_department_id",
        "report_department",
        "item_id",
        "item__item_code",
        "item__name",
        "item__unit",
        "item__item_type",
    ]
    if employee:
        group_fields[2:2] = ["report_employee_id", "report_employee"]
    issue_q = _effective_type_q(SupplyStockMovementType.ISSUE_OUT)
    return_q = _effective_type_q(SupplyStockMovementType.RETURN_IN)
    annotations = {
        "issue_quantity": _sum(-F("quantity_delta"), issue_q, QTY_FIELD),
        "return_quantity": _sum(F("quantity_delta"), return_q, QTY_FIELD),
    }
    if include_cost:
        annotations.update(
            issue_amount=_sum(-F("amount_delta"), issue_q, MONEY_FIELD),
            return_amount=_sum(F("amount_delta"), return_q, MONEY_FIELD),
        )
    queryset = queryset.values(*group_fields).annotate(**annotations).order_by(
        "report_department", "report_employee" if employee else "item__item_code", "item__item_code"
    )

    def mapper(row):
        result = {
            "department": row["report_department"] or "",
            "item_code": row["item__item_code"],
            "item_name": row["item__name"],
            "unit": row["item__unit"],
            "management_mode": dict(SupplyItemType.choices).get(
                row["item__item_type"], row["item__item_type"]
            ),
            "issue_quantity": quantize_quantity(row["issue_quantity"]),
            "return_quantity": quantize_quantity(row["return_quantity"]),
            "net_quantity": quantize_quantity(
                row["issue_quantity"] - row["return_quantity"]
            ),
        }
        if employee:
            result["employee"] = row["report_employee"] or ""
        if include_cost:
            result.update(
                issue_amount=quantize_money(row["issue_amount"]),
                return_amount=quantize_money(row["return_amount"]),
                net_amount=quantize_money(
                    row["issue_amount"] - row["return_amount"]
                ),
            )
        return result

    return _query_source(queryset, mapper)


def _custody_roots(batch):
    """Resolve root sources in bounded batches without one query per parent."""

    known = {custody.pk: custody for custody in batch}
    pending = {
        custody.parent_custody_id
        for custody in batch
        if custody.parent_custody_id and custody.parent_custody_id not in known
    }
    while pending:
        parents = SupplyCustody.objects.filter(pk__in=pending).select_related(
            "origin_issue_line__document", "origin_import_row__batch", "parent_custody"
        )
        new_pending = set()
        found = 0
        for parent in parents:
            known[parent.pk] = parent
            found += 1
            if parent.parent_custody_id and parent.parent_custody_id not in known:
                new_pending.add(parent.parent_custody_id)
        if not found:
            break
        pending = new_pending
    roots = {}
    for custody in batch:
        node = custody
        seen = set()
        while node.parent_custody_id and node.pk not in seen:
            seen.add(node.pk)
            node = known.get(node.parent_custody_id)
            if node is None:
                break
        roots[custody.pk] = node or custody
    return roots


def _custody_balance_rows(*, actor, company, filters):
    include_cost = can_view_supply_cost(actor)
    outgoing_date = (
        SupplyCustodyMovement.objects.filter(
            company=company, from_custody_id=OuterRef("pk")
        )
        .order_by("-business_date", "-created_at")
        .values("business_date")[:1]
    )
    incoming_date = (
        SupplyCustodyMovement.objects.filter(
            company=company, to_custody_id=OuterRef("pk")
        )
        .order_by("-business_date", "-created_at")
        .values("business_date")[:1]
    )
    active_count = SupplyCountLine.objects.filter(
        company=company,
        custody_id=OuterRef("pk"),
        count_task__status__in=ACTIVE_COUNT_STATUSES,
    )
    pending_clearance = EmployeeSupplyClearanceItem.objects.filter(
        company=company,
        custody_id=OuterRef("pk"),
        resolution="pending",
    )
    queryset = scoped_supply_custodies(
        actor,
        company,
        SupplyCustody.objects.select_related(
            "item",
            "item__category",
            "department",
            "employee",
            "origin_issue_line__document",
            "origin_import_row__batch",
            "parent_custody",
        ).annotate(
            last_outgoing_date=Subquery(outgoing_date, output_field=DateField()),
            last_incoming_date=Subquery(incoming_date, output_field=DateField()),
            in_active_count=Exists(active_count),
            pending_clearance=Exists(pending_clearance),
        ),
    )
    queryset = _item_filters(queryset, filters)
    if filters.get("department"):
        queryset = queryset.filter(department_id=filters["department"])
    if filters.get("employee"):
        queryset = queryset.filter(employee_id=filters["employee"])
    if filters.get("clearance_pending"):
        queryset = queryset.filter(pending_clearance=True)
    status = filters.get("custody_status") or SupplyCustodyStatus.OPEN
    queryset = queryset.filter(status=status)
    if not include_cost:
        queryset = queryset.defer("current_amount", "unit_cost_snapshot")
    queryset = queryset.order_by(
        "item__normalized_item_code", "department__normalized_code", "started_on", "pk"
    )

    def map_batch(custodies):
        roots = _custody_roots(custodies)
        for custody in custodies:
            root = roots[custody.pk]
            if root.origin_issue_line_id:
                source_type = "领用"
                source_reference = root.origin_issue_line.document.document_no
            elif root.origin_import_row_id:
                source_type = "期初导入"
                source_reference = str(root.origin_import_row.batch_id)
            else:
                source_type = "来源异常"
                source_reference = ""
            dates = [
                value
                for value in (custody.last_outgoing_date, custody.last_incoming_date)
                if value is not None
            ]
            row = {
                "custody_id": str(custody.pk),
                "item": f"{custody.item.item_code} / {custody.item.name}",
                "unit": custody.item.unit,
                "department": custody.department.name,
                "employee": getattr(custody.employee, "name", ""),
                "current_quantity": custody.current_quantity,
                "status": custody.get_status_display(),
                "started_on": custody.started_on,
                "root_source_type": source_type,
                "source_reference": source_reference,
                "parent_custody": str(custody.parent_custody_id or ""),
                "last_action_date": max(dates) if dates else custody.started_on,
                "in_active_count": custody.in_active_count,
                "pending_clearance": custody.pending_clearance,
            }
            if include_cost:
                row.update(
                    unit_cost=custody.unit_cost_snapshot,
                    current_amount=custody.current_amount,
                )
            yield row

    return _batched_source(queryset, map_batch)


def _custody_movement_rows(*, actor, company, filters):
    include_cost = can_view_supply_cost(actor)
    queryset = scoped_supply_custody_movements(
        actor,
        company,
        SupplyCustodyMovement.objects.select_related(
            "item",
            "item__category",
            "from_custody__department",
            "from_custody__employee",
            "to_custody__department",
            "to_custody__employee",
            "source_document_line__document",
            "created_by",
            "reverses_movement",
            "reversal_movement",
            "source_count_line__count_task",
        ).prefetch_related("clearance_items__clearance"),
    )
    queryset = _item_filters(queryset, filters)
    if filters.get("date_from"):
        queryset = queryset.filter(
            business_date__gte=filters["date_from"],
            business_date__lte=filters["date_to"],
        )
    if filters.get("custody_action"):
        queryset = queryset.filter(action=filters["custody_action"])
    if filters.get("department"):
        department_id = filters["department"]
        queryset = queryset.filter(
            Q(from_custody__department_id=department_id)
            | Q(to_custody__department_id=department_id)
        )
    if filters.get("employee"):
        employee_id = filters["employee"]
        queryset = queryset.filter(
            Q(from_custody__employee_id=employee_id)
            | Q(to_custody__employee_id=employee_id)
        )
    if not include_cost:
        queryset = queryset.defer("amount", "unit_cost")
    queryset = queryset.order_by("-business_date", "-created_at", "pk")

    def mapper(movement):
        count_line = getattr(movement, "source_count_line", None)
        clearance_item = next(iter(movement.clearance_items.all()), None)
        row = {
            "business_date": movement.business_date,
            "action": movement.get_action_display(),
            "item": f"{movement.item.item_code} / {movement.item.name}",
            "unit": movement.item.unit,
            "from_department": getattr(
                getattr(movement.from_custody, "department", None), "name", ""
            ),
            "from_employee": getattr(
                getattr(movement.from_custody, "employee", None), "name", ""
            ),
            "to_department": getattr(
                getattr(movement.to_custody, "department", None), "name", ""
            ),
            "to_employee": getattr(
                getattr(movement.to_custody, "employee", None), "name", ""
            ),
            "from_custody": str(movement.from_custody_id or ""),
            "to_custody": str(movement.to_custody_id or ""),
            "quantity": movement.quantity,
            "source_document": (
                movement.source_document_line.document.document_no
                if movement.source_document_line_id
                else ""
            ),
            "count_task": count_line.count_task.task_no if count_line else "",
            "count_line": str(getattr(count_line, "pk", "")),
            "clearance": str(
                getattr(getattr(clearance_item, "clearance", None), "pk", "")
            ),
            "clearance_item": str(getattr(clearance_item, "pk", "")),
            "original_movement": str(movement.reverses_movement_id or ""),
            "reversal_movement": str(
                getattr(getattr(movement, "reversal_movement", None), "pk", "")
            ),
            "reason": movement.reason,
            "operator": getattr(movement.created_by, "username", ""),
        }
        if include_cost:
            row.update(unit_cost=movement.unit_cost, amount=movement.amount)
        return row

    return _query_source(queryset, mapper, chunk_size=500)


def _count_difference_rows(*, actor, company, filters):
    include_cost = can_view_supply_cost(actor)
    stock_ledger = SupplyStockLedger.objects.filter(
        document_line_id=OuterRef("adjustment_document_line_id")
    ).order_by("pk").values("pk")[:1]
    queryset = scoped_supply_count_lines(
        actor,
        company,
        SupplyCountLine.objects.select_related(
            "count_task",
            "count_task__warehouse",
            "count_task__department",
            "count_task__employee",
            "item",
            "item__category",
            "custody",
            "adjustment_document_line__document",
            "resolution_custody_movement",
            "counted_by",
            "resolved_by",
        ).annotate(stock_ledger_id=Subquery(stock_ledger)),
    )
    queryset = _item_filters(queryset, filters)
    if filters.get("date_from"):
        queryset = queryset.filter(
            count_task__planned_start__gte=filters["date_from"],
            count_task__planned_end__lte=filters["date_to"],
        )
    if filters.get("warehouse"):
        queryset = queryset.filter(count_task__warehouse_id=filters["warehouse"])
    if filters.get("department"):
        queryset = queryset.filter(count_task__department_id=filters["department"])
    if filters.get("employee"):
        queryset = queryset.filter(count_task__employee_id=filters["employee"])
    if filters.get("count_domain"):
        queryset = queryset.filter(count_task__count_domain=filters["count_domain"])
    if filters.get("count_status"):
        queryset = queryset.filter(count_task__status=filters["count_status"])
    if filters.get("differences_only"):
        queryset = queryset.filter(difference_quantity__isnull=False).exclude(
            difference_quantity=ZERO_QTY
        )
    if not include_cost:
        queryset = queryset.defer(
            "expected_amount", "expected_unit_cost", "adjustment_unit_cost"
        )
    queryset = queryset.order_by("-count_task__created_at", "item_code_snapshot", "pk")

    def mapper(line):
        task = line.count_task
        row = {
            "task_no": task.task_no,
            "count_domain": task.get_count_domain_display(),
            "status": task.get_status_display(),
            "scope": (
                task.warehouse.name
                if task.warehouse_id
                else getattr(task.department, "name", "")
            ),
            "employee_scope": getattr(task.employee, "name", ""),
            "item": f"{line.item_code_snapshot} / {line.item_name_snapshot}",
            "custody": str(line.custody_id or ""),
            "expected_quantity": line.expected_quantity,
            "counted_quantity": line.counted_quantity,
            "difference_quantity": line.difference_quantity,
            "reason": line.remark,
            "resolution_type": line.get_resolution_type_display()
            if line.resolution_type
            else "",
            "adjustment_document": (
                line.adjustment_document_line.document.document_no
                if line.adjustment_document_line_id
                else ""
            ),
            "stock_ledger": str(line.stock_ledger_id or ""),
            "custody_movement": str(line.resolution_custody_movement_id or ""),
            "counted_by": getattr(line.counted_by, "username", ""),
            "resolved_by": getattr(line.resolved_by, "username", ""),
            "closed_at": task.closed_at,
        }
        if include_cost:
            row.update(
                expected_amount=line.expected_amount,
                adjustment_unit_cost=line.adjustment_unit_cost,
            )
        return row

    return _query_source(queryset, mapper)


def _location_paths(company):
    from apps.masterdata.models import Location

    nodes = {
        row["id"]: row
        for row in Location.objects.filter(company=company).values("id", "name", "parent_id")
    }
    result = {}
    for node_id in nodes:
        names = []
        current = node_id
        seen = set()
        while current and current not in seen and current in nodes:
            seen.add(current)
            names.append(nodes[current]["name"])
            current = nodes[current]["parent_id"]
        result[node_id] = " / ".join(reversed(names))
    return result


def _controlled_asset_queryset(*, actor, company, filters):
    from apps.assets.models import Asset, AssetQrIdentity
    from apps.inventory.models import InventoryTaskAsset
    from apps.offboarding.models import EmployeeAssetClearanceItem
    from apps.reports.permissions import scoped_report_assets

    qr_status = (
        AssetQrIdentity.objects.filter(
            company=company, asset_id=OuterRef("pk"), status="active"
        )
        .order_by("-version")
        .values("label_status")[:1]
    )
    inventory_status = (
        InventoryTaskAsset.objects.filter(company=company, asset_id=OuterRef("pk"))
        .order_by("-inventory_task__created_at", "-pk")
        .values("inventory_status")[:1]
    )
    clearance_status = (
        EmployeeAssetClearanceItem.objects.filter(
            company=company, asset_id=OuterRef("pk")
        )
        .order_by("-clearance__initiated_at", "-pk")
        .values("resolution")[:1]
    )
    queryset = scoped_report_assets(
        actor,
        company,
        Asset.objects.filter(
            company=company,
            record_status=Asset.RecordStatus.ACTIVE,
            asset_status__in=MANAGED_STATUSES,
            finance__accounting_treatment="controlled_non_fixed",
            finance__finance_confirmed_at__isnull=False,
        ).select_related(
            "category", "department", "responsible_employee", "location"
        ),
    ).annotate(
        report_qr_status=Subquery(qr_status, output_field=CharField()),
        report_inventory_status=Subquery(
            inventory_status, output_field=CharField()
        ),
        report_clearance_status=Subquery(
            clearance_status, output_field=CharField()
        ),
    )
    if filters.get("department"):
        queryset = queryset.filter(department_id=filters["department"])
    if filters.get("employee"):
        queryset = queryset.filter(responsible_employee_id=filters["employee"])
    return queryset.order_by("asset_code", "pk")


def _controlled_asset_rows(*, actor, company, filters):
    include_finance = can_view_financial_fields(actor)
    queryset = _controlled_asset_queryset(
        actor=actor, company=company, filters=filters
    )
    if include_finance:
        queryset = queryset.select_related("finance")
    locations = _location_paths(company)
    qr_labels = {
        "not_generated": "未生成",
        "ready_to_print": "待打印",
        "printed": "已打印",
        "attached": "已贴标",
    }
    inventory_labels = {
        "pending": "未盘",
        "normal": "正常",
        "exception": "异常",
        "missing": "盘亏候选",
        "resolved": "已处理",
    }
    clearance_labels = {
        "pending": "待处理",
        "disposal_in_progress": "处置中",
        "returned": "已归还",
        "transferred": "已转交",
        "disposed": "已处置",
    }

    def mapper(asset):
        row = {
            "asset_code": asset.asset_code,
            "asset_name": asset.asset_name,
            "category": asset.category.name,
            "department": getattr(asset.department, "name", ""),
            "responsible_employee": getattr(
                asset.responsible_employee, "name", ""
            ),
            "location": locations.get(asset.location_id, ""),
            "acquisition_date": asset.acquisition_date,
            "status": asset.get_asset_status_display(),
            "qr_status": qr_labels.get(asset.report_qr_status, "未生成"),
            "inventory_status": inventory_labels.get(
                asset.report_inventory_status, "未纳入盘点"
            ),
            "offboarding_status": clearance_labels.get(
                asset.report_clearance_status, "无清退项"
            ),
        }
        if include_finance:
            row["original_cost"] = asset.finance.original_cost
        return row

    return _query_source(queryset, mapper)


def _management_amount_rows(*, actor, company, filters):
    include_supply = can_view_supply_cost(actor)
    include_finance = can_view_financial_fields(actor)
    if not include_supply:
        raise PermissionDenied("您没有查看低值物品管理金额的权限。")
    stock = (
        SupplyStockBalance.objects.filter(company=company)
        .values("item__item_type", "item__unit")
        .annotate(
            quantity=Coalesce(Sum("quantity_on_hand"), Value(ZERO_QTY)),
            amount=Coalesce(Sum("amount_on_hand"), Value(ZERO_MONEY)),
        )
        .order_by("item__item_type", "item__unit")
    )
    custody = (
        SupplyCustody.objects.filter(
            company=company,
            status=SupplyCustodyStatus.OPEN,
            item__item_type=SupplyItemType.DURABLE_QUANTITY,
        )
        .values("item__unit")
        .annotate(
            quantity=Coalesce(Sum("current_quantity"), Value(ZERO_QTY)),
            amount=Coalesce(Sum("current_amount"), Value(ZERO_MONEY)),
        )
        .order_by("item__unit")
    )
    rows = []
    durable_by_unit = defaultdict(lambda: [ZERO_QTY, ZERO_MONEY])
    for value in stock:
        item_type = value["item__item_type"]
        label = (
            "低值易耗品仓库库存"
            if item_type == SupplyItemType.CONSUMABLE
            else "数量型耐用品仓库库存"
        )
        row = {
            "component": label,
            "quantity": quantize_quantity(value["quantity"]),
            "unit": value["item__unit"],
            "supply_amount": quantize_money(value["amount"]),
            "note": "来自当前库存余额缓存，并以库存流水核对。",
        }
        rows.append(row)
        if item_type == SupplyItemType.DURABLE_QUANTITY:
            durable_by_unit[value["item__unit"]][0] += row["quantity"]
            durable_by_unit[value["item__unit"]][1] += row["supply_amount"]
    for value in custody:
        quantity = quantize_quantity(value["quantity"])
        amount = quantize_money(value["amount"])
        unit = value["item__unit"]
        rows.append(
            {
                "component": "数量型耐用品开放保管",
                "quantity": quantity,
                "unit": unit,
                "supply_amount": amount,
                "note": "来自开放保管余额，并以保管动作流水核对。",
            }
        )
        durable_by_unit[unit][0] += quantity
        durable_by_unit[unit][1] += amount
    for unit, values in sorted(durable_by_unit.items()):
        rows.append(
            {
                "component": "数量型耐用品管理金额小计",
                "quantity": quantize_quantity(values[0]),
                "unit": unit,
                "supply_amount": quantize_money(values[1]),
                "note": "仓库库存金额 + 开放保管金额。",
            }
        )
    controlled = _controlled_asset_queryset(
        actor=actor, company=company, filters={}
    )
    controlled_summary = controlled.aggregate(quantity=Sum("quantity"))
    controlled_cost = None
    if include_finance:
        controlled_cost = controlled.aggregate(amount=Sum("finance__original_cost"))[
            "amount"
        ] or ZERO_MONEY
    controlled_row = {
        "component": "逐件受控非固定资产",
        "quantity": quantize_quantity(controlled_summary["quantity"] or ZERO_QTY),
        "unit": "件",
        "supply_amount": None,
        "note": "逐件资产原值单独列示，不与数量型管理金额混作同一会计余额。",
    }
    if include_finance:
        controlled_row["asset_original_cost"] = quantize_money(controlled_cost)
    rows.append(controlled_row)
    return ReportRowSource(
        len(rows),
        lambda start, stop: iter(rows[slice(start, stop)]),
    )


REPORT_BUILDERS = {
    "supply_stock_balance": _stock_balance_rows,
    "supply_low_stock": _low_stock_rows,
    "supply_stock_movement": _stock_movement_rows,
    "supply_stock_ledger": _stock_ledger_rows,
    "supply_issue_detail": _issue_detail_rows,
    "supply_department_issue": lambda **kwargs: _issue_summary_rows(
        **kwargs, employee=False
    ),
    "supply_employee_issue": lambda **kwargs: _issue_summary_rows(
        **kwargs, employee=True
    ),
    "supply_custody_balance": _custody_balance_rows,
    "supply_custody_movement": _custody_movement_rows,
    "supply_count_difference": _count_difference_rows,
    "controlled_non_fixed_assets": _controlled_asset_rows,
    "supply_management_amount": _management_amount_rows,
}


def build_supply_report_dataset(*, actor, company, report_key, filters=None):
    from apps.reports.permissions import require_view_report

    require_view_report(actor, report_key)
    definition = get_report_definition(report_key)
    if not definition.supply or report_key not in REPORT_BUILDERS:
        raise ReportValidationError(("未知低值物品报表。",))
    clean = _validated_filters(
        actor=actor,
        company=company,
        report_key=report_key,
        filters=filters,
    )
    rows = REPORT_BUILDERS[report_key](actor=actor, company=company, filters=clean)
    warnings = ()
    if report_key == "supply_management_amount":
        warnings = (
            "管理参考数据来自库存管理金额和逐件资产原值，不代表同一会计科目余额。",
        )
    return _dataset(actor, report_key, clean, rows, warnings=warnings)


def build_supply_dashboard(*, actor, company):
    from apps.supplies.permissions import (
        can_view_supply_custodies,
        can_view_supply_master_data,
        can_view_supply_module,
        can_view_supply_stock,
    )

    if not can_view_supply_module(actor):
        raise PermissionDenied("您没有查看低值物品 Dashboard 的权限。")
    include_cost = can_view_supply_cost(actor)
    result = {
        "data_snapshot_at": timezone.now(),
        "show_cost": include_cost,
        "reconciliation_status": "请按月末流程执行只读余额核对命令。",
    }
    if can_view_supply_master_data(actor):
        result["enabled_item_count"] = SupplyItem.objects.filter(
            company=company, is_active=True
        ).count()
    if can_view_supply_stock(actor):
        stock_scope = scoped_supply_stock_balances(actor, company).filter(
            quantity_on_hand__gt=ZERO_QTY
        )
        result["stock_combination_count"] = stock_scope.count()
        result["stock_quantities"] = tuple(
            {
                "unit": row["item__unit"],
                "quantity": quantize_quantity(row["quantity"]),
            }
            for row in stock_scope.values("item__unit")
            .annotate(quantity=Sum("quantity_on_hand"))
            .order_by("item__unit")
        )
        result["low_stock_count"] = len(
            _low_stock_rows(
                actor=actor,
                company=company,
                filters={"low_stock_scope": "formal"},
            )
        )
        if include_cost:
            result["stock_amount"] = quantize_money(
                stock_scope.aggregate(total=Sum("amount_on_hand"))["total"]
                or ZERO_MONEY
            )
    if can_view_supply_custodies(actor):
        custody_scope = scoped_supply_custodies(actor, company).filter(
            status=SupplyCustodyStatus.OPEN,
            item__item_type=SupplyItemType.DURABLE_QUANTITY,
        )
        result["custody_count"] = custody_scope.count()
        result["custody_quantities"] = tuple(
            {
                "unit": row["item__unit"],
                "quantity": quantize_quantity(row["quantity"]),
            }
            for row in custody_scope.values("item__unit")
            .annotate(quantity=Sum("current_quantity"))
            .order_by("item__unit")
        )
        if include_cost:
            result["custody_amount"] = quantize_money(
                custody_scope.aggregate(total=Sum("current_amount"))["total"]
                or ZERO_MONEY
            )
    result["controlled_non_fixed_count"] = _controlled_asset_queryset(
        actor=actor, company=company, filters={}
    ).count()
    result["draft_document_count"] = scoped_supply_documents(
        actor, company
    ).filter(status="draft").count()
    result["open_count_task_count"] = scoped_supply_count_tasks(
        actor, company
    ).exclude(status__in=("closed", "cancelled")).count()
    result["pending_clearance_count"] = scoped_employee_supply_clearance_items(
        actor, company
    ).filter(resolution="pending").count()
    return result


__all__ = [
    "ReportRowSource",
    "build_supply_dashboard",
    "build_supply_report_dataset",
]
