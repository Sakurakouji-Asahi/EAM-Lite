from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.offboarding.services import (
    complete_clearance,
    create_supplemental_clearance,
    initiate_clearance,
    refresh_clearance,
)
from apps.supplies.models import (
    EmployeeSupplyClearanceItem,
    SupplyCountLine,
    SupplyCountTask,
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyDocument,
    SupplyStockBalance,
    SupplyStockLedger,
)
from apps.supplies.services import (
    add_supply_count_item,
    cancel_supply_count_task,
    close_supply_count_task,
    correct_custody_for_count,
    create_supply_count_task,
    post_supply_document,
    publish_supply_count_task,
    record_supply_count,
    return_custody_for_count,
    return_custody_to_warehouse,
    set_supply_count_adjustment_cost,
    stop_supply_count_entry,
    transfer_custody,
    write_off_custody,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_department,
    make_employee,
    make_issue_document,
    make_supply_item,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db


def make_count(
    *, actor, company, domain, warehouse=None, department=None, employee=None, key
):
    task = create_supply_count_task(
        actor=actor,
        company=company,
        data={
            "name": f"{key} 专项盘点",
            "count_domain": domain,
            "warehouse": warehouse,
            "department": department,
            "employee": employee,
            "planned_start": date(2026, 8, 27),
            "planned_end": date(2026, 8, 28),
            "idempotency_key": key,
        },
    )
    return task


def issued_custody(*, quantity="3", unit_cost="80"):
    company, actor, department, employee, source, target, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity=quantity,
        unit_cost=unit_cost,
        key="s17-custody-stock",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity=quantity,
        key="s17-custody-issue",
    )
    post_supply_document(document=issue, actor=actor)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    return company, actor, department, employee, source, target, durable, custody


def test_warehouse_count_draft_does_not_freeze_publish_freezes_and_cancel_releases():
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="10",
        unit_cost="100",
        key="s17-freeze-seed",
    )
    task = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-freeze-count",
    )
    draft = seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="1",
        unit_cost="100",
        key="s17-before-publish",
    )
    draft.refresh_from_db()
    assert draft.status == "posted"
    publish_supply_count_task(task=task, actor=actor)
    blocked = make_issue_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        department=company.departments.first(),
        quantity="1",
        key="s17-blocked-issue",
    )
    with pytest.raises(ValidationError, match="正在进行低值物品盘点"):
        post_supply_document(document=blocked, actor=actor)
    cancel_supply_count_task(task=task, actor=actor, reason="重新安排盘点")
    post_supply_document(document=blocked, actor=actor)
    blocked.refresh_from_db()
    assert blocked.status == "posted"


def test_warehouse_count_mixed_gain_loss_posts_one_document_and_no_difference_posts_none():
    company, actor, _, _, warehouse, _, consumable, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=consumable,
        quantity="10",
        unit_cost="100",
        key="s17-mixed-a",
    )
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="10",
        unit_cost="80",
        key="s17-mixed-b",
    )
    task = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-mixed-count",
    )
    publish_supply_count_task(task=task, actor=actor)
    record_supply_count(
        line=task.lines.get(item=consumable),
        counted_quantity=Decimal("11"),
        remark="现场多一箱",
        actor=actor,
    )
    record_supply_count(
        line=task.lines.get(item=durable),
        counted_quantity=Decimal("8"),
        remark="现场少两把",
        actor=actor,
    )
    stop_supply_count_entry(task=task, actor=actor)
    close_supply_count_task(task=task, actor=actor)
    task.refresh_from_db()
    adjustment = task.adjustment_document
    assert task.status == "closed"
    assert adjustment.status == "posted"
    assert adjustment.document_type == "count_adjustment"
    assert adjustment.lines.count() == 2
    assert set(adjustment.stock_ledgers.values_list("movement_type", flat=True)) == {
        "count_gain",
        "count_loss",
    }
    assert SupplyStockBalance.objects.get(
        warehouse=warehouse, item=consumable
    ).quantity_on_hand == Decimal("11.0000")
    loss_balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=durable)
    assert loss_balance.quantity_on_hand == Decimal("8.0000")
    assert loss_balance.amount_on_hand == Decimal("640.00")

    second = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-no-diff",
    )
    publish_supply_count_task(task=second, actor=actor)
    for line in second.lines.all():
        record_supply_count(
            line=line,
            counted_quantity=line.expected_quantity,
            remark="",
            actor=actor,
        )
    stop_supply_count_entry(task=second, actor=actor)
    close_supply_count_task(task=second, actor=actor)
    assert not SupplyDocument.objects.filter(source_count_task=second).exists()


def test_zero_stock_gain_requires_cost_and_zero_reason():
    company, actor, _, _, warehouse, _, _, _ = supply_context()
    item = make_supply_item(
        company,
        company.supply_categories.first(),
        "ZERO-GAIN",
    )
    task = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-zero-gain",
    )
    publish_supply_count_task(task=task, actor=actor)
    line = add_supply_count_item(task=task, item=item, actor=actor)
    record_supply_count(
        line=line,
        counted_quantity=Decimal("2"),
        remark="现场发现两件",
        actor=actor,
    )
    stop_supply_count_entry(task=task, actor=actor)
    with pytest.raises(ValidationError, match="必须填写单位成本"):
        close_supply_count_task(task=task, actor=actor)
    with pytest.raises(ValidationError, match="零成本原因"):
        set_supply_count_adjustment_cost(
            line=line, unit_cost=Decimal("0"), actor=actor
        )
    set_supply_count_adjustment_cost(
        line=line,
        unit_cost=Decimal("12.5"),
        actor=actor,
    )
    close_supply_count_task(task=task, actor=actor)
    balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=item)
    assert balance.quantity_on_hand == Decimal("2.0000")
    assert balance.amount_on_hand == Decimal("25.00")


def test_same_warehouse_active_task_stop_immutability_and_close_rollback(monkeypatch):
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="5",
        unit_cost="10",
        key="s17-rollback-seed",
    )
    first = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-active-first",
    )
    second = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-active-second",
    )
    publish_supply_count_task(task=first, actor=actor)
    with pytest.raises(ValidationError, match="已有进行中"):
        publish_supply_count_task(task=second, actor=actor)
    line = first.lines.get()
    record_supply_count(
        line=line,
        counted_quantity=Decimal("6"),
        remark="盘盈一件",
        actor=actor,
    )
    stop_supply_count_entry(task=first, actor=actor)
    with pytest.raises(ValidationError, match="只有进行中"):
        record_supply_count(
            line=line,
            counted_quantity=Decimal("5"),
            remark="停止后篡改",
            actor=actor,
        )
    from apps.supplies import services

    original_create_ledger = services._create_stock_ledger

    def fail_ledger(*, values):
        raise ValidationError("受控盘点关闭故障")

    monkeypatch.setattr(services, "_create_stock_ledger", fail_ledger)
    with pytest.raises(ValidationError, match="受控盘点关闭故障"):
        close_supply_count_task(task=first, actor=actor)
    first.refresh_from_db()
    line.refresh_from_db()
    balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=item)
    assert first.status == "reconciliation"
    assert balance.quantity_on_hand == Decimal("5.0000")
    assert line.adjustment_document_line_id is None
    assert not SupplyDocument.objects.filter(source_count_task=first).exists()
    monkeypatch.setattr(services, "_create_stock_ledger", original_create_ledger)


def test_close_rejects_balance_drift_even_if_stock_freeze_was_bypassed():
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="5",
        unit_cost="10",
        key="s17-drift-seed",
    )
    task = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-drift-count",
    )
    publish_supply_count_task(task=task, actor=actor)
    line = task.lines.get()
    record_supply_count(
        line=line,
        counted_quantity=Decimal("5"),
        remark="",
        actor=actor,
    )
    stop_supply_count_entry(task=task, actor=actor)
    from apps.supplies import services

    balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=item)
    services._base_update(
        SupplyStockBalance,
        balance.pk,
        {
            "quantity_on_hand": Decimal("6.0000"),
            "amount_on_hand": Decimal("60.00"),
            "average_unit_cost": Decimal("10.000000"),
            "updated_at": timezone.now(),
        },
        "controlled_supply_balance_mutation",
    )
    with pytest.raises(ValidationError, match="余额与盘点快照不一致"):
        close_supply_count_task(task=task, actor=actor)
    task.refresh_from_db()
    assert task.status == "reconciliation"


def test_custody_count_negative_and_positive_correction_do_not_change_stock():
    company, actor, department, employee, source, _, durable, custody = issued_custody()
    equipment = make_user("s17-custody-equipment", "equipment")
    stock_before = SupplyStockBalance.objects.get(
        warehouse=source, item=durable
    ).quantity_on_hand
    task = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=department,
        employee=employee,
        key="s17-custody-negative",
    )
    publish_supply_count_task(task=task, actor=equipment)
    line = task.lines.get(custody=custody)
    record_supply_count(
        line=line,
        counted_quantity=Decimal("2"),
        remark="现场少一把",
        actor=equipment,
    )
    stop_supply_count_entry(task=task, actor=equipment)
    movement = correct_custody_for_count(
        count_line=line,
        actor=equipment,
        reason="经复核账面多记一把",
        idempotency_key="s17-correction-negative",
    )
    assert movement.action == "correction"
    assert movement.from_custody_id == custody.pk
    assert movement.to_custody_id is None
    close_supply_count_task(task=task, actor=equipment)
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("2.0000")
    assert SupplyStockBalance.objects.get(
        warehouse=source, item=durable
    ).quantity_on_hand == stock_before
    assert not SupplyStockLedger.objects.filter(
        document__source_count_task=task
    ).exists()

    positive = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=department,
        employee=employee,
        key="s17-custody-positive",
    )
    publish_supply_count_task(task=positive, actor=equipment)
    positive_line = positive.lines.get(custody=custody)
    record_supply_count(
        line=positive_line,
        counted_quantity=Decimal("3"),
        remark="现场多一把",
        actor=equipment,
    )
    stop_supply_count_entry(task=positive, actor=equipment)
    positive_movement = correct_custody_for_count(
        count_line=positive_line,
        actor=equipment,
        reason="期初保管少记一把",
        idempotency_key="s17-correction-positive",
    )
    assert positive_movement.from_custody_id is None
    assert positive_movement.to_custody_id == custody.pk
    close_supply_count_task(task=positive, actor=equipment)
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("3.0000")


def test_offboarding_supply_item_partial_action_stays_pending_and_final_action_resolves():
    company, actor, _, employee_department, source, _, durable, custody = issued_custody(
        quantity="2"
    )
    employee = custody.employee
    employee.hire_date = timezone.localdate() - timedelta(days=30)
    employee.save(update_fields=["hire_date", "updated_at"])
    hr = make_user("s17-hr", "hr")
    clearance = initiate_clearance(
        actor=hr,
        employee=employee,
        idempotency_key="s17-offboarding-init",
    )
    assert clearance.total_assets_snapshot == 0
    assert clearance.unresolved_assets == 0
    assert clearance.total_supply_custodies_snapshot == 1
    assert clearance.unresolved_supply_custodies == 1
    supply_item = EmployeeSupplyClearanceItem.objects.get(clearance=clearance)
    first = write_off_custody(
        custody=custody,
        quantity=Decimal("1"),
        action="loss",
        business_date=timezone.localdate(),
        reason="离职清退部分报损",
        actor=actor,
        idempotency_key="s17-offboarding-partial",
    )
    supply_item.refresh_from_db()
    clearance.refresh_from_db()
    assert first.action == "loss"
    assert supply_item.resolution == "pending"
    assert clearance.unresolved_supply_custodies == 1
    with pytest.raises(ValidationError, match="数量型低值耐用品"):
        complete_clearance(
            actor=hr,
            clearance=clearance,
            termination_date=timezone.localdate(),
        )
    final = write_off_custody(
        custody=custody,
        quantity=Decimal("1"),
        action="scrap",
        business_date=timezone.localdate(),
        reason="离职清退剩余报废",
        actor=actor,
        idempotency_key="s17-offboarding-final",
    )
    supply_item.refresh_from_db()
    clearance.refresh_from_db()
    assert supply_item.resolution == "scrapped"
    assert supply_item.custody_movement_id == final.pk
    assert clearance.unresolved_supply_custodies == 0
    completed = complete_clearance(
        actor=hr,
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    assert completed.status == "completed"


@pytest.mark.parametrize("resolution_type", ["return", "transfer", "loss", "scrap"])
def test_custody_count_negative_difference_requires_and_links_real_action(
    resolution_type,
):
    company, _, department, employee, _, target, _, custody = issued_custody()
    equipment = make_user(f"s17-count-{resolution_type}", "equipment")
    task = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=department,
        employee=employee,
        key=f"s17-count-action-{resolution_type}",
    )
    publish_supply_count_task(task=task, actor=equipment)
    line = task.lines.get(custody=custody)
    record_supply_count(
        line=line,
        counted_quantity=Decimal("2"),
        remark=f"{resolution_type} 解决差异",
        actor=equipment,
    )
    stop_supply_count_entry(task=task, actor=equipment)
    if resolution_type == "return":
        movement = return_custody_for_count(
            count_line=line,
            target_warehouse=target,
            business_date=timezone.localdate(),
            reason="盘点差异归还",
            actor=equipment,
            idempotency_key="s17-count-return",
        )
    elif resolution_type == "transfer":
        target_department = make_department(company, "S17-COUNT-TARGET")
        target_employee = make_employee(
            company, target_department, "S17-COUNT-RECEIVER"
        )
        transfer_custody(
            custody=custody,
            quantity=Decimal("1"),
            target_department=target_department,
            target_employee=target_employee,
            business_date=timezone.localdate(),
            reason="盘点差异转交",
            actor=equipment,
            idempotency_key="s17-count-transfer",
            count_line=line,
        )
        movement = SupplyCustodyMovement.objects.get(
            action="transfer", from_custody=custody
        )
    else:
        movement = write_off_custody(
            custody=custody,
            quantity=Decimal("1"),
            action=resolution_type,
            business_date=timezone.localdate(),
            reason=f"盘点差异{resolution_type}",
            actor=equipment,
            idempotency_key=f"s17-count-{resolution_type}",
            count_line=line,
        )
    line.refresh_from_db()
    assert line.resolution_type == resolution_type
    assert line.resolution_custody_movement_id == movement.pk
    close_supply_count_task(task=task, actor=equipment)
    task.refresh_from_db()
    assert task.status == "closed"


def test_custody_count_freezes_ordinary_actions_and_exact_resolution_quantity():
    company, _, department, employee, _, _, _, custody = issued_custody()
    equipment = make_user("s17-count-freeze-equipment", "equipment")
    task = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=department,
        employee=employee,
        key="s17-count-custody-freeze",
    )
    publish_supply_count_task(task=task, actor=equipment)
    with pytest.raises(ValidationError, match="正在进行耐用品盘点"):
        write_off_custody(
            custody=custody,
            quantity=Decimal("1"),
            action="loss",
            business_date=timezone.localdate(),
            reason="普通动作应冻结",
            actor=equipment,
            idempotency_key="s17-count-frozen-loss",
        )
    line = task.lines.get(custody=custody)
    record_supply_count(
        line=line,
        counted_quantity=Decimal("2"),
        remark="少一把",
        actor=equipment,
    )
    stop_supply_count_entry(task=task, actor=equipment)
    with pytest.raises(ValidationError, match="精确等于差异"):
        write_off_custody(
            custody=custody,
            quantity=Decimal("0.5"),
            action="loss",
            business_date=timezone.localdate(),
            reason="错误数量",
            actor=equipment,
            idempotency_key="s17-count-wrong-qty",
            count_line=line,
        )
    with pytest.raises(ValidationError, match="对应盘点差异行"):
        write_off_custody(
            custody=custody,
            quantity=Decimal("1"),
            action="loss",
            business_date=timezone.localdate(),
            reason="绕过盘点行",
            actor=equipment,
            idempotency_key="s17-count-no-line",
        )


@pytest.mark.parametrize(
    ("action", "expected_resolution"),
    [
        ("return", "returned"),
        ("transfer", "transferred"),
        ("loss", "lost"),
        ("scrap", "scrapped"),
    ],
)
def test_offboarding_all_real_custody_actions_resolve_with_final_movement(
    action, expected_resolution
):
    company, actor, _, employee, _, target, _, custody = issued_custody(
        quantity="1"
    )
    employee = custody.employee
    employee.hire_date = timezone.localdate() - timedelta(days=30)
    employee.save(update_fields=["hire_date", "updated_at"])
    hr = make_user(f"s17-offboard-{action}-hr", "hr")
    clearance = initiate_clearance(
        actor=hr,
        employee=employee,
        idempotency_key=f"s17-offboard-{action}-init",
    )
    if action == "return":
        document = return_custody_to_warehouse(
            custody=custody,
            target_warehouse=target,
            quantity=Decimal("1"),
            business_date=timezone.localdate(),
            reason="离职全部归还",
            actor=actor,
            idempotency_key="s17-offboard-return",
        )
        post_supply_document(document=document, actor=actor)
        movement = SupplyCustodyMovement.objects.get(
            action="return", from_custody=custody
        )
    elif action == "transfer":
        target_department = make_department(company, "S17-OFF-TARGET")
        receiver = make_employee(company, target_department, "S17-OFF-RECEIVER")
        transfer_custody(
            custody=custody,
            quantity=Decimal("1"),
            target_department=target_department,
            target_employee=receiver,
            business_date=timezone.localdate(),
            reason="离职全部转交",
            actor=actor,
            idempotency_key="s17-offboard-transfer",
        )
        movement = SupplyCustodyMovement.objects.get(
            action="transfer", from_custody=custody
        )
    else:
        movement = write_off_custody(
            custody=custody,
            quantity=Decimal("1"),
            action=action,
            business_date=timezone.localdate(),
            reason=f"离职{action}",
            actor=actor,
            idempotency_key=f"s17-offboard-{action}",
        )
    item = EmployeeSupplyClearanceItem.objects.get(clearance=clearance)
    assert item.resolution == expected_resolution
    assert item.custody_movement_id == movement.pk
    clearance.refresh_from_db()
    assert clearance.unresolved_supply_custodies == 0


def test_supply_clearance_refresh_and_supplement_capture_historical_omission(
    monkeypatch,
):
    company, _, _, employee, _, _, _, custody = issued_custody(quantity="1")
    employee = custody.employee
    employee.hire_date = timezone.localdate() - timedelta(days=30)
    employee.save(update_fields=["hire_date", "updated_at"])
    hr = make_user("s17-supply-supplement-hr", "hr")
    from apps.supplies import services as supply_services

    real_create = supply_services.create_supply_clearance_items
    monkeypatch.setattr(
        supply_services, "create_supply_clearance_items", lambda **kwargs: []
    )
    clearance = initiate_clearance(
        actor=hr,
        employee=employee,
        idempotency_key="s17-supply-refresh-init",
    )
    assert clearance.total_supply_custodies_snapshot == 0
    monkeypatch.setattr(
        supply_services, "create_supply_clearance_items", real_create
    )
    refreshed = refresh_clearance(
        actor=hr,
        clearance=clearance,
        reason="核对发现历史耐用品遗漏",
    )
    assert refreshed.total_supply_custodies_snapshot == 1
    item = refreshed.supply_items.get()
    assert item.custody_id == custody.pk


@pytest.mark.django_db(transaction=True)
def test_completed_clearance_uses_existing_supplement_for_missed_custody(
    monkeypatch,
):
    _, _, _, _, _, _, _, custody = issued_custody(quantity="1")
    employee = custody.employee
    employee.hire_date = timezone.localdate() - timedelta(days=30)
    employee.save(update_fields=["hire_date", "updated_at"])
    hr = make_user("s17-supply-completed-supplement-hr", "hr")
    from apps.supplies import services as supply_services

    real_create = supply_services.create_supply_clearance_items
    monkeypatch.setattr(
        supply_services, "create_supply_clearance_items", lambda **kwargs: []
    )
    original = initiate_clearance(
        actor=hr,
        employee=employee,
        idempotency_key="s17-supply-supplement-init",
    )
    original = complete_clearance(
        actor=hr,
        clearance=original,
        termination_date=timezone.localdate(),
    )
    monkeypatch.setattr(
        supply_services, "create_supply_clearance_items", real_create
    )
    supplemental = create_supplemental_clearance(
        actor=hr,
        original_clearance=original,
        reason="完成后发现历史耐用品遗漏",
        idempotency_key="s17-supply-supplement-create",
    )
    assert supplemental.supplements_clearance_id == original.pk
    assert supplemental.total_assets_snapshot == 0
    assert supplemental.total_supply_custodies_snapshot == 1
    assert supplemental.supply_items.get().custody_id == custody.pk
