"""Pure Decimal rules for quantity-managed low-value goods stock."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError


QTY_QUANT = Decimal("0.0001")
COST_QUANT = Decimal("0.000001")
MONEY_QUANT = Decimal("0.01")

ZERO_QTY = Decimal("0.0000")
ZERO_COST = Decimal("0.000000")
ZERO_MONEY = Decimal("0.00")


def _decimal(value, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationError(f"{label}必须使用 Decimal 或十进制文本，不能使用 float/布尔值。")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{label}必须是有效十进制数。") from exc
    if not result.is_finite():
        raise ValidationError(f"{label}必须是有限十进制数。")
    return result


def _quantize(value, *, label: str, quantum: Decimal) -> Decimal:
    result = _decimal(value, label=label)
    try:
        return result.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValidationError(f"{label}超出系统支持的十进制范围。") from exc


def quantize_quantity(value) -> Decimal:
    return _quantize(value, label="数量", quantum=QTY_QUANT)


def quantize_unit_cost(value) -> Decimal:
    return _quantize(value, label="单位成本", quantum=COST_QUANT)


def quantize_money(value) -> Decimal:
    return _quantize(value, label="金额", quantum=MONEY_QUANT)


def calculate_receipt_amount(quantity, unit_cost) -> Decimal:
    receipt_quantity = quantize_quantity(quantity)
    receipt_unit_cost = quantize_unit_cost(unit_cost)
    if receipt_quantity <= ZERO_QTY:
        raise ValidationError("入库数量必须大于 0。")
    if receipt_unit_cost < ZERO_COST:
        raise ValidationError("入库单位成本不得小于 0。")
    return quantize_money(receipt_quantity * receipt_unit_cost)


def calculate_average_unit_cost(quantity, amount) -> Decimal:
    balance_quantity = quantize_quantity(quantity)
    balance_amount = quantize_money(amount)
    if balance_quantity < ZERO_QTY or balance_amount < ZERO_MONEY:
        raise ValidationError("库存数量和金额不得小于 0。")
    if balance_quantity == ZERO_QTY:
        if balance_amount != ZERO_MONEY:
            raise ValidationError("库存数量为 0 时库存金额必须同时为 0。")
        return ZERO_COST
    return quantize_unit_cost(balance_amount / balance_quantity)


@dataclass(frozen=True)
class ReceiptCalculation:
    receipt_quantity: Decimal
    receipt_unit_cost: Decimal
    receipt_amount: Decimal
    quantity_after: Decimal
    amount_after: Decimal
    average_unit_cost_after: Decimal


@dataclass(frozen=True)
class IssueCalculation:
    issue_quantity: Decimal
    issue_unit_cost: Decimal
    issue_amount: Decimal
    quantity_after: Decimal
    amount_after: Decimal
    average_unit_cost_after: Decimal


@dataclass(frozen=True)
class CustodyAmountAllocation:
    action_quantity: Decimal
    action_amount: Decimal
    quantity_after: Decimal
    amount_after: Decimal


def calculate_receipt(
    balance_quantity,
    balance_amount,
    receipt_quantity,
    receipt_unit_cost,
) -> ReceiptCalculation:
    quantity_before = quantize_quantity(balance_quantity)
    amount_before = quantize_money(balance_amount)
    if quantity_before < ZERO_QTY or amount_before < ZERO_MONEY:
        raise ValidationError("过账前库存数量和金额不得小于 0。")
    if quantity_before == ZERO_QTY and amount_before != ZERO_MONEY:
        raise ValidationError("过账前库存数量为 0 时库存金额必须为 0。")

    in_quantity = quantize_quantity(receipt_quantity)
    in_unit_cost = quantize_unit_cost(receipt_unit_cost)
    in_amount = calculate_receipt_amount(in_quantity, in_unit_cost)
    quantity_after = quantize_quantity(quantity_before + in_quantity)
    amount_after = quantize_money(amount_before + in_amount)
    average_after = calculate_average_unit_cost(quantity_after, amount_after)
    return ReceiptCalculation(
        receipt_quantity=in_quantity,
        receipt_unit_cost=in_unit_cost,
        receipt_amount=in_amount,
        quantity_after=quantity_after,
        amount_after=amount_after,
        average_unit_cost_after=average_after,
    )


def calculate_receipt_from_amount(
    balance_quantity,
    balance_amount,
    receipt_quantity,
    receipt_amount,
) -> ReceiptCalculation:
    """Add stock using an already quantified, authoritative amount.

    Returns and transfers must carry the exact amount from their source.  They
    must not multiply a six-decimal unit-cost snapshot a second time because
    that can create an otherwise unexplained one-cent difference.
    """

    quantity_before = quantize_quantity(balance_quantity)
    amount_before = quantize_money(balance_amount)
    if quantity_before < ZERO_QTY or amount_before < ZERO_MONEY:
        raise ValidationError("过账前库存数量和金额不得小于 0。")
    if quantity_before == ZERO_QTY and amount_before != ZERO_MONEY:
        raise ValidationError("过账前库存数量为 0 时库存金额必须为 0。")

    in_quantity = quantize_quantity(receipt_quantity)
    in_amount = quantize_money(receipt_amount)
    if in_quantity <= ZERO_QTY:
        raise ValidationError("入库数量必须大于 0。")
    if in_amount < ZERO_MONEY:
        raise ValidationError("入库金额不得小于 0。")
    in_unit_cost = quantize_unit_cost(in_amount / in_quantity)
    quantity_after = quantize_quantity(quantity_before + in_quantity)
    amount_after = quantize_money(amount_before + in_amount)
    average_after = calculate_average_unit_cost(quantity_after, amount_after)
    return ReceiptCalculation(
        receipt_quantity=in_quantity,
        receipt_unit_cost=in_unit_cost,
        receipt_amount=in_amount,
        quantity_after=quantity_after,
        amount_after=amount_after,
        average_unit_cost_after=average_after,
    )


def calculate_issue(
    balance_quantity,
    balance_amount,
    issue_quantity,
) -> IssueCalculation:
    """Remove stock at the current moving average without leaving a tail."""

    quantity_before = quantize_quantity(balance_quantity)
    amount_before = quantize_money(balance_amount)
    out_quantity = quantize_quantity(issue_quantity)
    if quantity_before < ZERO_QTY or amount_before < ZERO_MONEY:
        raise ValidationError("过账前库存数量和金额不得小于 0。")
    if quantity_before == ZERO_QTY and amount_before != ZERO_MONEY:
        raise ValidationError("过账前库存数量为 0 时库存金额必须为 0。")
    if out_quantity <= ZERO_QTY:
        raise ValidationError("出库数量必须大于 0。")
    if out_quantity > quantity_before:
        raise ValidationError("库存不足。")

    issue_unit_cost = calculate_average_unit_cost(
        quantity_before, amount_before
    )
    if out_quantity == quantity_before:
        issue_amount = amount_before
        quantity_after = ZERO_QTY
        amount_after = ZERO_MONEY
        average_after = ZERO_COST
    else:
        issue_amount = quantize_money(out_quantity * issue_unit_cost)
        quantity_after = quantize_quantity(quantity_before - out_quantity)
        amount_after = quantize_money(amount_before - issue_amount)
        if amount_after < ZERO_MONEY:
            raise ValidationError("出库金额超过当前库存金额。")
        average_after = calculate_average_unit_cost(
            quantity_after, amount_after
        )
    return IssueCalculation(
        issue_quantity=out_quantity,
        issue_unit_cost=issue_unit_cost,
        issue_amount=issue_amount,
        quantity_after=quantity_after,
        amount_after=amount_after,
        average_unit_cost_after=average_after,
    )


def allocate_custody_amount(
    *,
    current_quantity,
    current_amount,
    unit_cost_snapshot,
    action_quantity,
) -> CustodyAmountAllocation:
    """Split one custody balance without leaving a final monetary tail.

    Partial actions use the immutable custody cost snapshot.  The final action
    consumes the exact remaining amount so return, transfer, loss and scrap
    all follow one rule and can never leave quantity zero with CNY 0.01 behind.
    """

    quantity_before = quantize_quantity(current_quantity)
    amount_before = quantize_money(current_amount)
    cost_snapshot = quantize_unit_cost(unit_cost_snapshot)
    quantity = quantize_quantity(action_quantity)
    if quantity_before <= ZERO_QTY:
        raise ValidationError("当前保管数量必须大于 0。")
    if amount_before < ZERO_MONEY or cost_snapshot < ZERO_COST:
        raise ValidationError("当前保管金额和单位成本不得小于 0。")
    if quantity <= ZERO_QTY:
        raise ValidationError("处理数量必须大于 0。")
    if quantity > quantity_before:
        raise ValidationError(
            f"处理数量超过当前保管数量，当前最多可处理 {quantity_before}。"
        )

    if quantity == quantity_before:
        action_amount = amount_before
        quantity_after = ZERO_QTY
        amount_after = ZERO_MONEY
    else:
        action_amount = quantize_money(quantity * cost_snapshot)
        if action_amount > amount_before:
            raise ValidationError("本次处理金额超过当前保管金额，请先执行保管核对。")
        quantity_after = quantize_quantity(quantity_before - quantity)
        amount_after = quantize_money(amount_before - action_amount)
        if quantity_after <= ZERO_QTY:
            raise ValidationError("部分处理后的保管数量必须大于 0。")
        if amount_after < ZERO_MONEY:
            raise ValidationError("处理后保管金额不得小于 0。")

    if quantity_after == ZERO_QTY and amount_after != ZERO_MONEY:
        raise ValidationError("处理后数量为 0 时保管金额必须同时为 0。")
    return CustodyAmountAllocation(
        action_quantity=quantity,
        action_amount=action_amount,
        quantity_after=quantity_after,
        amount_after=amount_after,
    )


def calculate_moving_average(
    balance_quantity,
    balance_amount,
    receipt_quantity,
    receipt_unit_cost,
) -> tuple[Decimal, Decimal, Decimal]:
    result = calculate_receipt(
        balance_quantity,
        balance_amount,
        receipt_quantity,
        receipt_unit_cost,
    )
    return (
        result.quantity_after,
        result.amount_after,
        result.average_unit_cost_after,
    )


def validate_zero_cost_reason(unit_cost, reason) -> str:
    cost = quantize_unit_cost(unit_cost)
    if cost < ZERO_COST:
        raise ValidationError("入库单位成本不得小于 0。")
    cleaned = str(reason or "").strip()
    if cost == ZERO_COST and not cleaned:
        raise ValidationError("0 成本入库必须填写明确原因。")
    return cleaned
