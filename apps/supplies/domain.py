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


def quantize_quantity(value) -> Decimal:
    return _decimal(value, label="数量").quantize(QTY_QUANT, rounding=ROUND_HALF_UP)


def quantize_unit_cost(value) -> Decimal:
    return _decimal(value, label="单位成本").quantize(
        COST_QUANT, rounding=ROUND_HALF_UP
    )


def quantize_money(value) -> Decimal:
    return _decimal(value, label="金额").quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


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
