"""Production-grade reconciliation and controlled cache rebuild services.

The immutable stock/custody movements are the authority. Reconciliation is
stricter than a final SUM comparison: a broken history must never look healthy
merely because later rows happen to offset it.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone

from apps.audit.services import write_business_audit_log
from apps.masterdata.models import Company, Employee
from apps.masterdata.permissions import current_company, role_names_for
from apps.supplies.domain import (
    ZERO_COST,
    ZERO_MONEY,
    ZERO_QTY,
    calculate_average_unit_cost,
    quantize_money,
    quantize_quantity,
)
from apps.supplies.models import (
    SupplyAdjustmentDirection,
    SupplyCountDomain,
    SupplyCountLine,
    SupplyCountStatus,
    SupplyCountTask,
    SupplyCustody,
    SupplyCustodyAction,
    SupplyCustodyMovement,
    SupplyCustodyStatus,
    SupplyDocument,
    SupplyDocumentLine,
    SupplyDocumentStatus,
    SupplyDocumentType,
    SupplyItem,
    SupplyItemType,
    SupplyStockBalance,
    SupplyStockLedger,
    SupplyStockMovementType,
    SupplyWarehouse,
)
from apps.supplies.services import (
    _lock_or_create_balance,
    _update_balance_values,
    _update_custody_values,
)


ACTIVE_COUNT_STATUSES = (
    SupplyCountStatus.IN_PROGRESS,
    SupplyCountStatus.RECONCILIATION,
)
SUPPLY_REBUILD_ROLES = frozenset({"system_admin", "finance"})
ITERATOR_CHUNK_SIZE = 2000
MAX_INTEGRITY_MESSAGES = 1000
FORMAL_DOCUMENT_STATUSES = (
    SupplyDocumentStatus.POSTED,
    SupplyDocumentStatus.REVERSED,
)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    kind: str
    checked_count: int
    differences: tuple[dict, ...]
    integrity_errors: tuple[str, ...]
    expected: dict

    @property
    def is_consistent(self):
        return not self.differences and not self.integrity_errors


class _IntegrityErrors:
    """Bound diagnostic memory without ever hiding that corruption exists."""

    __slots__ = ("_messages", "_seen", "_omitted")

    def __init__(self):
        self._messages = []
        self._seen = set()
        self._omitted = 0

    def add(self, message):
        message = str(message)
        if message in self._seen:
            return
        self._seen.add(message)
        if len(self._messages) < MAX_INTEGRITY_MESSAGES:
            self._messages.append(message)
        else:
            self._omitted += 1

    def result(self):
        messages = list(self._messages)
        if self._omitted:
            messages.append(
                f"另有 {self._omitted} 条完整性错误未展开；请先修复已列错误后重新核对。"
            )
        return tuple(messages)


@contextmanager
def _stable_read_snapshot():
    """Use MVCC for a coherent read without blocking normal stock writes.

    A standalone PostgreSQL reconciliation starts a read-only repeatable-read
    transaction before its first business query.  If the caller already owns
    a transaction (for example a rollback-only corruption test), Django cannot
    change that transaction's isolation level; the caller's transaction is
    then the explicit consistency boundary.
    """

    already_atomic = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == "postgresql" and not already_atomic:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
        yield


def _average_or_error(*, quantity, amount, errors, context):
    quantity = quantize_quantity(quantity)
    amount = quantize_money(amount)
    try:
        return calculate_average_unit_cost(quantity, amount)
    except ValidationError as exc:
        errors.add(f"{context}：{'；'.join(exc.messages)}")
        return None


def _stock_state(row, *, before):
    suffix = "before" if before else "after"
    return (
        row[f"quantity_{suffix}"],
        row[f"amount_{suffix}"],
        row[f"average_unit_cost_{suffix}"],
    )


def _validate_stock_ledger_row(row, errors):
    ledger_id = row["id"]
    context = f"库存流水 {ledger_id}"
    quantity_delta = quantize_quantity(row["quantity_delta"])
    amount_delta = quantize_money(row["amount_delta"])
    quantity_before = quantize_quantity(row["quantity_before"])
    quantity_after = quantize_quantity(row["quantity_after"])
    amount_before = quantize_money(row["amount_before"])
    amount_after = quantize_money(row["amount_after"])

    if quantity_delta == ZERO_QTY:
        errors.add(f"{context} 的数量变动为 0。")
    if quantity_before < ZERO_QTY or quantity_after < ZERO_QTY:
        errors.add(f"{context} 包含负数库存数量快照。")
    if amount_before < ZERO_MONEY or amount_after < ZERO_MONEY:
        errors.add(f"{context} 包含负数库存金额快照。")
    if row["unit_cost"] < ZERO_COST:
        errors.add(f"{context} 的过账单位成本为负数。")
    if quantity_after != quantize_quantity(quantity_before + quantity_delta):
        errors.add(f"{context} 的数量 before/delta/after 不勾稽。")
    if amount_after != quantize_money(amount_before + amount_delta):
        errors.add(f"{context} 的金额 before/delta/after 不勾稽。")

    before_average = _average_or_error(
        quantity=quantity_before,
        amount=amount_before,
        errors=errors,
        context=f"{context} 的变动前平均成本无效",
    )
    after_average = _average_or_error(
        quantity=quantity_after,
        amount=amount_after,
        errors=errors,
        context=f"{context} 的变动后平均成本无效",
    )
    if before_average is not None and row["average_unit_cost_before"] != before_average:
        errors.add(f"{context} 的变动前平均成本与数量、金额不勾稽。")
    if after_average is not None and row["average_unit_cost_after"] != after_average:
        errors.add(f"{context} 的变动后平均成本与数量、金额不勾稽。")

    if (
        row["warehouse__company_id"] != row["company_id"]
        or row["item__company_id"] != row["company_id"]
        or row["document__company_id"] != row["company_id"]
        or row["document_line__company_id"] != row["company_id"]
    ):
        errors.add(f"{context} 存在跨公司引用。")
    if (
        row["document_line__document_id"] != row["document_id"]
        or row["document_line__item_id"] != row["item_id"]
    ):
        errors.add(f"{context} 的单据、明细或物品引用不一致。")
    if row["document__status"] not in FORMAL_DOCUMENT_STATUSES:
        errors.add(f"{context} 引用了未正式过账的单据。")
    if row["occurred_at"] != row["document__posted_at"]:
        errors.add(f"{context} 的发生时间与单据过账时间不一致。")
    if row["document__document_type"] == SupplyDocumentType.REVERSAL:
        if (
            row["movement_type"] != SupplyStockMovementType.REVERSAL
            or row["reverses_ledger_id"] is None
        ):
            errors.add(f"{context} 未按冲销流水保存原流水引用。")
    elif row["reverses_ledger_id"] is not None:
        errors.add(f"{context} 不是冲销流水却引用了被冲销流水。")


def _advance_stock_bucket(*, key, occurred_at, rows, state, errors):
    # UUID primary keys do not preserve insertion order.  Rows sharing one
    # posting timestamp are therefore linked by their immutable snapshots.
    # Indexing by the before state keeps a large multi-line document O(n).
    remaining = {row["id"]: row for row in rows}
    by_before = defaultdict(list)
    for row in rows:
        by_before[_stock_state(row, before=True)].append(row["id"])
    current = state
    while remaining:
        matches = [value for value in by_before.get(current, ()) if value in remaining]
        if len(matches) != 1:
            sample = next(iter(remaining.values()))
            errors.add(
                "仓库 {} / 物品 {} 在 {} 的流水链断裂：流水 {} 的 before 快照"
                "不能唯一衔接上一行 after 快照。".format(
                    key[0], key[1], occurred_at, sample["id"]
                )
            )
            quantity = quantize_quantity(
                current[0]
                + sum(
                    (row["quantity_delta"] for row in remaining.values()),
                    ZERO_QTY,
                )
            )
            amount = quantize_money(
                current[1]
                + sum(
                    (row["amount_delta"] for row in remaining.values()),
                    ZERO_MONEY,
                )
            )
            average = _average_or_error(
                quantity=quantity,
                amount=amount,
                errors=errors,
                context=f"仓库 {key[0]} / 物品 {key[1]} 的断链后汇总无效",
            )
            if quantity < ZERO_QTY or amount < ZERO_MONEY:
                errors.add(
                    f"仓库 {key[0]} / 物品 {key[1]} 的流水在 {occurred_at} 后形成负数中间状态。"
            )
            return (quantity, amount, average if average is not None else ZERO_COST)
        row = remaining.pop(matches[0])
        current = _stock_state(row, before=False)
    return current


def _stock_expected_from_ledgers(*, company, errors):
    expected = {}
    rows = (
        SupplyStockLedger.objects.filter(company=company)
        .values(
            "id",
            "company_id",
            "warehouse_id",
            "warehouse__company_id",
            "item_id",
            "item__company_id",
            "document_id",
            "document__company_id",
            "document__document_type",
            "document__status",
            "document__posted_at",
            "document_line_id",
            "document_line__company_id",
            "document_line__document_id",
            "document_line__item_id",
            "movement_type",
            "quantity_delta",
            "amount_delta",
            "unit_cost",
            "quantity_before",
            "quantity_after",
            "amount_before",
            "amount_after",
            "average_unit_cost_before",
            "average_unit_cost_after",
            "occurred_at",
            "reverses_ledger_id",
        )
        .order_by("warehouse_id", "item_id", "occurred_at", "id")
    )
    current_key = None
    current_time = None
    bucket = []
    state = (ZERO_QTY, ZERO_MONEY, ZERO_COST)
    total_quantity = ZERO_QTY
    total_amount = ZERO_MONEY

    def flush_bucket():
        nonlocal state, bucket
        if bucket:
            state = _advance_stock_bucket(
                key=current_key,
                occurred_at=current_time,
                rows=bucket,
                state=state,
                errors=errors,
            )
            bucket = []

    def flush_group():
        if current_key is None:
            return
        flush_bucket()
        quantity = quantize_quantity(total_quantity)
        amount = quantize_money(total_amount)
        average = _average_or_error(
            quantity=quantity,
            amount=amount,
            errors=errors,
            context=f"仓库 {current_key[0]} / 物品 {current_key[1]} 的流水汇总无效",
        )
        if quantity < ZERO_QTY or amount < ZERO_MONEY:
            errors.add(
                f"仓库 {current_key[0]} / 物品 {current_key[1]} 的流水汇总为负数。"
            )
        expected[current_key] = {
            "quantity": quantity,
            "amount": amount,
            "average": average,
        }

    for row in rows.iterator(chunk_size=ITERATOR_CHUNK_SIZE):
        key = (row["warehouse_id"], row["item_id"])
        if key != current_key:
            flush_group()
            current_key = key
            current_time = row["occurred_at"]
            state = (ZERO_QTY, ZERO_MONEY, ZERO_COST)
            total_quantity = ZERO_QTY
            total_amount = ZERO_MONEY
        elif row["occurred_at"] != current_time:
            flush_bucket()
            current_time = row["occurred_at"]
        _validate_stock_ledger_row(row, errors)
        total_quantity = quantize_quantity(total_quantity + row["quantity_delta"])
        total_amount = quantize_money(total_amount + row["amount_delta"])
        bucket.append(row)
    flush_group()
    return expected


def _document_integrity(*, company, errors):
    line_exists = SupplyDocumentLine.objects.filter(document_id=OuterRef("pk"))
    ledger_exists = SupplyStockLedger.objects.filter(document_id=OuterRef("pk"))
    documents = (
        SupplyDocument.objects.filter(company=company)
        .select_related("reversal_of", "reversal_document")
        .annotate(
            reconcile_has_lines=Exists(line_exists),
            reconcile_has_ledgers=Exists(ledger_exists),
        )
        .order_by("pk")
    )
    for document in documents.iterator(chunk_size=ITERATOR_CHUNK_SIZE):
        context = f"库存单据 {document.document_no}"
        reversal_document = getattr(document, "reversal_document", None)
        if document.status in FORMAL_DOCUMENT_STATUSES:
            if not document.reconcile_has_lines:
                errors.add(f"{context} 已正式过账但没有明细。")
            if not document.reconcile_has_ledgers:
                errors.add(f"{context} 已正式过账但没有库存流水。")
        elif document.reconcile_has_ledgers:
            errors.add(f"{context} 尚未正式过账却存在库存流水。")

        if document.document_type == SupplyDocumentType.REVERSAL:
            original = document.reversal_of
            if document.status != SupplyDocumentStatus.POSTED:
                errors.add(f"{context} 是冲销单但状态不是已过账。")
            if original is None or original.document_type == SupplyDocumentType.REVERSAL:
                errors.add(f"{context} 的原单关系无效。")
            elif (
                original.company_id != document.company_id
                or original.status != SupplyDocumentStatus.REVERSED
                or original.reversed_at != document.posted_at
            ):
                errors.add(f"{context} 与原单的公司、冲销状态或时间链不完整。")
            if reversal_document is not None:
                errors.add(f"{context} 不能再次被冲销。")
        elif document.status == SupplyDocumentStatus.REVERSED:
            if reversal_document is None:
                errors.add(f"{context} 已标记冲销但没有唯一冲销单。")
            elif (
                reversal_document.status != SupplyDocumentStatus.POSTED
                or reversal_document.reversal_of_id != document.pk
                or reversal_document.posted_at != document.reversed_at
            ):
                errors.add(f"{context} 的冲销单状态链不完整。")
        elif reversal_document is not None:
            errors.add(f"{context} 尚未标记冲销却已经存在冲销单。")


def _normal_line_specs(line):
    document = line.document
    posted_amount = line.posted_amount
    if document.document_type == SupplyDocumentType.OPENING:
        return [
            (
                SupplyStockMovementType.OPENING_IN,
                document.target_warehouse_id,
                line.quantity,
                posted_amount,
            )
        ]
    if document.document_type == SupplyDocumentType.RECEIPT:
        return [
            (
                SupplyStockMovementType.RECEIPT_IN,
                document.target_warehouse_id,
                line.quantity,
                posted_amount,
            )
        ]
    if document.document_type == SupplyDocumentType.ISSUE:
        return [
            (
                SupplyStockMovementType.ISSUE_OUT,
                document.source_warehouse_id,
                -line.quantity,
                -posted_amount,
            )
        ]
    if document.document_type == SupplyDocumentType.RETURN:
        return [
            (
                SupplyStockMovementType.RETURN_IN,
                document.target_warehouse_id,
                line.quantity,
                posted_amount,
            )
        ]
    if document.document_type == SupplyDocumentType.TRANSFER:
        return [
            (
                SupplyStockMovementType.TRANSFER_OUT,
                document.source_warehouse_id,
                -line.quantity,
                -posted_amount,
            ),
            (
                SupplyStockMovementType.TRANSFER_IN,
                document.target_warehouse_id,
                line.quantity,
                posted_amount,
            ),
        ]
    if document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT:
        warehouse_id = document.source_count_task.warehouse_id
        if line.adjustment_direction == SupplyAdjustmentDirection.INCREASE:
            return [
                (
                    SupplyStockMovementType.COUNT_GAIN,
                    warehouse_id,
                    line.quantity,
                    posted_amount,
                )
            ]
        if line.adjustment_direction == SupplyAdjustmentDirection.DECREASE:
            return [
                (
                    SupplyStockMovementType.COUNT_LOSS,
                    warehouse_id,
                    -line.quantity,
                    -posted_amount,
                )
            ]
    return []


def _validate_reversal_line(line, ledgers, errors):
    context = f"冲销单 {line.document.document_no} 第 {line.line_no} 行"
    original_document = line.document.reversal_of
    if original_document is None:
        errors.add(f"{context} 缺少被冲销原单。")
        return
    expected_count = (
        2 if original_document.document_type == SupplyDocumentType.TRANSFER else 1
    )
    if len(ledgers) != expected_count:
        errors.add(
            f"{context} 应有 {expected_count} 条精确反向流水，实际 {len(ledgers)} 条。"
        )
    original_ids = set()
    for ledger in ledgers:
        original = ledger.reverses_ledger
        if ledger.movement_type != SupplyStockMovementType.REVERSAL or original is None:
            errors.add(f"{context} 的流水 {ledger.pk} 没有有效原流水引用。")
            continue
        original_ids.add(original.pk)
        original_line = original.document_line
        if (
            original.document_id != original_document.pk
            or original_line.line_no != line.line_no
            or original.item_id != line.item_id
            or ledger.warehouse_id != original.warehouse_id
        ):
            errors.add(f"{context} 的流水 {ledger.pk} 没有反向对应原单同一明细。")
        if (
            ledger.quantity_delta != -original.quantity_delta
            or ledger.amount_delta != -original.amount_delta
            or ledger.unit_cost != original.unit_cost
            or ledger.quantity_before != original.quantity_after
            or ledger.quantity_after != original.quantity_before
            or ledger.amount_before != original.amount_after
            or ledger.amount_after != original.amount_before
            or ledger.average_unit_cost_before != original.average_unit_cost_after
            or ledger.average_unit_cost_after != original.average_unit_cost_before
        ):
            errors.add(
                f"{context} 的流水 {ledger.pk} 未精确恢复原流水 before/after 影响。"
            )
        if (
            line.quantity != original_line.quantity
            or line.posted_unit_cost != original_line.posted_unit_cost
            or line.posted_amount != original_line.posted_amount
        ):
            errors.add(f"{context} 的明细数量、成本或金额未复制原单快照。")
    if len(original_ids) != len(ledgers):
        errors.add(f"{context} 存在重复反向同一原流水的记录。")


def _validate_document_lines(*, company, errors):
    ledger_queryset = (
        SupplyStockLedger.objects.filter(company=company)
        .select_related(
            "reverses_ledger__document_line",
            "reverses_ledger__document",
            "reversal_ledger__document",
            "reversal_ledger__document_line",
        )
        .order_by("warehouse_id", "movement_type", "pk")
    )
    lines = (
        SupplyDocumentLine.objects.filter(company=company)
        .select_related(
            "document",
            "document__source_count_task__warehouse",
            "document__reversal_of",
            "item",
            "source_issue_line",
            "source_custody",
        )
        .prefetch_related(
            Prefetch(
                "stock_ledgers",
                queryset=ledger_queryset,
                to_attr="reconcile_stock_ledgers",
            )
        )
        .order_by("document_id", "line_no", "pk")
    )
    for line in lines.iterator(chunk_size=ITERATOR_CHUNK_SIZE):
        document = line.document
        ledgers = line.reconcile_stock_ledgers
        context = f"库存单据 {document.document_no} 第 {line.line_no} 行"
        if document.status not in FORMAL_DOCUMENT_STATUSES:
            if ledgers:
                errors.add(f"{context} 尚未正式过账却存在库存流水。")
            if line.posted_unit_cost is not None or line.posted_amount is not None:
                errors.add(f"{context} 尚未正式过账却保存了过账成本或金额。")
            continue
        if line.posted_unit_cost is None or line.posted_amount is None:
            errors.add(f"{context} 已正式过账但缺少过账成本或金额。")
            continue
        if document.document_type == SupplyDocumentType.REVERSAL:
            _validate_reversal_line(line, ledgers, errors)
            continue

        specs = _normal_line_specs(line)
        if not specs:
            errors.add(f"{context} 无法解析应有库存流水方向。")
            continue
        if len(ledgers) != len(specs):
            errors.add(
                f"{context} 应有 {len(specs)} 条库存流水，实际 {len(ledgers)} 条。"
            )
        remaining = list(ledgers)
        for movement_type, warehouse_id, quantity_delta, amount_delta in specs:
            matches = [
                ledger
                for ledger in remaining
                if ledger.movement_type == movement_type
                and ledger.warehouse_id == warehouse_id
            ]
            if len(matches) != 1:
                errors.add(
                    f"{context} 缺少唯一的 {movement_type} 流水或流水仓库错误。"
                )
                continue
            ledger = matches[0]
            remaining.remove(ledger)
            if (
                ledger.document_id != document.pk
                or ledger.document_line_id != line.pk
                or ledger.item_id != line.item_id
            ):
                errors.add(f"{context} 的流水 {ledger.pk} 引用关系不一致。")
            if ledger.quantity_delta != quantize_quantity(quantity_delta):
                errors.add(f"{context} 的流水 {ledger.pk} 数量方向或数值错误。")
            if ledger.amount_delta != quantize_money(amount_delta):
                errors.add(f"{context} 的流水 {ledger.pk} 金额方向或数值错误。")
            if ledger.unit_cost != line.posted_unit_cost:
                errors.add(f"{context} 的流水 {ledger.pk} 与明细过账成本不一致。")
            if ledger.reverses_ledger_id is not None:
                errors.add(f"{context} 的普通流水 {ledger.pk} 不得引用冲销来源。")

            reversal = getattr(ledger, "reversal_ledger", None)
            if document.status == SupplyDocumentStatus.REVERSED:
                reversal_document = getattr(document, "reversal_document", None)
                if (
                    reversal is None
                    or reversal_document is None
                    or reversal.document_id != reversal_document.pk
                    or reversal.document_line.line_no != line.line_no
                ):
                    errors.add(f"{context} 的原流水 {ledger.pk} 没有唯一完整反向流水。")
            elif reversal is not None:
                errors.add(f"{context} 尚未标记冲销但流水 {ledger.pk} 已被反向。")

        if remaining:
            errors.add(f"{context} 存在 {len(remaining)} 条无法解释的额外库存流水。")
        if document.document_type == SupplyDocumentType.TRANSFER and len(ledgers) == 2:
            outbound = next(
                (
                    value
                    for value in ledgers
                    if value.movement_type == SupplyStockMovementType.TRANSFER_OUT
                ),
                None,
            )
            inbound = next(
                (
                    value
                    for value in ledgers
                    if value.movement_type == SupplyStockMovementType.TRANSFER_IN
                ),
                None,
            )
            if (
                outbound is None
                or inbound is None
                or outbound.quantity_delta != -inbound.quantity_delta
                or outbound.amount_delta != -inbound.amount_delta
                or outbound.unit_cost != inbound.unit_cost
            ):
                errors.add(f"{context} 的调拨两腿数量、金额或成本不等额。")


def _reconcile_stock_balances(*, company):
    errors = _IntegrityErrors()
    expected = _stock_expected_from_ledgers(company=company, errors=errors)
    _document_integrity(company=company, errors=errors)
    _validate_document_lines(company=company, errors=errors)

    balances = {}
    balance_rows = (
        SupplyStockBalance.objects.filter(company=company)
        .values(
            "id",
            "warehouse_id",
            "warehouse__code",
            "warehouse__name",
            "warehouse__company_id",
            "item_id",
            "item__item_code",
            "item__name",
            "item__company_id",
            "quantity_on_hand",
            "amount_on_hand",
            "average_unit_cost",
        )
        .order_by("warehouse_id", "item_id")
    )
    for row in balance_rows.iterator(chunk_size=ITERATOR_CHUNK_SIZE):
        key = (row["warehouse_id"], row["item_id"])
        if (
            row["warehouse__company_id"] != company.pk
            or row["item__company_id"] != company.pk
        ):
            errors.add(f"库存余额 {row['id']} 存在跨公司仓库或物品引用。")
        balances[key] = row

    keys = sorted(
        set(expected) | set(balances), key=lambda key: (str(key[0]), str(key[1]))
    )
    differences = []
    for key in keys:
        target = expected.get(
            key,
            {"quantity": ZERO_QTY, "amount": ZERO_MONEY, "average": ZERO_COST},
        )
        balance = balances.get(key)
        current = {
            "quantity": balance["quantity_on_hand"] if balance else ZERO_QTY,
            "amount": balance["amount_on_hand"] if balance else ZERO_MONEY,
            "average": balance["average_unit_cost"] if balance else ZERO_COST,
        }
        if target["average"] is None or current != target:
            differences.append(
                {
                    "warehouse_id": key[0],
                    "item_id": key[1],
                    "warehouse": (
                        f"{balance['warehouse__code']} / {balance['warehouse__name']}"
                        if balance
                        else str(key[0])
                    ),
                    "item": (
                        f"{balance['item__item_code']} / {balance['item__name']}"
                        if balance
                        else str(key[1])
                    ),
                    "current": current,
                    "expected": target,
                    "balance_id": balance["id"] if balance else None,
                }
            )
    return ReconciliationResult(
        kind="stock",
        checked_count=len(keys),
        differences=tuple(differences),
        integrity_errors=errors.result(),
        expected=expected,
    )


def reconcile_stock_balances(*, company):
    with _stable_read_snapshot():
        return _reconcile_stock_balances(company=company)


def _custody_movement_shape(movement):
    if movement.action in {
        SupplyCustodyAction.ISSUE,
        SupplyCustodyAction.OPENING,
    }:
        return movement.from_custody_id is None and movement.to_custody_id is not None
    if movement.action in {
        SupplyCustodyAction.RETURN,
        SupplyCustodyAction.LOSS,
        SupplyCustodyAction.SCRAP,
    }:
        return movement.from_custody_id is not None and movement.to_custody_id is None
    if movement.action == SupplyCustodyAction.TRANSFER:
        return bool(
            movement.from_custody_id
            and movement.to_custody_id
            and movement.from_custody_id != movement.to_custody_id
        )
    if movement.action == SupplyCustodyAction.CORRECTION:
        return (movement.from_custody_id is None) != (movement.to_custody_id is None)
    if movement.action == SupplyCustodyAction.REVERSAL:
        original = movement.reverses_movement
        return bool(
            original
            and original.action != SupplyCustodyAction.REVERSAL
            and movement.from_custody_id == original.to_custody_id
            and movement.to_custody_id == original.from_custody_id
            and movement.quantity == original.quantity
            and movement.amount == original.amount
            and movement.unit_cost == original.unit_cost
        )
    return False


def _custody_rows(*, company):
    fields = (
        "id",
        "company_id",
        "item_id",
        "item__company_id",
        "item__item_type",
        "item__item_code",
        "item__name",
        "department_id",
        "department__name",
        "employee_id",
        "employee__name",
        "current_quantity",
        "current_amount",
        "unit_cost_snapshot",
        "started_on",
        "status",
        "parent_custody_id",
        "parent_custody__company_id",
        "parent_custody__item_id",
        "origin_issue_line_id",
        "origin_issue_line__company_id",
        "origin_issue_line__item_id",
        "origin_issue_line__quantity",
        "origin_issue_line__posted_unit_cost",
        "origin_issue_line__posted_amount",
        "origin_issue_line__document__document_type",
        "origin_issue_line__document__status",
        "origin_import_row_id",
        "origin_import_row__batch__company_id",
        "origin_import_row__batch__import_type",
        "origin_import_row__batch__status",
        "origin_import_row__validation_status",
    )
    return SupplyCustody.objects.filter(company=company).values(*fields).order_by("pk")


def _validate_custody_sources(custodies, errors):
    root_cache = {}
    for custody_id, custody in custodies.items():
        context = f"保管 {custody_id}"
        if (
            custody["item__company_id"] != custody["company_id"]
            or custody["item__item_type"] != SupplyItemType.DURABLE_QUANTITY
        ):
            errors.add(f"{context} 的物品公司或管理模式无效。")
        parent_id = custody["parent_custody_id"]
        if parent_id is None:
            if bool(custody["origin_issue_line_id"]) == bool(
                custody["origin_import_row_id"]
            ):
                errors.add(
                    f"{context} 的根来源必须且只能是领用明细或期初导入行之一。"
                )
            if custody["origin_issue_line_id"]:
                if (
                    custody["origin_issue_line__company_id"] != custody["company_id"]
                    or custody["origin_issue_line__item_id"] != custody["item_id"]
                    or custody["origin_issue_line__document__document_type"]
                    != SupplyDocumentType.ISSUE
                    or custody["origin_issue_line__document__status"]
                    not in FORMAL_DOCUMENT_STATUSES
                ):
                    errors.add(f"{context} 的根领用来源无效。")
            if custody["origin_import_row_id"]:
                if (
                    custody["origin_import_row__batch__company_id"]
                    != custody["company_id"]
                    or custody["origin_import_row__batch__import_type"]
                    != "opening_custody"
                    or custody["origin_import_row__batch__status"] != "confirmed"
                    or custody["origin_import_row__validation_status"] != "created"
                ):
                    errors.add(f"{context} 的期初导入根来源未形成完整确认链。")
        else:
            parent = custodies.get(parent_id)
            if (
                custody["origin_issue_line_id"] is not None
                or custody["origin_import_row_id"] is not None
            ):
                errors.add(f"{context} 是转交子保管却重复保存了根来源。")
            if (
                parent is None
                or parent_id == custody_id
                or custody["parent_custody__company_id"] != custody["company_id"]
                or custody["parent_custody__item_id"] != custody["item_id"]
            ):
                errors.add(
                    f"{context} 的父保管不存在、自引用、跨公司或物品不一致。"
                )

        if custody_id in root_cache:
            continue
        path = []
        positions = {}
        current_id = custody_id
        root_id = None
        while current_id is not None:
            if current_id in root_cache:
                root_id = root_cache[current_id]
                break
            if current_id in positions:
                cycle = path[positions[current_id] :]
                errors.add(
                    "保管父子链形成循环："
                    + " -> ".join(str(value) for value in cycle)
                )
                break
            current = custodies.get(current_id)
            if current is None:
                break
            positions[current_id] = len(path)
            path.append(current_id)
            if current["parent_custody_id"] is None:
                root_id = current_id
                break
            current_id = current["parent_custody_id"]
        for value in path:
            root_cache[value] = root_id


def _validate_custody_movement_source(movement, errors):
    context = f"保管流水 {movement.pk}"
    source_line = movement.source_document_line
    if movement.action == SupplyCustodyAction.ISSUE:
        if (
            source_line is None
            or source_line.document.document_type != SupplyDocumentType.ISSUE
            or source_line.document.status not in FORMAL_DOCUMENT_STATUSES
            or movement.to_custody.origin_issue_line_id != source_line.pk
            or source_line.quantity != movement.quantity
            or source_line.posted_amount != movement.amount
            or source_line.posted_unit_cost != movement.unit_cost
        ):
            errors.add(f"{context} 没有唯一勾稽到耐用品领用明细。")
    elif movement.action == SupplyCustodyAction.RETURN:
        # A transferred child custody intentionally has no direct issue-line
        # source. The custody FK, return line and movement still form the
        # authoritative link; root provenance is validated via parent_custody.
        if (
            source_line is None
            or source_line.document.document_type != SupplyDocumentType.RETURN
            or source_line.document.status not in FORMAL_DOCUMENT_STATUSES
            or source_line.source_custody_id != movement.from_custody_id
            or source_line.quantity != movement.quantity
            or source_line.posted_amount != movement.amount
            or source_line.posted_unit_cost != movement.unit_cost
        ):
            errors.add(f"{context} 没有唯一勾稽到耐用品归还明细。")
    elif movement.action == SupplyCustodyAction.REVERSAL:
        if (
            source_line is None
            or source_line.document.document_type != SupplyDocumentType.REVERSAL
            or source_line.document.status != SupplyDocumentStatus.POSTED
            or movement.reverses_movement is None
            or movement.reverses_movement.source_document_line is None
            or source_line.document.reversal_of_id
            != movement.reverses_movement.source_document_line.document_id
            or source_line.line_no
            != movement.reverses_movement.source_document_line.line_no
        ):
            errors.add(f"{context} 的冲销单据和原保管动作链不完整。")
    elif source_line is not None:
        errors.add(f"{context} 的动作不应引用库存单据明细。")

    if source_line is not None:
        if movement.business_date != source_line.document.business_date:
            errors.add(f"{context} 的业务日期与来源单据不一致。")
        if (
            source_line.company_id != movement.company_id
            or source_line.item_id != movement.item_id
        ):
            errors.add(f"{context} 的来源单据明细跨公司或物品不一致。")


def _validate_closed_count_evidence(*, company, errors):
    action_for_resolution = {
        "return": SupplyCustodyAction.RETURN,
        "transfer": SupplyCustodyAction.TRANSFER,
        "loss": SupplyCustodyAction.LOSS,
        "scrap": SupplyCustodyAction.SCRAP,
        "correction": SupplyCustodyAction.CORRECTION,
    }
    lines = (
        SupplyCountLine.objects.filter(
            company=company,
            count_task__count_domain=SupplyCountDomain.CUSTODY,
            count_task__status=SupplyCountStatus.CLOSED,
            resolution_custody_movement__isnull=False,
        )
        .select_related(
            "count_task",
            "custody",
            "resolution_custody_movement__reversal_movement",
        )
        .order_by("pk")
    )
    for line in lines.iterator(chunk_size=ITERATOR_CHUNK_SIZE):
        movement = line.resolution_custody_movement
        context = f"已关闭保管盘点行 {line.pk}"
        if (
            movement.action == SupplyCustodyAction.REVERSAL
            or getattr(movement, "reversal_movement", None) is not None
        ):
            errors.add(f"{context} 的解决证据指向已冲销或冲销动作流水。")
        if action_for_resolution.get(line.resolution_type) != movement.action:
            errors.add(f"{context} 的解决方式与保管流水动作不一致。")
        if (
            movement.company_id != company.pk
            or movement.item_id != line.item_id
            or line.custody_id
            not in {movement.from_custody_id, movement.to_custody_id}
        ):
            errors.add(f"{context} 的解决流水不属于同一公司、物品或保管记录。")


def _reconcile_custodies(*, company):
    errors = _IntegrityErrors()
    custodies = {
        row["id"]: row
        for row in _custody_rows(company=company).iterator(
            chunk_size=ITERATOR_CHUNK_SIZE
        )
    }
    _validate_custody_sources(custodies, errors)

    totals = defaultdict(lambda: {"quantity": ZERO_QTY, "amount": ZERO_MONEY})
    entrance_counts = defaultdict(int)
    entrance_movements = {}
    movements = (
        SupplyCustodyMovement.objects.filter(company=company)
        .select_related(
            "item",
            "from_custody",
            "to_custody",
            "source_document_line__document",
            "reverses_movement__source_document_line__document",
            "reversal_movement__source_document_line__document",
        )
        .order_by("created_at", "pk")
    )
    for movement in movements.iterator(chunk_size=ITERATOR_CHUNK_SIZE):
        context = f"保管流水 {movement.pk}"
        if not _custody_movement_shape(movement):
            errors.add(f"{context} 的动作方向或冲销快照无效。")
        if (
            movement.item.company_id != company.pk
            or movement.item.item_type != SupplyItemType.DURABLE_QUANTITY
        ):
            errors.add(f"{context} 的物品公司或管理模式无效。")
        for custody_id in (movement.from_custody_id, movement.to_custody_id):
            if custody_id is None:
                continue
            custody = custodies.get(custody_id)
            if custody is None or custody["item_id"] != movement.item_id:
                errors.add(
                    f"{context} 引用的保管记录不存在、跨公司或物品不一致。"
                )
            elif movement.unit_cost != custody["unit_cost_snapshot"]:
                errors.add(f"{context} 的单位成本没有沿用保管成本快照。")

        _validate_custody_movement_source(movement, errors)
        reverse = getattr(movement, "reversal_movement", None)
        if movement.action != SupplyCustodyAction.REVERSAL and reverse is not None:
            if movement.action not in {
                SupplyCustodyAction.ISSUE,
                SupplyCustodyAction.RETURN,
            }:
                errors.add(f"{context} 的动作类型不允许库存单据冲销。")
            source_line = movement.source_document_line
            if (
                source_line is None
                or source_line.document.status != SupplyDocumentStatus.REVERSED
            ):
                errors.add(f"{context} 已有反向流水但原单未形成已冲销状态链。")
        elif (
            movement.action
            in {SupplyCustodyAction.ISSUE, SupplyCustodyAction.RETURN}
            and movement.source_document_line is not None
            and movement.source_document_line.document.status
            == SupplyDocumentStatus.REVERSED
            and reverse is None
        ):
            errors.add(f"{context} 的原单已冲销但缺少保管反向流水。")

        if movement.action in {
            SupplyCustodyAction.ISSUE,
            SupplyCustodyAction.OPENING,
            SupplyCustodyAction.TRANSFER,
        } and movement.to_custody_id is not None:
            entrance_counts[movement.to_custody_id] += 1
            entrance_movements.setdefault(movement.to_custody_id, movement)

        if movement.from_custody_id:
            source = totals[movement.from_custody_id]
            source["quantity"] = quantize_quantity(
                source["quantity"] - movement.quantity
            )
            source["amount"] = quantize_money(source["amount"] - movement.amount)
            if source["quantity"] < ZERO_QTY or source["amount"] < ZERO_MONEY:
                errors.add(
                    f"{context} 使保管 {movement.from_custody_id} 在该时点出现负数。"
                )
        if movement.to_custody_id:
            target = totals[movement.to_custody_id]
            target["quantity"] = quantize_quantity(
                target["quantity"] + movement.quantity
            )
            target["amount"] = quantize_money(target["amount"] + movement.amount)

    differences = []
    expected = {}
    for custody_id, custody in sorted(custodies.items(), key=lambda item: str(item[0])):
        context = f"保管 {custody_id}"
        entrance = entrance_movements.get(custody_id)
        if entrance_counts[custody_id] != 1 or entrance is None:
            errors.add(
                f"{context} 应有且只能有一条领用、期初或转交入口流水，"
                f"实际 {entrance_counts[custody_id]} 条。"
            )
        elif custody["parent_custody_id"] is None and custody["origin_issue_line_id"]:
            if (
                entrance.action != SupplyCustodyAction.ISSUE
                or entrance.from_custody_id is not None
                or entrance.source_document_line_id != custody["origin_issue_line_id"]
            ):
                errors.add(f"{context} 的唯一入口没有勾稽到根领用来源。")
        elif custody["parent_custody_id"] is None and custody["origin_import_row_id"]:
            if (
                entrance.action != SupplyCustodyAction.OPENING
                or entrance.from_custody_id is not None
            ):
                errors.add(f"{context} 的唯一入口不是期初保管流水。")
        elif custody["parent_custody_id"] is not None:
            if (
                entrance.action != SupplyCustodyAction.TRANSFER
                or entrance.from_custody_id != custody["parent_custody_id"]
            ):
                errors.add(f"{context} 的唯一入口没有勾稽到父保管转交。")
        if entrance is not None and entrance.business_date != custody["started_on"]:
            errors.add(f"{context} 的开始日期与唯一入口流水业务日期不一致。")

        target = totals[custody_id]
        if target["quantity"] < ZERO_QTY or target["amount"] < ZERO_MONEY:
            errors.add(f"{context} 的流水重建结果为负数。")
            status = "invalid"
        elif target["quantity"] == ZERO_QTY:
            if target["amount"] != ZERO_MONEY:
                errors.add(f"{context} 的流水数量为 0 但金额不为 0。")
                status = "invalid"
            else:
                status = SupplyCustodyStatus.CLOSED
        else:
            status = SupplyCustodyStatus.OPEN
        target = {**target, "status": status}
        expected[custody_id] = target
        current = {
            "quantity": custody["current_quantity"],
            "amount": custody["current_amount"],
            "status": custody["status"],
        }
        if current != target:
            differences.append(
                {
                    "custody_id": custody_id,
                    "item": f"{custody['item__item_code']} / {custody['item__name']}",
                    "department": custody["department__name"],
                    "employee": custody["employee__name"] or "",
                    "current": current,
                    "expected": target,
                }
            )
    _validate_closed_count_evidence(company=company, errors=errors)
    return ReconciliationResult(
        kind="custody",
        checked_count=len(custodies),
        differences=tuple(differences),
        integrity_errors=errors.result(),
        expected=expected,
    )


def reconcile_custodies(*, company):
    with _stable_read_snapshot():
        return _reconcile_custodies(company=company)


def _audit_rebuild(*, actor, company, kind, reason, before):
    return write_business_audit_log(
        company=company,
        user=actor,
        action=f"supply_{kind}_balances_rebuilt",
        object_type="SupplyStockBalance" if kind == "stock" else "SupplyCustody",
        object_id=company.pk,
        old_data={"difference_count": len(before.differences)},
        new_data={
            "difference_count": 0,
            "reason": reason,
            "checked_count": before.checked_count,
        },
    )


def _validate_rebuild_authority(*, company, actor):
    active_company = current_company()
    if (
        company is None
        or not getattr(company, "pk", None)
        or active_company is None
        or active_company.pk != company.pk
        or not Company.objects.filter(pk=company.pk, is_active=True).exists()
    ):
        raise ValidationError("余额重建只能针对当前启用公司执行。")
    if (
        actor is None
        or not getattr(actor, "pk", None)
        or not getattr(actor, "is_active", False)
        or not getattr(actor, "is_authenticated", False)
    ):
        raise ValidationError(
            "余额重建操作人必须是启用的 application system_admin 或 finance 用户。"
        )
    if not role_names_for(actor).intersection(SUPPLY_REBUILD_ROLES):
        raise PermissionDenied("只有系统管理员或财务可以执行低值物品余额重建。")
    employee_links = Employee.objects.filter(user=actor)
    if employee_links.exists() and employee_links.exclude(company=company).exists():
        raise ValidationError("余额重建操作人的员工关系不属于指定公司。")


def rebuild_stock_balances(*, company, actor, reason, confirm=False):
    _validate_rebuild_authority(company=company, actor=actor)
    reason = str(reason or "").strip()
    if not reason:
        raise ValidationError("库存余额重建必须填写原因。")
    if not confirm:
        return reconcile_stock_balances(company=company)
    with transaction.atomic():
        warehouses = list(
            SupplyWarehouse.objects.select_for_update()
            .filter(company=company)
            .order_by("pk")
        )
        if SupplyCountTask.objects.filter(
            company=company,
            count_domain=SupplyCountDomain.WAREHOUSE_STOCK,
            status__in=ACTIVE_COUNT_STATUSES,
        ).exists():
            raise ValidationError("存在活动仓库盘点，不能重建库存余额。")
        list(
            SupplyStockBalance.objects.select_for_update()
            .filter(company=company)
            .order_by("warehouse_id", "item_id", "pk")
        )
        before = _reconcile_stock_balances(company=company)
        if before.integrity_errors:
            raise ValidationError(list(before.integrity_errors))
        warehouse_map = {warehouse.pk: warehouse for warehouse in warehouses}
        now = timezone.now()
        for difference in before.differences:
            target = difference["expected"]
            if target["average"] is None:
                raise ValidationError("流水汇总平均成本无效，已拒绝重建。")
            balance = (
                SupplyStockBalance.objects.select_for_update()
                .filter(
                    company=company,
                    warehouse_id=difference["warehouse_id"],
                    item_id=difference["item_id"],
                )
                .first()
            )
            if balance is None:
                item = SupplyItem.objects.get(
                    company=company, pk=difference["item_id"]
                )
                balance = _lock_or_create_balance(
                    company=company,
                    warehouse=warehouse_map[difference["warehouse_id"]],
                    item=item,
                )
            _update_balance_values(
                balance=balance,
                quantity=target["quantity"],
                amount=target["amount"],
                average_unit_cost=target["average"],
                updated_at=now,
            )
        after = _reconcile_stock_balances(company=company)
        if not after.is_consistent:
            raise ValidationError("库存余额重建后核对仍不一致，事务已回滚。")
        if before.differences:
            _audit_rebuild(
                actor=actor,
                company=company,
                kind="stock",
                reason=reason,
                before=before,
            )
        return after


def rebuild_custodies(*, company, actor, reason, confirm=False):
    _validate_rebuild_authority(company=company, actor=actor)
    reason = str(reason or "").strip()
    if not reason:
        raise ValidationError("保管余额重建必须填写原因。")
    if not confirm:
        return reconcile_custodies(company=company)
    with transaction.atomic():
        custodies = list(
            SupplyCustody.objects.select_for_update()
            .filter(company=company)
            .order_by("pk")
        )
        if SupplyCountLine.objects.filter(
            company=company,
            custody_id__in=[custody.pk for custody in custodies],
            count_task__status__in=ACTIVE_COUNT_STATUSES,
        ).exists():
            raise ValidationError("存在活动保管盘点，不能重建保管余额。")
        before = _reconcile_custodies(company=company)
        if before.integrity_errors:
            raise ValidationError(list(before.integrity_errors))
        now = timezone.now()
        for custody in custodies:
            target = before.expected[custody.pk]
            if target["status"] not in {
                SupplyCustodyStatus.OPEN,
                SupplyCustodyStatus.CLOSED,
            }:
                raise ValidationError("保管流水重建结果无效，已拒绝写入。")
            current = {
                "quantity": custody.current_quantity,
                "amount": custody.current_amount,
                "status": custody.status,
            }
            if current == target:
                continue
            _update_custody_values(
                custody=custody,
                quantity=target["quantity"],
                amount=target["amount"],
                status=target["status"],
                updated_at=now,
            )
        after = _reconcile_custodies(company=company)
        if not after.is_consistent:
            raise ValidationError("保管余额重建后核对仍不一致，事务已回滚。")
        if before.differences:
            _audit_rebuild(
                actor=actor,
                company=company,
                kind="custody",
                reason=reason,
                before=before,
            )
        return after


__all__ = [
    "ReconciliationResult",
    "rebuild_custodies",
    "rebuild_stock_balances",
    "reconcile_custodies",
    "reconcile_stock_balances",
]
