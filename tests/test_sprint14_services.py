from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.models.query import QuerySet

from apps.audit.models import AuditLog
from apps.supplies.models import (
    SupplyDocument,
    SupplyDocumentLine,
    SupplyStockBalance,
    SupplyStockLedger,
)
from apps.supplies.services import (
    cancel_supply_document,
    create_supply_document,
    post_supply_document,
    supply_item_has_business_history,
    update_draft_document,
    update_supply_item,
)
from tests.test_sprint14_support import (
    make_company,
    make_supply_category,
    make_supply_document,
    make_supply_item,
    make_supply_warehouse,
    make_user,
)


pytestmark = pytest.mark.django_db


def stock_context(role="warehouse"):
    company = make_company()
    actor = make_user(f"s14-{role}", role)
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    return company, actor, warehouse, item


def test_document_numbers_are_scoped_by_type_year_and_drafts_do_not_touch_stock():
    company, actor, warehouse, item = stock_context()
    opening = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="opening-1"
    )
    receipt = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        document_type="receipt",
        key="receipt-1",
    )
    opening_two = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="opening-2"
    )
    assert opening.document_no == "QC-2026-000001"
    assert opening_two.document_no == "QC-2026-000002"
    assert receipt.document_no == "RK-2026-000001"
    assert not SupplyStockBalance.objects.exists()
    assert not SupplyStockLedger.objects.exists()


def test_opening_and_receipt_posting_build_immutable_ledger_and_moving_average():
    company, actor, warehouse, item = stock_context()
    opening = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="post-opening"
    )
    post_supply_document(document=opening, actor=actor)
    balance = SupplyStockBalance.objects.get(
        company=company, warehouse=warehouse, item=item
    )
    assert balance.quantity_on_hand == Decimal("10.0000")
    assert balance.amount_on_hand == Decimal("1000.00")
    assert balance.average_unit_cost == Decimal("100.000000")
    assert SupplyStockLedger.objects.filter(
        document=opening, movement_type="opening_in"
    ).count() == 1

    receipt = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        document_type="receipt",
        unit_cost="120.000000",
        key="post-receipt",
    )
    post_supply_document(document=receipt, actor=actor)
    balance.refresh_from_db()
    assert balance.quantity_on_hand == Decimal("20.0000")
    assert balance.amount_on_hand == Decimal("2200.00")
    assert balance.average_unit_cost == Decimal("110.000000")
    line = receipt.lines.get()
    assert line.posted_unit_cost == Decimal("120.000000")
    assert line.posted_amount == Decimal("1200.00")
    assert AuditLog.objects.filter(
        company=company, action="supply_document_post"
    ).count() == 2
    assert supply_item_has_business_history(item)


def test_zero_cost_requires_reason_and_valid_zero_cost_posts():
    company, actor, warehouse, item = stock_context()
    with pytest.raises(ValidationError):
        make_supply_document(
            actor=actor,
            company=company,
            warehouse=warehouse,
            item=item,
            unit_cost="0",
            key="zero-no-reason",
        )
    document = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="1",
        unit_cost="0",
        key="zero-with-reason",
        line_remark="供应商赠品",
    )
    post_supply_document(document=document, actor=actor)
    balance = SupplyStockBalance.objects.get()
    assert balance.quantity_on_hand == Decimal("1.0000")
    assert balance.amount_on_hand == Decimal("0.00")
    assert balance.average_unit_cost == Decimal("0.000000")


def test_duplicate_post_is_idempotent_and_cancelled_draft_cannot_recover():
    company, actor, warehouse, item = stock_context()
    document = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="post-twice"
    )
    first = post_supply_document(
        document=document,
        actor=actor,
        idempotency_key=document.idempotency_key,
    )
    second = post_supply_document(
        document=document,
        actor=actor,
        idempotency_key=document.idempotency_key,
    )
    assert first.pk == second.pk
    assert SupplyStockLedger.objects.count() == 1
    assert SupplyStockBalance.objects.get().quantity_on_hand == Decimal("10.0000")
    with pytest.raises(ValidationError, match="幂等键与该单据不一致"):
        post_supply_document(
            document=document,
            actor=actor,
            idempotency_key="different-post-request",
        )

    cancelled = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="cancel-me"
    )
    cancel_supply_document(actor=actor, document=cancelled, reason="录入错误")
    cancelled.refresh_from_db()
    assert cancelled.status == "cancelled"
    with pytest.raises(ValidationError):
        post_supply_document(document=cancelled, actor=actor)


def test_posting_failure_rolls_back_document_balances_ledgers_lines_and_audit(monkeypatch):
    company, actor, warehouse, item = stock_context()
    second_item = make_supply_item(company, item.category, "ITEM-2")
    document = create_supply_document(
        actor=actor,
        company=company,
        document_type="opening",
        data={
            "business_date": date(2026, 8, 26),
            "target_warehouse": warehouse,
            "idempotency_key": "rollback-post",
        },
        lines=[
            {"item": item, "quantity": Decimal("1"), "entered_unit_cost": Decimal("10")},
            {"item": second_item, "quantity": Decimal("2"), "entered_unit_cost": Decimal("20")},
        ],
    )
    original_save = SupplyStockLedger.save
    calls = {"count": 0}

    def fail_second(instance, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated ledger failure")
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(SupplyStockLedger, "save", fail_second)
    with pytest.raises(RuntimeError, match="simulated ledger failure"):
        post_supply_document(document=document, actor=actor)

    document.refresh_from_db()
    assert document.status == "draft"
    assert not SupplyStockBalance.objects.exists()
    assert not SupplyStockLedger.objects.exists()
    assert not document.lines.exclude(posted_unit_cost=None, posted_amount=None).exists()
    assert not AuditLog.objects.filter(
        company=company, action="supply_document_post", object_id=str(document.pk)
    ).exists()


def test_posted_documents_lines_and_ledgers_reject_ordinary_mutation_and_delete():
    company, actor, warehouse, item = stock_context()
    document = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="immutable"
    )
    post_supply_document(document=document, actor=actor)
    document.refresh_from_db()
    line = document.lines.get()
    ledger = SupplyStockLedger.objects.get()

    document.remark = "非法修改"
    with pytest.raises(ValidationError):
        document.save()
    with pytest.raises(ValidationError):
        SupplyDocument.objects.filter(pk=document.pk).update(remark="非法")
    line.quantity = Decimal("99")
    with pytest.raises(ValidationError):
        line.save()
    with pytest.raises(ValidationError):
        SupplyDocumentLine.objects.filter(pk=line.pk).delete()
    ledger.quantity_delta = Decimal("99")
    with pytest.raises(ValidationError):
        ledger.save()
    with pytest.raises(ValidationError):
        SupplyStockLedger.objects.filter(pk=ledger.pk).update(amount_delta=Decimal("1"))
    with pytest.raises(ValidationError):
        ledger.delete()


def test_draft_update_replaces_lines_without_stock_and_company_role_boundaries_hold():
    company, actor, warehouse, item = stock_context()
    document = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="edit-draft"
    )
    update_draft_document(
        actor=actor,
        document=document,
        data={"remark": "已复核"},
        lines=[
            {"item": item, "quantity": Decimal("3"), "entered_unit_cost": Decimal("15")}
        ],
    )
    document.refresh_from_db()
    assert document.remark == "已复核"
    assert document.lines.get().quantity == Decimal("3.0000")
    assert not SupplyStockBalance.objects.exists()

    other = make_company("OTHER", active=False)
    other_warehouse = make_supply_warehouse(other, "OTHER-WH")
    with pytest.raises(ValidationError):
        update_draft_document(
            actor=actor,
            document=document,
            data={"target_warehouse": other_warehouse},
            lines=[
                {"item": item, "quantity": Decimal("1"), "entered_unit_cost": Decimal("1")}
            ],
        )
    readonly = make_user("s14-management-service", "management")
    with pytest.raises(PermissionDenied):
        post_supply_document(document=document, actor=readonly)


def test_reconcile_command_is_read_only_and_reports_mismatch():
    company, actor, warehouse, item = stock_context()
    document = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="reconcile"
    )
    post_supply_document(document=document, actor=actor)
    output = StringIO()
    call_command("reconcile_supply_balances", company=company.code, stdout=output)
    assert "一致" in output.getvalue()

    balance = SupplyStockBalance.objects.get()
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_balance_mutation','on',true)"
            )
    QuerySet.update(
        SupplyStockBalance._base_manager.filter(pk=balance.pk),
        amount_on_hand=Decimal("999.00"),
        average_unit_cost=Decimal("99.900000"),
    )
    failed_output = StringIO()
    with pytest.raises(CommandError):
        call_command(
            "reconcile_supply_balances", company=company.code, stdout=failed_output
        )
    assert "流水金额=1000.00" in failed_output.getvalue()
    balance.refresh_from_db()
    assert balance.amount_on_hand == Decimal("999.00")


def test_posted_item_code_and_mode_are_frozen():
    company, actor, warehouse, item = stock_context()
    document = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="freeze-item"
    )
    post_supply_document(document=document, actor=actor)
    with pytest.raises(ValidationError):
        update_supply_item(actor=actor, item=item, data={"item_type": "durable_quantity"})
