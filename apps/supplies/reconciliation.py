"""Read-only reconciliation and maintenance-window cache rebuild services."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.services import write_business_audit_log
from apps.supplies.domain import (
    ZERO_COST,
    ZERO_MONEY,
    ZERO_QTY,
    calculate_average_unit_cost,
    quantize_money,
    quantize_quantity,
)
from apps.supplies.models import (
    SupplyCountDomain,
    SupplyCountLine,
    SupplyCountStatus,
    SupplyCountTask,
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyCustodyStatus,
    SupplyItem,
    SupplyStockBalance,
    SupplyStockLedger,
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


def reconcile_stock_balances(*, company):
    expected = {}
    integrity_errors = []
    ledger_rows = (
        SupplyStockLedger.objects.filter(company=company)
        .values("warehouse_id", "item_id")
        .annotate(quantity=Sum("quantity_delta"), amount=Sum("amount_delta"))
    )
    for row in ledger_rows:
        quantity = quantize_quantity(row["quantity"] or ZERO_QTY)
        amount = quantize_money(row["amount"] or ZERO_MONEY)
        try:
            average = calculate_average_unit_cost(quantity, amount)
        except ValidationError as exc:
            integrity_errors.append(
                f"仓库 {row['warehouse_id']} / 物品 {row['item_id']} 的流水汇总无效："
                + "；".join(exc.messages)
            )
            average = None
        if quantity < ZERO_QTY or amount < ZERO_MONEY:
            integrity_errors.append(
                f"仓库 {row['warehouse_id']} / 物品 {row['item_id']} 的流水汇总为负数。"
            )
        expected[(row["warehouse_id"], row["item_id"])] = {
            "quantity": quantity,
            "amount": amount,
            "average": average,
        }
    balances = {
        (balance.warehouse_id, balance.item_id): balance
        for balance in SupplyStockBalance.objects.filter(company=company).select_related(
            "warehouse", "item"
        )
    }
    keys = sorted(set(expected) | set(balances), key=lambda key: (str(key[0]), str(key[1])))
    differences = []
    for key in keys:
        target = expected.get(
            key,
            {"quantity": ZERO_QTY, "amount": ZERO_MONEY, "average": ZERO_COST},
        )
        balance = balances.get(key)
        current = {
            "quantity": balance.quantity_on_hand if balance else ZERO_QTY,
            "amount": balance.amount_on_hand if balance else ZERO_MONEY,
            "average": balance.average_unit_cost if balance else ZERO_COST,
        }
        if target["average"] is None or current != target:
            differences.append(
                {
                    "warehouse_id": key[0],
                    "item_id": key[1],
                    "warehouse": str(balance.warehouse) if balance else str(key[0]),
                    "item": str(balance.item) if balance else str(key[1]),
                    "current": current,
                    "expected": target,
                    "balance_id": getattr(balance, "pk", None),
                }
            )
    return ReconciliationResult(
        kind="stock",
        checked_count=len(keys),
        differences=tuple(differences),
        integrity_errors=tuple(dict.fromkeys(integrity_errors)),
        expected=expected,
    )


def _custody_movement_shape(movement):
    if movement.action in {"issue", "opening"}:
        return movement.from_custody_id is None and movement.to_custody_id is not None
    if movement.action in {"return", "loss", "scrap"}:
        return movement.from_custody_id is not None and movement.to_custody_id is None
    if movement.action == "transfer":
        return bool(
            movement.from_custody_id
            and movement.to_custody_id
            and movement.from_custody_id != movement.to_custody_id
        )
    if movement.action == "correction":
        return (movement.from_custody_id is None) != (movement.to_custody_id is None)
    if movement.action == "reversal":
        original = movement.reverses_movement
        return bool(
            original
            and movement.from_custody_id == original.to_custody_id
            and movement.to_custody_id == original.from_custody_id
            and movement.quantity == original.quantity
            and movement.amount == original.amount
            and movement.unit_cost == original.unit_cost
        )
    return False


def reconcile_custodies(*, company):
    totals = defaultdict(lambda: {"quantity": ZERO_QTY, "amount": ZERO_MONEY})
    integrity_errors = []
    custodies = {
        custody.pk: custody
        for custody in SupplyCustody.objects.filter(company=company).select_related(
            "item", "department", "employee", "parent_custody"
        )
    }
    movements = SupplyCustodyMovement.objects.filter(company=company).select_related(
        "reverses_movement"
    ).order_by("created_at", "pk")
    for movement in movements.iterator(chunk_size=1000):
        if not _custody_movement_shape(movement):
            integrity_errors.append(f"保管流水 {movement.pk} 的动作方向或冲销快照无效。")
        for custody_id in (movement.from_custody_id, movement.to_custody_id):
            if custody_id is None:
                continue
            custody = custodies.get(custody_id)
            if custody is None or custody.item_id != movement.item_id:
                integrity_errors.append(
                    f"保管流水 {movement.pk} 引用的保管记录不存在、跨公司或物品不一致。"
                )
        if movement.to_custody_id:
            target = totals[movement.to_custody_id]
            target["quantity"] = quantize_quantity(target["quantity"] + movement.quantity)
            target["amount"] = quantize_money(target["amount"] + movement.amount)
        if movement.from_custody_id:
            source = totals[movement.from_custody_id]
            source["quantity"] = quantize_quantity(source["quantity"] - movement.quantity)
            source["amount"] = quantize_money(source["amount"] - movement.amount)
    differences = []
    expected = {}
    for custody_id, custody in sorted(custodies.items(), key=lambda item: str(item[0])):
        target = totals[custody_id]
        if target["quantity"] < ZERO_QTY or target["amount"] < ZERO_MONEY:
            integrity_errors.append(f"保管 {custody_id} 的流水重建结果为负数。")
            status = "invalid"
        elif target["quantity"] == ZERO_QTY:
            if target["amount"] != ZERO_MONEY:
                integrity_errors.append(f"保管 {custody_id} 的流水数量为 0 但金额不为 0。")
                status = "invalid"
            else:
                status = SupplyCustodyStatus.CLOSED
        else:
            status = SupplyCustodyStatus.OPEN
        target = {**target, "status": status}
        expected[custody_id] = target
        current = {
            "quantity": custody.current_quantity,
            "amount": custody.current_amount,
            "status": custody.status,
        }
        if current != target:
            differences.append(
                {
                    "custody_id": custody_id,
                    "item": str(custody.item),
                    "department": str(custody.department),
                    "employee": str(custody.employee or ""),
                    "current": current,
                    "expected": target,
                }
            )
    return ReconciliationResult(
        kind="custody",
        checked_count=len(custodies),
        differences=tuple(differences),
        integrity_errors=tuple(dict.fromkeys(integrity_errors)),
        expected=expected,
    )


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


def rebuild_stock_balances(*, company, actor, reason, confirm=False):
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
        before = reconcile_stock_balances(company=company)
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
        after = reconcile_stock_balances(company=company)
        if not after.is_consistent:
            raise ValidationError("库存余额重建后核对仍不一致，事务已回滚。")
        if before.differences:
            _audit_rebuild(
                actor=actor, company=company, kind="stock", reason=reason, before=before
            )
        return after


def rebuild_custodies(*, company, actor, reason, confirm=False):
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
        before = reconcile_custodies(company=company)
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
        after = reconcile_custodies(company=company)
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
