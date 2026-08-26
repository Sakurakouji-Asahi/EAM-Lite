from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command

from apps.audit.models import AuditLog
from apps.supplies.models import (
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyDocument,
    SupplyStockBalance,
    SupplyStockLedger,
)
from apps.supplies.services import (
    create_supply_document,
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


pytestmark = pytest.mark.django_db


def supply_context():
    company = make_company()
    actor = make_user("s15-warehouse", "warehouse")
    department = make_department(company, "USE")
    employee = make_employee(company, department, "EMP")
    category = make_supply_category(company)
    source = make_supply_warehouse(company, "SOURCE")
    target = make_supply_warehouse(company, "TARGET")
    consumable = make_supply_item(company, category, "PAPER")
    durable = make_supply_item(
        company,
        category,
        "CHAIR",
        item_type="durable_quantity",
        unit="把",
    )
    return company, actor, department, employee, source, target, consumable, durable


def make_return(*, actor, company, target, source_line, quantity, key, reason="未使用退回"):
    return create_supply_document(
        actor=actor,
        company=company,
        document_type="return",
        data={
            "business_date": date(2026, 8, 26),
            "target_warehouse": target,
            "idempotency_key": key,
            "remark": reason,
        },
        lines=[
            {
                "item": source_line.item,
                "quantity": Decimal(quantity),
                "entered_unit_cost": None,
                "source_issue_line": source_line,
                "line_remark": reason,
            }
        ],
    )


def make_transfer(*, actor, company, source, target, item, quantity, key):
    return create_supply_document(
        actor=actor,
        company=company,
        document_type="transfer",
        data={
            "business_date": date(2026, 8, 26),
            "source_warehouse": source,
            "target_warehouse": target,
            "idempotency_key": key,
        },
        lines=[
            {
                "item": item,
                "quantity": Decimal(quantity),
                "entered_unit_cost": None,
            }
        ],
    )


def test_issue_draft_is_neutral_partial_cost_and_consumable_has_no_custody():
    company, actor, department, employee, source, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="20",
        unit_cost="110",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        employee=employee,
        quantity="5",
    )
    balance = SupplyStockBalance.objects.get(warehouse=source, item=item)
    assert balance.quantity_on_hand == Decimal("20.0000")
    assert not issue.stock_ledgers.exists()

    post_supply_document(document=issue, actor=actor)
    balance.refresh_from_db()
    line = issue.lines.get()
    assert balance.quantity_on_hand == Decimal("15.0000")
    assert balance.amount_on_hand == Decimal("1650.00")
    assert line.posted_unit_cost == Decimal("110.000000")
    assert line.posted_amount == Decimal("550.00")
    assert issue.stock_ledgers.get().movement_type == "issue_out"
    assert not SupplyCustody.objects.exists()
    assert not SupplyCustodyMovement.objects.exists()


def test_full_issue_clears_quantity_amount_and_average_without_tail():
    company, actor, department, _, source, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="3",
        unit_cost="3.336667",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="3",
    )
    post_supply_document(document=issue, actor=actor)
    balance = SupplyStockBalance.objects.get(warehouse=source, item=item)
    assert balance.quantity_on_hand == Decimal("0.0000")
    assert balance.amount_on_hand == Decimal("0.00")
    assert balance.average_unit_cost == Decimal("0.000000")
    assert issue.lines.get().posted_amount == Decimal("10.01")


def test_issue_shortage_rolls_back_and_employee_rules_are_enforced():
    company, actor, department, employee, source, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor, company=company, warehouse=source, item=item, quantity="1"
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="2",
    )
    with pytest.raises(ValidationError, match="库存不足"):
        post_supply_document(document=issue, actor=actor)
    issue.refresh_from_db()
    assert issue.status == "draft"
    assert SupplyStockBalance.objects.get().quantity_on_hand == Decimal("1.0000")
    assert not issue.stock_ledgers.exists()

    other_department = make_department(company, "OTHER")
    with pytest.raises(ValidationError, match="不属于所选部门"):
        make_issue_document(
            actor=actor,
            company=company,
            warehouse=source,
            item=item,
            department=other_department,
            employee=employee,
            key="bad-department",
        )
    inactive = make_employee(company, department, "INACTIVE", is_active=False)
    with pytest.raises(ValidationError, match="在职"):
        make_issue_document(
            actor=actor,
            company=company,
            warehouse=source,
            item=item,
            department=department,
            employee=inactive,
            key="bad-employee",
        )


def test_durable_issue_creates_exact_custody_and_preserves_managed_amount():
    company, actor, department, employee, source, _, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity="20",
        unit_cost="80",
    )
    before = SupplyStockBalance.objects.get(warehouse=source, item=durable).amount_on_hand
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity="3",
        key="durable-issue",
    )
    post_supply_document(document=issue, actor=actor)
    balance = SupplyStockBalance.objects.get(warehouse=source, item=durable)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    movement = SupplyCustodyMovement.objects.get(to_custody=custody)
    assert custody.current_quantity == Decimal("3.0000")
    assert custody.current_amount == Decimal("240.00")
    assert custody.status == "open"
    assert movement.action == "issue"
    assert movement.amount == issue.lines.get().posted_amount
    assert balance.amount_on_hand + custody.current_amount == before
    post_supply_document(document=issue, actor=actor)
    assert SupplyCustody.objects.count() == 1
    assert SupplyCustodyMovement.objects.count() == 1
    assert AuditLog.objects.filter(action="supply_custody_create").exists()


def test_consumable_partial_return_uses_original_cost_and_caps_cumulative_quantity():
    company, actor, department, _, source, target, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="10",
        unit_cost="110",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="5",
    )
    post_supply_document(document=issue, actor=actor)
    source_line = issue.lines.get()
    returned = make_return(
        actor=actor,
        company=company,
        target=target,
        source_line=source_line,
        quantity="2",
        key="return-1",
    )
    post_supply_document(document=returned, actor=actor)
    line = returned.lines.get()
    assert line.posted_unit_cost == Decimal("110.000000")
    assert line.posted_amount == Decimal("220.00")
    assert SupplyStockBalance.objects.get(warehouse=target, item=item).amount_on_hand == Decimal("220.00")

    excessive = make_return(
        actor=actor,
        company=company,
        target=target,
        source_line=source_line,
        quantity="4",
        key="return-too-much",
    )
    with pytest.raises(ValidationError, match="超过原领用未退"):
        post_supply_document(document=excessive, actor=actor)
    assert SupplyStockBalance.objects.get(warehouse=target, item=item).amount_on_hand == Decimal("220.00")


def test_final_consumable_return_uses_remaining_original_amount_without_tail():
    company, actor, department, _, source, target, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="3",
        unit_cost="3.336667",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="3",
        key="tail-issue",
    )
    post_supply_document(document=issue, actor=actor)
    source_line = issue.lines.get()
    first = make_return(
        actor=actor,
        company=company,
        target=target,
        source_line=source_line,
        quantity="1",
        key="tail-return-1",
    )
    post_supply_document(document=first, actor=actor)
    final = make_return(
        actor=actor,
        company=company,
        target=target,
        source_line=source_line,
        quantity="2",
        key="tail-return-2",
    )
    post_supply_document(document=final, actor=actor)
    assert first.lines.get().posted_amount == Decimal("3.34")
    assert final.lines.get().posted_amount == Decimal("6.67")
    assert (
        first.lines.get().posted_amount + final.lines.get().posted_amount
        == source_line.posted_amount
        == Decimal("10.01")
    )

def test_durable_return_without_custody_source_is_rejected_by_backend():
    company, actor, department, _, source, target, _, durable = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=durable)
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
    )
    post_supply_document(document=issue, actor=actor)
    with pytest.raises(ValidationError, match="有效的易耗品领用明细"):
        make_return(
            actor=actor,
            company=company,
            target=target,
            source_line=issue.lines.get(),
            quantity="1",
            key="durable-return",
        )


def test_transfer_writes_two_equal_ledgers_and_updates_both_balances():
    company, actor, _, _, source, target, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="10",
        unit_cost="3.336667",
    )
    transfer = make_transfer(
        actor=actor,
        company=company,
        source=source,
        target=target,
        item=item,
        quantity="4",
        key="transfer",
    )
    post_supply_document(document=transfer, actor=actor)
    ledgers = list(transfer.stock_ledgers.order_by("movement_type"))
    assert {ledger.movement_type for ledger in ledgers} == {"transfer_in", "transfer_out"}
    assert abs(ledgers[0].amount_delta) == abs(ledgers[1].amount_delta)
    assert transfer.lines.get().stock_ledgers.count() == 2
    assert SupplyStockBalance.objects.get(warehouse=source, item=item).quantity_on_hand == Decimal("6.0000")
    assert SupplyStockBalance.objects.get(warehouse=target, item=item).quantity_on_hand == Decimal("4.0000")
    assert not SupplyCustody.objects.exists()


def test_transfer_same_warehouse_and_shortage_are_atomic():
    company, actor, _, _, source, target, item, _ = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item, quantity="1")
    with pytest.raises(ValidationError, match="不能相同"):
        make_transfer(
            actor=actor,
            company=company,
            source=source,
            target=source,
            item=item,
            quantity="1",
            key="same-wh",
        )
    transfer = make_transfer(
        actor=actor,
        company=company,
        source=source,
        target=target,
        item=item,
        quantity="2",
        key="short-transfer",
    )
    with pytest.raises(ValidationError, match="库存不足"):
        post_supply_document(document=transfer, actor=actor)
    assert SupplyStockBalance.objects.get(warehouse=source, item=item).quantity_on_hand == Decimal("1.0000")
    assert not SupplyStockBalance.objects.filter(warehouse=target, item=item).exists()
    assert not transfer.stock_ledgers.exists()


def test_reverse_receipt_restores_snapshot_and_is_idempotent():
    company, actor, _, _, source, _, item, _ = supply_context()
    opening = seed_supply_stock(
        actor=actor, company=company, warehouse=source, item=item, quantity="3", key="reverse-opening"
    )
    reversal = reverse_supply_document(
        document=opening,
        actor=actor,
        idempotency_key="reverse-opening-key",
        reason="期初录入错误",
    )
    opening.refresh_from_db()
    balance = SupplyStockBalance.objects.get(warehouse=source, item=item)
    assert opening.status == "reversed"
    assert reversal.document_type == "reversal"
    assert reversal.document_no == "CX-2026-000001"
    assert balance.quantity_on_hand == Decimal("0.0000")
    assert balance.amount_on_hand == Decimal("0.00")
    assert reversal.stock_ledgers.get().reverses_ledger.document_id == opening.pk
    again = reverse_supply_document(
        document=opening,
        actor=actor,
        idempotency_key="another-key",
        reason="重复点击",
    )
    assert again.pk == reversal.pk
    assert SupplyDocument.objects.filter(document_type="reversal").count() == 1


def test_reverse_issue_rejects_active_return_and_reversed_return_no_longer_counts():
    company, actor, department, _, source, target, item, _ = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item, quantity="5")
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="2",
    )
    post_supply_document(document=issue, actor=actor)
    returned = make_return(
        actor=actor,
        company=company,
        target=target,
        source_line=issue.lines.get(),
        quantity="1",
        key="return-before-issue-reverse",
    )
    post_supply_document(document=returned, actor=actor)
    with pytest.raises(ValidationError, match="已经发生退回"):
        reverse_supply_document(
            document=issue,
            actor=actor,
            idempotency_key="reject-issue-reverse",
            reason="错误领用",
        )
    reverse_supply_document(
        document=returned,
        actor=actor,
        idempotency_key="reverse-return",
        reason="退回错误",
    )
    assert not issue.lines.get().return_lines.filter(
        document__document_type="return", document__status="posted"
    ).exists()


def test_reverse_transfer_restores_two_warehouses_and_durable_issue_closes_custody():
    company, actor, department, employee, source, target, item, durable = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item, quantity="5", key="seed-transfer-reverse")
    transfer = make_transfer(
        actor=actor,
        company=company,
        source=source,
        target=target,
        item=item,
        quantity="2",
        key="transfer-reverse",
    )
    post_supply_document(document=transfer, actor=actor)
    reversal = reverse_supply_document(
        document=transfer,
        actor=actor,
        idempotency_key="reverse-transfer",
        reason="调拨仓库错误",
    )
    assert reversal.stock_ledgers.count() == 2
    assert SupplyStockBalance.objects.get(warehouse=source, item=item).quantity_on_hand == Decimal("5.0000")
    assert SupplyStockBalance.objects.get(warehouse=target, item=item).quantity_on_hand == Decimal("0.0000")

    seed_supply_stock(actor=actor, company=company, warehouse=source, item=durable, quantity="2", unit_cost="80", key="seed-durable-reverse")
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity="2",
        key="durable-reverse",
    )
    post_supply_document(document=issue, actor=actor)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    reverse_supply_document(
        document=issue,
        actor=actor,
        idempotency_key="reverse-durable-issue",
        reason="领用错误",
    )
    custody.refresh_from_db()
    assert custody.status == "closed"
    assert custody.current_quantity == Decimal("0.0000")
    assert custody.current_amount == Decimal("0.00")
    assert custody.outgoing_movements.get().action == "reversal"


def test_reverse_same_item_multiple_lines_restores_snapshots_in_reverse_line_order():
    company, actor, department, _, source, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="10",
        unit_cost="3.336667",
    )
    issue = create_supply_document(
        actor=actor,
        company=company,
        document_type="issue",
        data={
            "business_date": date(2026, 8, 26),
            "source_warehouse": source,
            "department": department,
            "idempotency_key": "multi-line-issue",
        },
        lines=[
            {"item": item, "quantity": Decimal("2"), "entered_unit_cost": None},
            {"item": item, "quantity": Decimal("3"), "entered_unit_cost": None},
        ],
    )
    post_supply_document(document=issue, actor=actor)
    original = list(issue.stock_ledgers.order_by("document_line__line_no"))
    assert original[1].amount_before == original[0].amount_after
    reversal = reverse_supply_document(
        document=issue,
        actor=actor,
        idempotency_key="reverse-multi-line",
        reason="多行领用错误",
    )
    reversed_ledgers = list(reversal.stock_ledgers.order_by("occurred_at"))
    assert len(reversed_ledgers) == 2
    balance = SupplyStockBalance.objects.get(warehouse=source, item=item)
    assert balance.quantity_on_hand == Decimal("10.0000")
    assert balance.amount_on_hand == Decimal("33.37")


def test_reversal_failure_rolls_back_document_balances_ledgers_and_audit(monkeypatch):
    company, actor, _, _, source, target, item, _ = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item)
    transfer = make_transfer(
        actor=actor,
        company=company,
        source=source,
        target=target,
        item=item,
        quantity="2",
        key="rollback-reversal-transfer",
    )
    post_supply_document(document=transfer, actor=actor)
    before = {
        balance.warehouse_id: (balance.quantity_on_hand, balance.amount_on_hand)
        for balance in SupplyStockBalance.objects.filter(item=item)
    }
    from apps.supplies import services

    original_create = services._create_stock_ledger
    calls = {"count": 0}

    def fail_second(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated reversal ledger failure")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(services, "_create_stock_ledger", fail_second)
    with pytest.raises(RuntimeError, match="simulated reversal"):
        reverse_supply_document(
            document=transfer,
            actor=actor,
            idempotency_key="rollback-reversal",
            reason="故障注入",
        )
    transfer.refresh_from_db()
    assert transfer.status == "posted"
    assert not SupplyDocument.objects.filter(idempotency_key="rollback-reversal").exists()
    assert not SupplyStockLedger.objects.filter(movement_type="reversal").exists()
    assert {
        balance.warehouse_id: (balance.quantity_on_hand, balance.amount_on_hand)
        for balance in SupplyStockBalance.objects.filter(item=item)
    } == before
    assert not AuditLog.objects.filter(action="supply_document_reverse").exists()


def test_reverse_rejects_subsequent_stock_business_and_readonly_role():
    company, actor, _, _, source, _, item, _ = supply_context()
    first = seed_supply_stock(actor=actor, company=company, warehouse=source, item=item, quantity="1", key="first")
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item, quantity="1", key="second")
    with pytest.raises(ValidationError, match="之后已经发生库存业务"):
        reverse_supply_document(
            document=first,
            actor=actor,
            idempotency_key="reverse-old",
            reason="旧单错误",
        )
    management = make_user("s15-management-service", "management")
    with pytest.raises(PermissionDenied):
        reverse_supply_document(
            document=first,
            actor=management,
            idempotency_key="readonly-reverse",
            reason="无权",
        )


def test_custody_reconciliation_is_read_only_and_reports_success():
    company, actor, department, _, source, _, _, durable = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=durable)
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
    )
    post_supply_document(document=issue, actor=actor)
    output = StringIO()
    call_command("reconcile_supply_custodies", company=company.code, stdout=output)
    assert "一致" in output.getvalue()


def test_user_cannot_override_issue_cost_even_by_direct_service_payload():
    company, actor, department, _, source, _, item, _ = supply_context()
    with pytest.raises(ValidationError, match="用户不得录入"):
        create_supply_document(
            actor=actor,
            company=company,
            document_type="issue",
            data={
                "business_date": date(2026, 8, 26),
                "source_warehouse": source,
                "department": department,
                "idempotency_key": "cost-override",
            },
            lines=[
                {
                    "item": item,
                    "quantity": Decimal("1"),
                    "entered_unit_cost": Decimal("0"),
                }
            ],
        )
