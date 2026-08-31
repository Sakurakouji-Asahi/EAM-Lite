from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet

from apps.supplies.models import (
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyStockLedger,
)
from apps.supplies.services import post_supply_document, reverse_supply_document
from tests.test_sprint15_services import make_transfer, supply_context
from tests.test_sprint15_support import make_issue_document, seed_supply_stock


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 15 database guards require PostgreSQL 18.6")


def test_postgresql_sprint15_triggers_are_installed():
    require_postgresql()
    expected = {
        "trg_supply_custody_mutation_s15",
        "trg_supply_custody_refs_s15",
        "trg_supply_custody_move_mutation_s15",
        "trg_supply_custody_move_refs_s15",
        "trg_supply_line_refs_s14",
        "trg_supply_ledger_refs_s14",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND NOT tgisinternal",
            [list(expected)],
        )
        actual = {row[0] for row in cursor.fetchall()}
    assert expected <= actual


def test_postgresql_transfer_line_allows_two_warehouses_but_no_duplicate_direction():
    require_postgresql()
    company, actor, _, _, source, target, item, _ = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item)
    transfer = make_transfer(
        actor=actor,
        company=company,
        source=source,
        target=target,
        item=item,
        quantity="2",
        key="db-transfer",
    )
    post_supply_document(document=transfer, actor=actor)
    line = transfer.lines.get()
    assert line.stock_ledgers.count() == 2
    original = line.stock_ledgers.get(movement_type="transfer_out")
    duplicate = SupplyStockLedger(
        company=company,
        warehouse=source,
        item=item,
        document=transfer,
        document_line=line,
        movement_type="transfer_out",
        quantity_delta=original.quantity_delta,
        amount_delta=original.amount_delta,
        unit_cost=original.unit_cost,
        quantity_before=original.quantity_before,
        quantity_after=original.quantity_after,
        amount_before=original.amount_before,
        amount_after=original.amount_after,
        average_unit_cost_before=original.average_unit_cost_before,
        average_unit_cost_after=original.average_unit_cost_after,
        occurred_at=original.occurred_at,
        created_by=actor,
    )
    duplicate._controlled_insert = True
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_supply_ledger_insert','on',true)"
            )
        duplicate.save(force_insert=True)


def test_postgresql_custody_and_movement_reject_direct_update_delete():
    require_postgresql()
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
    custody = SupplyCustody.objects.get()
    movement = SupplyCustodyMovement.objects.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyCustody._base_manager.filter(pk=custody.pk),
            current_quantity=Decimal("99.0000"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyCustodyMovement._base_manager.filter(pk=movement.pk),
            amount=Decimal("99.00"),
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM supplies_supplycustodymovement WHERE id=%s", [movement.pk]
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM supplies_supplycustody WHERE id=%s", [custody.pk])


def test_reversal_ledger_and_custody_movement_remain_append_only_after_actor_delete():
    require_postgresql()
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
    reverse_supply_document(
        document=issue,
        actor=actor,
        idempotency_key="db-reverse-durable",
        reason="测试冲销",
    )
    reversal_ledger = SupplyStockLedger.objects.get(movement_type="reversal")
    reversal_movement = SupplyCustodyMovement.objects.get(action="reversal")
    values = (
        reversal_ledger.quantity_delta,
        reversal_ledger.amount_delta,
        reversal_movement.quantity,
        reversal_movement.amount,
    )
    actor.delete()
    reversal_ledger.refresh_from_db()
    reversal_movement.refresh_from_db()
    assert reversal_ledger.created_by is None
    assert reversal_movement.created_by is None
    assert values == (
        reversal_ledger.quantity_delta,
        reversal_ledger.amount_delta,
        reversal_movement.quantity,
        reversal_movement.amount,
    )


def test_model_layer_rejects_ordinary_custody_mutation_on_all_backends():
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
    custody = SupplyCustody.objects.get()
    movement = SupplyCustodyMovement.objects.get()
    custody.current_quantity = Decimal("2")
    with pytest.raises(ValidationError):
        custody.save()
    movement.reason = "非法修改"
    with pytest.raises(ValidationError):
        movement.save()
    with pytest.raises(ValidationError):
        movement.delete()
