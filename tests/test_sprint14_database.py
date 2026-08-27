from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet

from apps.supplies.models import (
    SupplyDocument,
    SupplyStockBalance,
    SupplyStockLedger,
)
from apps.supplies.services import post_supply_document
from tests.test_sprint14_support import (
    make_company,
    make_supply_category,
    make_supply_document,
    make_supply_item,
    make_supply_warehouse,
    make_user,
)


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 14 database guards require PostgreSQL")


def context():
    company = make_company()
    actor = make_user("s14-db-warehouse", "warehouse")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    return company, actor, warehouse, item


def test_postgresql_sprint14_triggers_are_installed():
    require_postgresql()
    expected = {
        "trg_supply_sequence_controlled_s14",
        "trg_supply_document_refs_s14",
        "trg_supply_document_mutation_s14",
        "trg_supply_line_refs_s14",
        "trg_supply_line_mutation_s14",
        "trg_supply_line_state_s14",
        "trg_supply_balance_refs_s14",
        "trg_supply_balance_mutation_s14",
        "trg_supply_ledger_mutation_s14",
        "trg_supply_ledger_refs_s14",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND NOT tgisinternal",
            [list(expected)],
        )
        actual = {row[0] for row in cursor.fetchall()}
    assert expected <= actual


def test_postgresql_rejects_cross_company_document_and_balance_references():
    require_postgresql()
    company, _, warehouse, item = context()
    other = make_company("OTHER", active=False)
    foreign_warehouse = make_supply_warehouse(other, "FOREIGN-WH")
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplyDocument.objects.create(
            company=company,
            document_no="QC-2026-900001",
            document_type="opening",
            business_date="2026-08-26",
            target_warehouse=foreign_warehouse,
            idempotency_key="cross-company-document",
        )

    balance = SupplyStockBalance(
        company=company,
        warehouse=foreign_warehouse,
        item=item,
        quantity_on_hand=Decimal("0"),
        amount_on_hand=Decimal("0"),
        average_unit_cost=Decimal("0"),
    )
    balance._controlled_mutation = True
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_balance_mutation','on',true)"
            )
        balance.save(force_insert=True)
    assert warehouse.company_id == company.pk


def test_postgresql_rejects_direct_balance_document_and_ledger_mutation():
    require_postgresql()
    company, actor, warehouse, item = context()
    document = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="db-immutable"
    )
    post_supply_document(document=document, actor=actor)
    document.refresh_from_db()
    balance = SupplyStockBalance.objects.get()
    ledger = SupplyStockLedger.objects.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyStockBalance._base_manager.filter(pk=balance.pk),
            amount_on_hand=Decimal("999.00"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyDocument._base_manager.filter(pk=document.pk), remark="非法直改"
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyStockLedger._base_manager.filter(pk=ledger.pk),
            amount_delta=Decimal("999.00"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM supplies_supplystockledger WHERE id=%s", [ledger.pk]
            )


def test_operator_deletion_sets_null_without_changing_stock_business_values():
    require_postgresql()
    company, actor, warehouse, item = context()
    document = make_supply_document(
        actor=actor, company=company, warehouse=warehouse, item=item, key="actor-null"
    )
    post_supply_document(document=document, actor=actor)
    ledger = SupplyStockLedger.objects.get()
    before = (
        ledger.quantity_delta,
        ledger.amount_delta,
        ledger.quantity_before,
        ledger.quantity_after,
        ledger.amount_before,
        ledger.amount_after,
    )
    actor.delete()
    document.refresh_from_db()
    ledger.refresh_from_db()
    assert document.created_by is None
    assert document.posted_by is None
    assert ledger.created_by is None
    assert before == (
        ledger.quantity_delta,
        ledger.amount_delta,
        ledger.quantity_before,
        ledger.quantity_after,
        ledger.amount_before,
        ledger.amount_after,
    )
