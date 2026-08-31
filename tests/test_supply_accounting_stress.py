from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import pytest

from apps.supplies.models import SupplyStockBalance, SupplyStockLedger
from apps.supplies.reconciliation import reconcile_stock_balances
from apps.supplies.services import create_supply_document, post_supply_document
from tests.test_sprint15_support import (
    make_company,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
)


pytestmark = pytest.mark.django_db

LARGE_DOCUMENT_LINE_COUNT = 1_200
MONEY_QUANT = Decimal("0.01")


def test_large_multiline_posting_keeps_all_1200_lines_ledgers_and_balance_in_sync():
    company = make_company("INV-STRESS")
    actor = make_user("inventory-stress-warehouse", "warehouse")
    category = make_supply_category(company, "OFFICE")
    warehouse = make_supply_warehouse(company, "MAIN")
    item = make_supply_item(company, category, "PAPER")
    lines = []
    expected_amount = Decimal("0.00")
    for index in range(LARGE_DOCUMENT_LINE_COUNT):
        unit_cost = Decimal("1.000000") + Decimal(index % 37).scaleb(-3)
        line_amount = (Decimal("1.0000") * unit_cost).quantize(
            MONEY_QUANT,
            rounding=ROUND_HALF_UP,
        )
        expected_amount += line_amount
        lines.append(
            {
                "item": item,
                "quantity": Decimal("1.0000"),
                "entered_unit_cost": unit_cost,
                "line_remark": "",
            }
        )

    document = create_supply_document(
        actor=actor,
        company=company,
        document_type="receipt",
        data={
            "business_date": date(2026, 8, 31),
            "target_warehouse": warehouse,
            "idempotency_key": "inventory-stress-1200-lines",
        },
        lines=lines,
    )
    post_supply_document(
        document=document,
        actor=actor,
        idempotency_key=document.idempotency_key,
    )

    document.refresh_from_db()
    balance = SupplyStockBalance.objects.get(
        company=company,
        warehouse=warehouse,
        item=item,
    )
    assert document.status == "posted"
    assert document.lines.count() == LARGE_DOCUMENT_LINE_COUNT
    assert document.stock_ledgers.count() == LARGE_DOCUMENT_LINE_COUNT
    assert SupplyStockLedger.objects.filter(company=company).count() == LARGE_DOCUMENT_LINE_COUNT
    assert balance.quantity_on_hand == Decimal("1200.0000")
    assert balance.amount_on_hand == expected_amount
    assert all(
        value is not None
        for value in document.lines.values_list("posted_amount", flat=True).iterator()
    )
    result = reconcile_stock_balances(company=company)
    assert result.checked_count == 1
    assert result.is_consistent
