from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.supplies.models import SupplyCustody, SupplyDocument, SupplyDocumentLine
from apps.supplies.reconciliation import (
    rebuild_custodies,
    rebuild_stock_balances,
    reconcile_custodies,
    reconcile_stock_balances,
)
from apps.supplies.services import (
    _create_custody_movement,
    _create_stock_ledger,
    _update_balance_values,
    post_supply_document,
    reverse_supply_document,
)
from tests.test_sprint15_support import (
    make_company,
    make_department,
    make_employee,
    make_issue_document,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _stock_context(code):
    company = make_company(code)
    actor = make_user(f"{code.lower()}-finance", "finance")
    category = make_supply_category(company, f"{code}-CAT")
    warehouse = make_supply_warehouse(company, f"{code}-WH")
    item = make_supply_item(company, category, f"{code}-ITEM")
    opening = seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="5",
        unit_cost="10",
        key=f"{code}-opening",
    )
    return company, actor, warehouse, item, opening


def _posted_receipt_line(*, company, actor, warehouse, item, key, quantity="2"):
    posted_at = timezone.now()
    document = SupplyDocument(
        company=company,
        document_no=f"MANUAL-{key}",
        document_type="receipt",
        business_date=date(2026, 8, 27),
        target_warehouse=warehouse,
        status="posted",
        idempotency_key=key,
        created_by=actor,
        posted_by=actor,
        posted_at=posted_at,
    )
    document._controlled_transition = True
    document.full_clean()
    document.save(force_insert=True)
    line = SupplyDocumentLine(
        company=company,
        document=document,
        line_no=1,
        item=item,
        quantity=Decimal(quantity),
        entered_unit_cost=Decimal("10"),
        posted_unit_cost=Decimal("10"),
        posted_amount=Decimal(quantity) * Decimal("10"),
    )
    line._controlled_posting = True
    line.full_clean()
    line.save(force_insert=True)
    return document, line


def test_posted_line_without_ledger_is_integrity_error_and_blocks_rebuild():
    company, actor, warehouse, item, _ = _stock_context("RI1")
    with transaction.atomic():
        _posted_receipt_line(
            company=company,
            actor=actor,
            warehouse=warehouse,
            item=item,
            key="ri1-missing-ledger",
        )
        result = reconcile_stock_balances(company=company)
        assert not result.is_consistent
        assert any(
            "已正式过账但没有库存流水" in value
            for value in result.integrity_errors
        )
        assert any(
            "应有 1 条库存流水，实际 0 条" in value
            for value in result.integrity_errors
        )
        with pytest.raises(ValidationError, match="已正式过账但没有库存流水"):
            rebuild_stock_balances(
                company=company,
                actor=actor,
                reason="缺流水不得重建掩盖",
                confirm=True,
            )
        # PostgreSQL commit-level guards also reject this corruption.  Keep it
        # uncommitted so the read-only reconciliation defence is tested on
        # every backend without weakening the database guard.
        transaction.set_rollback(True)


def test_net_sum_cannot_hide_broken_before_after_chain():
    company, actor, warehouse, item, _ = _stock_context("RI2")
    balance = item.stock_balances.get(warehouse=warehouse)
    with transaction.atomic():
        document, line = _posted_receipt_line(
            company=company,
            actor=actor,
            warehouse=warehouse,
            item=item,
            key="ri2-broken-chain",
        )
        _create_stock_ledger(
            values={
                "company": company,
                "warehouse": warehouse,
                "item": item,
                "document": document,
                "document_line": line,
                "movement_type": "receipt_in",
                "quantity_delta": Decimal("2.0000"),
                "amount_delta": Decimal("20.00"),
                "unit_cost": Decimal("10.000000"),
                "quantity_before": Decimal("99.0000"),
                "quantity_after": Decimal("101.0000"),
                "amount_before": Decimal("990.00"),
                "amount_after": Decimal("1010.00"),
                "average_unit_cost_before": Decimal("10.000000"),
                "average_unit_cost_after": Decimal("10.000000"),
                "occurred_at": document.posted_at,
                "created_by": actor,
            }
        )
        _update_balance_values(
            balance=balance,
            quantity=Decimal("7.0000"),
            amount=Decimal("70.00"),
            average_unit_cost=Decimal("10.000000"),
            updated_at=timezone.now(),
        )

    result = reconcile_stock_balances(company=company)
    assert not result.differences
    assert any("流水链断裂" in value for value in result.integrity_errors)


def test_wrong_ledger_average_snapshot_is_detected_even_when_totals_match():
    company, actor, warehouse, item, _ = _stock_context("RI3")
    balance = item.stock_balances.get(warehouse=warehouse)
    with transaction.atomic():
        document, line = _posted_receipt_line(
            company=company,
            actor=actor,
            warehouse=warehouse,
            item=item,
            key="ri3-wrong-average",
        )
        _create_stock_ledger(
            values={
                "company": company,
                "warehouse": warehouse,
                "item": item,
                "document": document,
                "document_line": line,
                "movement_type": "receipt_in",
                "quantity_delta": Decimal("2.0000"),
                "amount_delta": Decimal("20.00"),
                "unit_cost": Decimal("10.000000"),
                "quantity_before": Decimal("5.0000"),
                "quantity_after": Decimal("7.0000"),
                "amount_before": Decimal("50.00"),
                "amount_after": Decimal("70.00"),
                "average_unit_cost_before": Decimal("999.000000"),
                "average_unit_cost_after": Decimal("999.000000"),
                "occurred_at": document.posted_at,
                "created_by": actor,
            }
        )
        _update_balance_values(
            balance=balance,
            quantity=Decimal("7.0000"),
            amount=Decimal("70.00"),
            average_unit_cost=Decimal("10.000000"),
            updated_at=timezone.now(),
        )
        result = reconcile_stock_balances(company=company)
        assert any("变动前平均成本" in value for value in result.integrity_errors)
        assert any("变动后平均成本" in value for value in result.integrity_errors)
        transaction.set_rollback(True)


def _durable_custody_context(code):
    company = make_company(code)
    actor = make_user(f"{code.lower()}-finance", "finance")
    department = make_department(company, f"{code}-D")
    category = make_supply_category(company, f"{code}-CAT")
    warehouse = make_supply_warehouse(company, f"{code}-WH")
    item = make_supply_item(
        company,
        category,
        f"{code}-DUR",
        item_type="durable_quantity",
    )
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="2",
        unit_cost="80",
        key=f"{code}-opening",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        department=department,
        quantity="1",
        key=f"{code}-issue",
    )
    post_supply_document(document=issue, actor=actor)
    return company, actor, SupplyCustody.objects.get(origin_issue_line__document=issue)


def test_duplicate_custody_entrance_is_not_hidden_by_offsetting_movement():
    company, actor, custody = _durable_custody_context("RI4")
    line = custody.origin_issue_line
    with transaction.atomic():
        _create_custody_movement(
            values={
                "company": company,
                "item": custody.item,
                "from_custody": None,
                "to_custody": custody,
                "action": "issue",
                "quantity": Decimal("1.0000"),
                "amount": Decimal("80.00"),
                "unit_cost": Decimal("80.000000"),
                "business_date": line.document.business_date,
                "source_document_line": line,
                "created_by": actor,
                "idempotency_key": "ri4-duplicate-entrance",
            }
        )
        _create_custody_movement(
            values={
                "company": company,
                "item": custody.item,
                "from_custody": custody,
                "to_custody": None,
                "action": "correction",
                "quantity": Decimal("1.0000"),
                "amount": Decimal("80.00"),
                "unit_cost": Decimal("80.000000"),
                "business_date": line.document.business_date,
                "reason": "抵销伪造入口",
                "created_by": actor,
                "idempotency_key": "ri4-offset-entry",
            }
        )

    result = reconcile_custodies(company=company)
    assert not result.differences
    assert any("只能有一条" in value for value in result.integrity_errors)
    with pytest.raises(ValidationError, match="只能有一条"):
        rebuild_custodies(
            company=company,
            actor=actor,
            reason="重复入口不得重建掩盖",
            confirm=True,
        )


def test_custody_intermediate_negative_is_detected_when_final_net_recovers():
    company, actor, custody = _durable_custody_context("RI5")
    with transaction.atomic():
        for direction, key in (("out", "ri5-negative"), ("in", "ri5-restore")):
            _create_custody_movement(
                values={
                    "company": company,
                    "item": custody.item,
                    "from_custody": custody if direction == "out" else None,
                    "to_custody": custody if direction == "in" else None,
                    "action": "correction",
                    "quantity": Decimal("2.0000"),
                    "amount": Decimal("160.00"),
                    "unit_cost": Decimal("80.000000"),
                    "business_date": date(2026, 8, 27),
                    "reason": "构造中途负数后恢复",
                    "created_by": actor,
                    "idempotency_key": key,
                }
            )

    result = reconcile_custodies(company=company)
    assert not result.differences
    assert any("在该时点出现负数" in value for value in result.integrity_errors)


def test_full_stock_and_custody_reversal_chains_reconcile():
    company, actor, custody = _durable_custody_context("RI7")
    issue = custody.origin_issue_line.document
    reverse_supply_document(
        document=issue,
        actor=actor,
        idempotency_key="ri7-reversal",
        reason="验证完整反向链",
    )

    assert reconcile_stock_balances(company=company).is_consistent
    assert reconcile_custodies(company=company).is_consistent


def test_rebuild_service_enforces_application_role_active_company_and_employee_scope():
    company, actor, _, _, _ = _stock_context("RI6")
    assert rebuild_stock_balances(
        company=company,
        actor=actor,
        reason="无员工链接的应用财务可执行 dry-run",
        confirm=False,
    ).is_consistent

    other = make_company("RI6O", active=False)
    other_department = make_department(other, "RI6O-D")
    make_employee(other, other_department, "RI6O-E", user=actor)
    with pytest.raises(ValidationError, match="员工关系不属于指定公司"):
        rebuild_stock_balances(
            company=company,
            actor=actor,
            reason="跨公司应拒绝",
            confirm=False,
        )

    actor.is_active = False
    actor.save(update_fields=("is_active",))
    with pytest.raises(ValidationError, match="启用的 application"):
        rebuild_stock_balances(
            company=company,
            actor=actor,
            reason="停用用户应拒绝",
            confirm=False,
        )
