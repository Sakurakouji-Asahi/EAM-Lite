from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.supplies.domain import (
    ZERO_COST,
    ZERO_MONEY,
    calculate_average_unit_cost,
    calculate_moving_average,
    calculate_receipt,
    calculate_receipt_amount,
    quantize_money,
    quantize_quantity,
    quantize_unit_cost,
    validate_zero_cost_reason,
)


def test_supply_decimal_quantization_uses_half_up_and_rejects_float():
    assert quantize_quantity("1.23445") == Decimal("1.2345")
    assert quantize_unit_cost("1.2345675") == Decimal("1.234568")
    assert quantize_money("1.005") == Decimal("1.01")
    with pytest.raises(ValidationError):
        quantize_money(1.25)


def test_receipt_amount_and_moving_average_are_exact_decimal():
    assert calculate_receipt_amount("10", "100") == Decimal("1000.00")
    result = calculate_receipt("10", "1000", "10", "120")
    assert result.receipt_amount == Decimal("1200.00")
    assert result.quantity_after == Decimal("20.0000")
    assert result.amount_after == Decimal("2200.00")
    assert result.average_unit_cost_after == Decimal("110.000000")
    assert calculate_moving_average("10", "1000", "10", "120") == (
        Decimal("20.0000"),
        Decimal("2200.00"),
        Decimal("110.000000"),
    )


def test_zero_quantity_clears_average_and_zero_cost_requires_reason():
    assert calculate_average_unit_cost("0", "0") == ZERO_COST
    with pytest.raises(ValidationError):
        calculate_average_unit_cost("0", "0.01")
    with pytest.raises(ValidationError):
        validate_zero_cost_reason("0", "")
    assert validate_zero_cost_reason("0", "赠品入库") == "赠品入库"
    assert calculate_receipt("0", ZERO_MONEY, "1", "0").amount_after == ZERO_MONEY
