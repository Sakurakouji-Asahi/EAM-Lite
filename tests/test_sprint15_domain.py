from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.supplies.domain import calculate_issue, calculate_receipt_from_amount


def test_partial_issue_uses_current_average_and_full_issue_clears_tail():
    partial = calculate_issue("5", "10.01", "2")
    assert partial.issue_unit_cost == Decimal("2.002000")
    assert partial.issue_amount == Decimal("4.00")
    assert partial.quantity_after == Decimal("3.0000")
    assert partial.amount_after == Decimal("6.01")

    final = calculate_issue(
        partial.quantity_after, partial.amount_after, partial.quantity_after
    )
    assert final.issue_amount == Decimal("6.01")
    assert final.quantity_after == Decimal("0.0000")
    assert final.amount_after == Decimal("0.00")
    assert final.average_unit_cost_after == Decimal("0.000000")


def test_issue_rejects_shortage_and_exact_amount_receipt_does_not_requantify():
    with pytest.raises(ValidationError, match="库存不足"):
        calculate_issue("1", "10", "1.0001")
    receipt = calculate_receipt_from_amount("1", "1.00", "3", "10.01")
    assert receipt.receipt_amount == Decimal("10.01")
    assert receipt.amount_after == Decimal("11.01")
    assert receipt.quantity_after == Decimal("4.0000")
