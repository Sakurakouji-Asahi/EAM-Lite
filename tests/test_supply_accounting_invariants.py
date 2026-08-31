from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.supplies.domain import (
    ZERO_MONEY,
    allocate_custody_amount,
    calculate_average_unit_cost,
    calculate_receipt,
    validate_zero_cost_reason,
)
from apps.supplies.models import SupplyCustody, SupplyDocument, SupplyStockBalance
from apps.supplies.services import (
    _update_balance_values,
    close_supply_count_task,
    deactivate_supply_item,
    deactivate_supply_warehouse,
    post_supply_document,
    publish_supply_count_task,
    record_supply_count,
    return_custody_for_count,
    return_custody_to_warehouse,
    reverse_supply_document,
    stop_supply_count_entry,
    update_supply_item,
)
from tests.test_sprint15_services import make_return, supply_context
from tests.test_sprint15_support import make_issue_document, seed_supply_stock
from tests.test_sprint17_services import make_count


pytestmark = pytest.mark.django_db


def test_sub_precision_negative_values_are_rejected_before_rounding_to_zero():
    with pytest.raises(ValidationError, match="不得小于 0"):
        validate_zero_cost_reason(Decimal("-0.0000004"), "异常负成本")
    with pytest.raises(ValidationError, match="不得小于 0"):
        calculate_average_unit_cost(Decimal("1"), Decimal("-0.004"))
    with pytest.raises(ValidationError, match="不得小于 0"):
        calculate_receipt(
            Decimal("-0.00001"),
            Decimal("0"),
            Decimal("1"),
            Decimal("1"),
        )


def test_balance_update_rejects_an_average_cost_that_does_not_reconcile():
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="2",
        unit_cost="1.50",
        key="invalid-average-opening",
    )
    balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=item)
    with pytest.raises(ValidationError, match="移动平均成本与数量、金额不勾稽"):
        _update_balance_values(
            balance=balance,
            quantity=Decimal("2.0000"),
            amount=Decimal("3.00"),
            average_unit_cost=Decimal("9.999999"),
            updated_at=balance.updated_at,
        )
    balance.refresh_from_db()
    assert balance.average_unit_cost == Decimal("1.500000")


def test_consumable_split_returns_never_exceed_original_rounded_issue_amount():
    company, actor, department, _, source, target, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="5",
        unit_cost="0.006000",
        key="split-return-opening",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="5",
        key="split-return-issue",
    )
    post_supply_document(document=issue, actor=actor)
    source_line = issue.lines.get()

    amounts = []
    for index in range(5):
        returned = make_return(
            actor=actor,
            company=company,
            target=target,
            source_line=source_line,
            quantity="1",
            key=f"split-return-{index}",
        )
        post_supply_document(document=returned, actor=actor)
        amounts.append(returned.lines.get().posted_amount)

    assert amounts == [
        Decimal("0.01"),
        Decimal("0.00"),
        Decimal("0.01"),
        Decimal("0.00"),
        Decimal("0.01"),
    ]
    assert sum(amounts, ZERO_MONEY) == source_line.posted_amount == Decimal("0.03")
    balance = SupplyStockBalance.objects.get(warehouse=target, item=item)
    assert balance.quantity_on_hand == Decimal("5.0000")
    assert balance.amount_on_hand == Decimal("0.03")


def test_repeated_partial_custody_actions_cap_each_amount_and_clear_final_tail():
    quantity = Decimal("5.0000")
    amount = Decimal("0.03")
    allocated = []
    for _ in range(5):
        result = allocate_custody_amount(
            current_quantity=quantity,
            current_amount=amount,
            unit_cost_snapshot=Decimal("0.006000"),
            action_quantity=Decimal("1.0000"),
        )
        assert ZERO_MONEY <= result.action_amount <= amount
        allocated.append(result.action_amount)
        quantity = result.quantity_after
        amount = result.amount_after
    assert sum(allocated, ZERO_MONEY) == Decimal("0.03")
    assert quantity == Decimal("0.0000")
    assert amount == ZERO_MONEY


def test_live_stock_open_custody_and_default_references_block_deactivation():
    company, actor, department, employee, source, target, item, durable = supply_context()
    item.default_warehouse = source
    item.save(update_fields=("default_warehouse", "updated_at"))
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="1",
        key="deactivate-stock",
    )
    with pytest.raises(ValidationError, match="非零仓库库存"):
        deactivate_supply_item(actor=actor, item=item, reason="错误停用")
    with pytest.raises(ValidationError, match="非零库存"):
        deactivate_supply_warehouse(actor=actor, warehouse=source, reason="错误停用")

    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="1",
        key="deactivate-stock-issue",
    )
    post_supply_document(document=issue, actor=actor)
    with pytest.raises(ValidationError, match="默认仓库"):
        deactivate_supply_warehouse(actor=actor, warehouse=source, reason="仍被引用")
    update_supply_item(actor=actor, item=item, data={"default_warehouse": None})
    deactivate_supply_item(actor=actor, item=item, reason="库存已结清")
    deactivate_supply_warehouse(actor=actor, warehouse=source, reason="库存已结清")

    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=target,
        item=durable,
        quantity="1",
        unit_cost="80",
        key="deactivate-custody-stock",
    )
    durable_issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=target,
        item=durable,
        department=department,
        employee=employee,
        quantity="1",
        key="deactivate-custody-issue",
    )
    post_supply_document(document=durable_issue, actor=actor)
    with pytest.raises(ValidationError, match="未结清保管"):
        deactivate_supply_item(actor=actor, item=durable, reason="仍在保管")


def test_closed_custody_count_resolution_cannot_be_invalidated_by_return_reversal():
    company, _, department, employee, source, target, _, durable = supply_context()
    # The count workflow is owned by equipment; stock issue/post remains a
    # warehouse action.  Use a separate application user to prove the audit
    # chain is independent of one actor account.
    from tests.test_sprint15_support import make_user

    warehouse_actor = make_user("invariant-count-warehouse", "warehouse")
    equipment_actor = make_user("invariant-count-equipment", "equipment")
    seed_supply_stock(
        actor=warehouse_actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity="3",
        unit_cost="80",
        key="count-reversal-opening",
    )
    issue = make_issue_document(
        actor=warehouse_actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity="3",
        key="count-reversal-issue",
    )
    post_supply_document(document=issue, actor=warehouse_actor)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    task = make_count(
        actor=equipment_actor,
        company=company,
        domain="custody",
        department=department,
        employee=employee,
        key="count-reversal-task",
    )
    publish_supply_count_task(task=task, actor=equipment_actor)
    line = task.lines.get(custody=custody)
    record_supply_count(
        line=line,
        counted_quantity=Decimal("2"),
        remark="盘亏一把并归还仓库",
        actor=equipment_actor,
    )
    stop_supply_count_entry(task=task, actor=equipment_actor)
    movement = return_custody_for_count(
        count_line=line,
        target_warehouse=target,
        business_date=date(2026, 8, 27),
        reason="盘点差异归还",
        actor=equipment_actor,
        idempotency_key="count-reversal-return",
    )
    close_supply_count_task(task=task, actor=equipment_actor)
    return_document = movement.source_document_line.document

    with pytest.raises(ValidationError, match="保管盘点解决证据"):
        reverse_supply_document(
            document=return_document,
            actor=warehouse_actor,
            idempotency_key="count-resolution-reversal",
            reason="不得破坏已关闭盘点",
        )
    return_document.refresh_from_db()
    custody.refresh_from_db()
    assert return_document.status == "posted"
    assert custody.current_quantity == Decimal("2.0000")
    assert not SupplyDocument.objects.filter(
        reversal_of=return_document,
    ).exists()


def test_durable_tiny_cost_can_be_returned_in_many_parts_without_stalling():
    company, actor, department, employee, source, target, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity="5",
        unit_cost="0.006000",
        key="durable-tail-opening",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity="5",
        key="durable-tail-issue",
    )
    post_supply_document(document=issue, actor=actor)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    returned_amounts = []
    for index in range(5):
        draft = return_custody_to_warehouse(
            custody=custody,
            target_warehouse=target,
            quantity=Decimal("1"),
            business_date=date(2026, 8, 27),
            reason="分次归还尾差验证",
            actor=actor,
            idempotency_key=f"durable-tail-return-{index}",
        )
        post_supply_document(document=draft, actor=actor)
        returned_amounts.append(draft.lines.get().posted_amount)
        custody.refresh_from_db()
    assert sum(returned_amounts, ZERO_MONEY) == issue.lines.get().posted_amount
    assert custody.status == "closed"
    assert custody.current_quantity == Decimal("0.0000")
    assert custody.current_amount == ZERO_MONEY
