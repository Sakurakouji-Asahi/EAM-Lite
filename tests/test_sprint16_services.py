from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command

from apps.audit.models import AuditLog
from apps.masterdata.models import UserDepartmentScope
from apps.supplies.models import (
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyStockBalance,
    SupplyStockLedger,
)
from apps.supplies.services import (
    durable_management_totals,
    post_supply_document,
    return_custody_to_warehouse,
    reverse_supply_document,
    transfer_custody,
    write_off_custody,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_department,
    make_employee,
    make_issue_document,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db


def issued_custody(*, quantity="3", unit_cost="3.336667"):
    company, actor, department, employee, source, target, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity=quantity,
        unit_cost=unit_cost,
        key="s16-source-opening",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity=quantity,
        key="s16-durable-issue",
    )
    post_supply_document(document=issue, actor=actor)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    return company, actor, department, employee, source, target, durable, issue, custody


def test_partial_full_durable_return_conserves_amount_and_reversal_restores_custody():
    company, actor, _, _, source, target, durable, issue, custody = issued_custody()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=target,
        item=durable,
        quantity="1",
        unit_cost="10",
        key="s16-target-opening",
    )
    managed_before = sum(
        SupplyStockBalance.objects.filter(item=durable).values_list(
            "amount_on_hand", flat=True
        ),
        custody.current_amount,
    )
    first = return_custody_to_warehouse(
        custody=custody,
        target_warehouse=target,
        quantity=Decimal("1"),
        business_date=date(2026, 8, 26),
        reason="部分归还",
        actor=actor,
        idempotency_key="s16-return-first",
    )
    assert not first.stock_ledgers.exists()
    post_supply_document(document=first, actor=actor)
    custody.refresh_from_db()
    target_balance = SupplyStockBalance.objects.get(warehouse=target, item=durable)
    assert custody.current_quantity == Decimal("2.0000")
    assert custody.current_amount == Decimal("6.67")
    assert first.lines.get().posted_amount == Decimal("3.34")
    assert target_balance.quantity_on_hand == Decimal("2.0000")
    assert target_balance.amount_on_hand == Decimal("13.34")
    assert target_balance.average_unit_cost == Decimal("6.670000")
    assert first.stock_ledgers.get().movement_type == "return_in"
    assert first.lines.get().custody_movements.get().action == "return"

    final = return_custody_to_warehouse(
        custody=custody,
        target_warehouse=target,
        quantity=Decimal("2"),
        business_date=date(2026, 8, 26),
        reason="全部归还",
        actor=actor,
        idempotency_key="s16-return-final",
    )
    post_supply_document(document=final, actor=actor)
    custody.refresh_from_db()
    target_balance.refresh_from_db()
    assert custody.status == "closed"
    assert custody.current_quantity == Decimal("0.0000")
    assert custody.current_amount == Decimal("0.00")
    assert final.lines.get().posted_amount == Decimal("6.67")
    assert target_balance.amount_on_hand + custody.current_amount + SupplyStockBalance.objects.get(
        warehouse=source, item=durable
    ).amount_on_hand == managed_before

    reversal = reverse_supply_document(
        document=final,
        actor=actor,
        idempotency_key="s16-reverse-final-return",
        reason="归还仓库错误",
    )
    custody.refresh_from_db()
    target_balance.refresh_from_db()
    assert custody.status == "open"
    assert custody.current_quantity == Decimal("2.0000")
    assert custody.current_amount == Decimal("6.67")
    movement = SupplyCustodyMovement.objects.get(
        action="reversal", reverses_movement__action="return"
    )
    assert movement.from_custody_id is None
    assert movement.to_custody_id == custody.pk
    assert reversal.stock_ledgers.get().amount_delta == Decimal("-6.67")
    with pytest.raises(ValidationError, match="退回|后续动作"):
        reverse_supply_document(
            document=issue,
            actor=actor,
            idempotency_key="s16-reverse-original-issue",
            reason="不能改写来源历史",
        )


def test_durable_return_rejects_overage_is_idempotent_and_rolls_back_failure(monkeypatch):
    company, actor, _, _, _, target, _, _, custody = issued_custody(
        quantity="2", unit_cost="80"
    )
    with pytest.raises(ValidationError, match="超过当前保管数量"):
        return_custody_to_warehouse(
            custody=custody,
            target_warehouse=target,
            quantity=Decimal("3"),
            business_date=date(2026, 8, 26),
            reason="超量",
            actor=actor,
            idempotency_key="s16-return-over",
        )
    document = return_custody_to_warehouse(
        custody=custody,
        target_warehouse=target,
        quantity=Decimal("1"),
        business_date=date(2026, 8, 26),
        reason="测试回滚",
        actor=actor,
        idempotency_key="s16-return-rollback",
    )
    from apps.supplies import services

    original = services._create_custody_movement

    def fail_return_movement(*, values):
        if values.get("action") == "return":
            raise ValidationError("受控故障")
        return original(values=values)

    monkeypatch.setattr(services, "_create_custody_movement", fail_return_movement)
    with pytest.raises(ValidationError, match="受控故障"):
        post_supply_document(document=document, actor=actor)
    custody.refresh_from_db()
    document.refresh_from_db()
    assert custody.current_quantity == Decimal("2.0000")
    assert custody.current_amount == Decimal("160.00")
    assert document.status == "draft"
    assert not document.stock_ledgers.exists()
    assert not SupplyStockBalance.objects.filter(
        warehouse=target, item=custody.item
    ).exists()
    assert not AuditLog.objects.filter(
        action="supply_document_post", object_id=str(document.pk)
    ).exists()


def test_transfer_partial_full_parent_chain_idempotency_and_no_stock_change():
    company, actor, department, _, source, _, durable, _, custody = issued_custody(
        quantity="4", unit_cost="80"
    )
    target_department = make_department(company, "TARGET-DEPT")
    target_employee = make_employee(company, target_department, "TARGET-EMP")
    stock_before = SupplyStockBalance.objects.get(
        warehouse=source, item=durable
    ).amount_on_hand
    target = transfer_custody(
        custody=custody,
        quantity=Decimal("1.5"),
        target_department=target_department,
        target_employee=target_employee,
        business_date=date(2026, 8, 26),
        reason="岗位调整",
        actor=actor,
        idempotency_key="s16-transfer-partial",
    )
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("2.5000")
    assert custody.current_amount == Decimal("200.00")
    assert target.current_quantity == Decimal("1.5000")
    assert target.current_amount == Decimal("120.00")
    assert target.parent_custody_id == custody.pk
    assert target.origin_issue_line_id is None
    assert target.origin_import_row_id is None
    again = transfer_custody(
        custody=custody,
        quantity=Decimal("1.5"),
        target_department=target_department,
        target_employee=target_employee,
        business_date=date(2026, 8, 26),
        reason="岗位调整",
        actor=actor,
        idempotency_key="s16-transfer-partial",
    )
    assert again.pk == target.pk
    assert SupplyCustodyMovement.objects.filter(action="transfer").count() == 1
    assert SupplyStockBalance.objects.get(
        warehouse=source, item=durable
    ).amount_on_hand == stock_before
    assert not SupplyStockLedger.objects.filter(
        movement_type__in=("transfer_in", "transfer_out")
    ).exists()

    second_target = transfer_custody(
        custody=custody,
        quantity=Decimal("2.5"),
        target_department=target_department,
        target_employee=target_employee,
        business_date=date(2026, 8, 26),
        reason="全部转交",
        actor=actor,
        idempotency_key="s16-transfer-final",
    )
    custody.refresh_from_db()
    assert custody.status == "closed"
    assert custody.current_amount == Decimal("0.00")
    assert second_target.pk != target.pk
    assert SupplyCustody.objects.filter(parent_custody=custody).count() == 2


def test_loss_scrap_amounts_close_and_permissions_enforce_department_scope():
    company, actor, department, _, _, _, _, _, custody = issued_custody(
        quantity="2", unit_cost="80"
    )
    manager = make_user("s16-dept-manager", "department_manager")
    UserDepartmentScope.objects.create(
        company=company,
        user=manager,
        department=department,
        include_descendants=True,
        assigned_by=actor,
    )
    loss = write_off_custody(
        custody=custody,
        quantity=Decimal("0.5"),
        action="loss",
        business_date=date(2026, 8, 26),
        reason="搬运损坏",
        actor=manager,
        idempotency_key="s16-loss",
    )
    custody.refresh_from_db()
    assert loss.amount == Decimal("40.00")
    assert custody.current_quantity == Decimal("1.5000")
    assert custody.current_amount == Decimal("120.00")
    scrap = write_off_custody(
        custody=custody,
        quantity=Decimal("1.5"),
        action="scrap",
        business_date=date(2026, 8, 26),
        reason="达到报废条件",
        actor=manager,
        idempotency_key="s16-scrap",
    )
    custody.refresh_from_db()
    assert scrap.amount == Decimal("120.00")
    assert custody.status == "closed"
    assert not SupplyStockLedger.objects.filter(
        document__business_date=date(2026, 8, 26), movement_type="return_in"
    ).exists()
    other = make_department(company, "OUTSIDE")
    with pytest.raises(PermissionDenied):
        transfer_custody(
            custody=SupplyCustody.objects.get(pk=custody.pk),
            quantity=Decimal("1"),
            target_department=other,
            business_date=date(2026, 8, 26),
            reason="跨范围",
            actor=manager,
            idempotency_key="s16-manager-cross",
        )
    readonly = make_user("s16-management", "management")
    with pytest.raises(PermissionDenied):
        write_off_custody(
            custody=custody,
            quantity=Decimal("1"),
            action="loss",
            business_date=date(2026, 8, 26),
            reason="越权",
            actor=readonly,
            idempotency_key="s16-readonly-loss",
        )


def test_reconcile_supports_transfer_loss_scrap_and_totals_separate_amounts():
    company, actor, _, _, _, _, _, _, custody = issued_custody(
        quantity="2", unit_cost="80"
    )
    write_off_custody(
        custody=custody,
        quantity=Decimal("0.5"),
        action="loss",
        business_date=date(2026, 8, 26),
        reason="核对测试",
        actor=actor,
        idempotency_key="s16-reconcile-loss",
    )
    output = StringIO()
    call_command("reconcile_supply_custodies", company=company.code, stdout=output)
    assert "一致" in output.getvalue()
    totals = durable_management_totals(company=company)
    assert totals["durable_stock_amount"] == Decimal("0.00")
    assert totals["durable_open_custody_amount"] == Decimal("120.00")
    assert totals["durable_managed_amount"] == Decimal("120.00")
    assert totals["controlled_non_fixed_asset_quantity"] == 0
    assert totals["controlled_non_fixed_original_cost"] == Decimal("0.00")
