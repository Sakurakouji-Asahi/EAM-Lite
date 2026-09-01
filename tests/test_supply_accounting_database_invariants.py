import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.supplies.models import (
    SupplyDocument,
    SupplyDocumentLine,
    SupplyStockLedger,
)
from apps.supplies.services import post_supply_document
from tests.test_sprint14_support import make_supply_document
from tests.test_sprint15_services import make_transfer, supply_context
from tests.test_sprint15_support import seed_supply_stock


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("库存核算数据库不变量必须在 PostgreSQL 上验证")


def test_postgresql_accounting_guards_are_deferrable_and_finite_checks_cover_tables():
    require_postgresql()
    expected_triggers = {
        "trg_supply_item_identity_i10",
        "trg_supply_document_accounting_i10",
        "trg_supply_line_accounting_i10",
        "trg_supply_ledger_accounting_i10",
    }
    expected_checks = {
        "ck_supply_item_numeric_finite",
        "ck_supply_line_numeric_finite",
        "ck_supply_balance_numeric_finite",
        "ck_supply_balance_average_reconciles",
        "ck_supply_ledger_numeric_finite",
        "ck_supply_custody_numeric_finite",
        "ck_supply_custody_move_numeric_finite",
        "ck_supply_count_line_numeric_finite",
        "ck_supply_clearance_item_numeric_finite",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tgname, tgdeferrable, tginitdeferred
              FROM pg_trigger
             WHERE tgname = ANY(%s)
               AND NOT tgisinternal
            """,
            [list(expected_triggers)],
        )
        actual_triggers = {
            name: (is_deferrable, initially_deferred)
            for name, is_deferrable, initially_deferred in cursor.fetchall()
        }
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
            [list(expected_checks)],
        )
        actual_checks = {row[0] for row in cursor.fetchall()}
    assert set(actual_triggers) == expected_triggers
    assert all(value == (True, True) for value in actual_triggers.values())
    assert actual_checks == expected_checks


@pytest.mark.parametrize(
    "field_name",
    ("quantity", "entered_unit_cost", "posted_amount"),
)
def test_raw_sql_rejects_nan_quantity_cost_and_amount(field_name):
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    document = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        key=f"nan-{field_name}",
    )
    line = document.lines.get()

    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE supplies_supplydocumentline SET {field_name}='NaN'::numeric WHERE id=%s",
            [line.pk],
        )
    line.refresh_from_db()
    assert str(getattr(line, field_name)) != "NaN"


def test_raw_sql_rejects_nan_item_quantity_that_nonnegative_check_would_accept():
    require_postgresql()
    _, _, _, _, _, _, item, _ = supply_context()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE supplies_supplyitem
               SET minimum_stock_quantity='NaN'::numeric
             WHERE id=%s
            """,
            [item.pk],
        )
    item.refresh_from_db()
    assert str(item.minimum_stock_quantity) != "NaN"


def test_raw_sql_rejects_positive_balance_with_unreconciled_average_cost():
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="3",
        unit_cost="7.25",
        key="db-invariant-balance-average",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_balance_mutation','on',true)"
            )
            cursor.execute(
                """
                UPDATE supplies_supplystockbalance
                   SET average_unit_cost=999.000000
                 WHERE company_id=%s AND warehouse_id=%s AND item_id=%s
                """,
                [company.pk, warehouse.pk, item.pk],
            )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT quantity_on_hand, amount_on_hand, average_unit_cost
              FROM supplies_supplystockbalance
             WHERE company_id=%s AND warehouse_id=%s AND item_id=%s
            """,
            [company.pk, warehouse.pk, item.pk],
        )
        quantity, amount, average = cursor.fetchone()
    assert quantity == Decimal("3.0000")
    assert amount == Decimal("21.75")
    assert average == Decimal("7.250000")


def test_raw_sql_wrong_warehouse_ledger_is_rejected_at_constraint_check():
    require_postgresql()
    company, actor, _, _, source, wrong_warehouse, item, _ = supply_context()
    document = seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="4",
        unit_cost="12.50",
        key="db-invariant-wrong-warehouse",
    )
    original = document.stock_ledgers.get()

    with pytest.raises(IntegrityError, match="one exact inbound ledger"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_ledger_insert','on',true)"
            )
            cursor.execute(
                """
                INSERT INTO supplies_supplystockledger (
                    id, company_id, warehouse_id, item_id,
                    document_id, document_line_id, movement_type,
                    quantity_delta, amount_delta, unit_cost,
                    quantity_before, quantity_after,
                    amount_before, amount_after,
                    average_unit_cost_before, average_unit_cost_after,
                    occurred_at, created_by_id, reverses_ledger_id
                )
                SELECT
                    %s, company_id, %s, item_id,
                    document_id, document_line_id, movement_type,
                    quantity_delta, amount_delta, unit_cost,
                    quantity_before, quantity_after,
                    amount_before, amount_after,
                    average_unit_cost_before, average_unit_cost_after,
                    occurred_at, created_by_id, reverses_ledger_id
                  FROM supplies_supplystockledger
                 WHERE id=%s
                """,
                [uuid.uuid4(), wrong_warehouse.pk, original.pk],
            )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    assert document.stock_ledgers.count() == 1
    assert document.stock_ledgers.get().warehouse_id == source.pk


def test_raw_sql_posted_document_without_ledger_is_rejected_at_commit_boundary():
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    document = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="3",
        unit_cost="9.25",
        key="db-invariant-missing-ledger",
    )
    line = document.lines.get()
    posted_at = timezone.now()

    with pytest.raises(IntegrityError, match="one exact inbound ledger"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE supplies_supplydocumentline
                   SET posted_unit_cost=entered_unit_cost,
                       posted_amount=round(quantity * entered_unit_cost, 2)
                 WHERE id=%s
                """,
                [line.pk],
            )
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_document_transition','on',true)"
            )
            cursor.execute(
                """
                UPDATE supplies_supplydocument
                   SET status='posted', posted_at=%s, posted_by_id=%s
                 WHERE id=%s
                """,
                [posted_at, actor.pk, document.pk],
            )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    document.refresh_from_db()
    line.refresh_from_db()
    assert document.status == "draft"
    assert line.posted_unit_cost is None
    assert line.posted_amount is None
    assert not document.stock_ledgers.exists()


def test_raw_sql_item_type_change_after_posting_is_deferred_then_rejected():
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="1",
        unit_cost="8",
        key="db-invariant-item-freeze",
    )

    with pytest.raises(IntegrityError, match="code and management mode are immutable"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE supplies_supplyitem SET item_type='durable_quantity' WHERE id=%s",
                [item.pk],
            )
            cursor.execute(
                "SELECT item_type FROM supplies_supplyitem WHERE id=%s",
                [item.pk],
            )
            assert cursor.fetchone()[0] == "durable_quantity"
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    item.refresh_from_db()
    assert item.item_type == "consumable"


def test_incomplete_transfer_reversal_cannot_commit_one_ledger_leg():
    require_postgresql()
    company, actor, _, _, source, target, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="10",
        unit_cost="15",
        key="db-invariant-reversal-seed",
    )
    original = make_transfer(
        actor=actor,
        company=company,
        source=source,
        target=target,
        item=item,
        quantity="2",
        key="db-invariant-reversal-transfer",
    )
    post_supply_document(document=original, actor=actor)
    original.refresh_from_db()
    original_line = original.lines.get()
    original_leg = original_line.stock_ledgers.get(movement_type="transfer_out")
    reversed_at = timezone.now()

    with pytest.raises(IntegrityError, match="every original ledger leg"), transaction.atomic():
        reversal = SupplyDocument(
            company=company,
            document_no="CX-2026-RAW-INCOMPLETE",
            document_type="reversal",
            business_date=date(2026, 8, 31),
            source_warehouse=source,
            target_warehouse=target,
            remark="故意遗漏调入腿",
            status="posted",
            idempotency_key="db-invariant-incomplete-reversal",
            reversal_of=original,
            created_by=actor,
            posted_by=actor,
            posted_at=reversed_at,
        )
        reversal._controlled_transition = True
        reversal.save(force_insert=True)
        reversal_line = SupplyDocumentLine(
            company=company,
            document=reversal,
            line_no=original_line.line_no,
            item=item,
            quantity=original_line.quantity,
            posted_unit_cost=original_line.posted_unit_cost,
            posted_amount=original_line.posted_amount,
            line_remark="故意遗漏调入腿",
        )
        reversal_line._controlled_posting = True
        reversal_line.save(force_insert=True)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_ledger_insert','on',true)"
            )
        reversal_ledger = SupplyStockLedger(
            company=company,
            warehouse=source,
            item=item,
            document=reversal,
            document_line=reversal_line,
            movement_type="reversal",
            quantity_delta=-original_leg.quantity_delta,
            amount_delta=-original_leg.amount_delta,
            unit_cost=original_leg.unit_cost,
            quantity_before=original_leg.quantity_after,
            quantity_after=original_leg.quantity_before,
            amount_before=original_leg.amount_after,
            amount_after=original_leg.amount_before,
            average_unit_cost_before=original_leg.average_unit_cost_after,
            average_unit_cost_after=original_leg.average_unit_cost_before,
            occurred_at=reversed_at,
            created_by=actor,
            reverses_ledger=original_leg,
        )
        reversal_ledger._controlled_insert = True
        reversal_ledger.save(force_insert=True)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_document_transition','on',true)"
            )
            cursor.execute(
                """
                UPDATE supplies_supplydocument
                   SET status='reversed', reversed_at=%s, reversed_by_id=%s
                 WHERE id=%s
                """,
                [reversed_at, actor.pk, original.pk],
            )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    original.refresh_from_db()
    assert original.status == "posted"
    assert not SupplyDocument.objects.filter(
        reversal_of=original,
        idempotency_key="db-invariant-incomplete-reversal",
    ).exists()
