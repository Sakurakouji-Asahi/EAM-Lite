from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.supplies.domain import allocate_custody_amount


def test_allocate_custody_amount_partial_and_final_tail_rules():
    partial = allocate_custody_amount(
        current_quantity=Decimal("3.0000"),
        current_amount=Decimal("10.01"),
        unit_cost_snapshot=Decimal("3.336667"),
        action_quantity=Decimal("1.0000"),
    )
    assert partial.action_amount == Decimal("3.34")
    assert partial.quantity_after == Decimal("2.0000")
    assert partial.amount_after == Decimal("6.67")

    final = allocate_custody_amount(
        current_quantity=partial.quantity_after,
        current_amount=partial.amount_after,
        unit_cost_snapshot=Decimal("3.336667"),
        action_quantity=Decimal("2.0000"),
    )
    assert final.action_amount == Decimal("6.67")
    assert final.quantity_after == Decimal("0.0000")
    assert final.amount_after == Decimal("0.00")


def test_allocate_custody_amount_allows_zero_cost_and_rejects_invalid_quantity():
    result = allocate_custody_amount(
        current_quantity=Decimal("2"),
        current_amount=Decimal("0"),
        unit_cost_snapshot=Decimal("0"),
        action_quantity=Decimal("1"),
    )
    assert result.action_amount == Decimal("0.00")
    assert result.amount_after == Decimal("0.00")
    with pytest.raises(ValidationError, match="大于 0"):
        allocate_custody_amount(
            current_quantity=Decimal("2"),
            current_amount=Decimal("10"),
            unit_cost_snapshot=Decimal("5"),
            action_quantity=Decimal("0"),
        )
    with pytest.raises(ValidationError, match="超过当前保管"):
        allocate_custody_amount(
            current_quantity=Decimal("2"),
            current_amount=Decimal("10"),
            unit_cost_snapshot=Decimal("5"),
            action_quantity=Decimal("3"),
        )
