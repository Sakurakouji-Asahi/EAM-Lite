from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.supplies.forms import SupplyItemForm
from apps.supplies.models import SupplyItem, SupplyStockBalance
from apps.supplies.services import add_supply_count_item, post_supply_document, publish_supply_count_task, update_supply_item
from tests.test_sprint14_services import stock_context
from tests.test_sprint14_support import make_supply_document
from tests.test_sprint17_services import make_count


pytestmark = pytest.mark.django_db(transaction=True)


def test_unused_unit_can_be_corrected_but_draft_and_posted_quantities_keep_their_unit():
    company, actor, warehouse, item = stock_context()
    item = update_supply_item(actor=actor, item=item, data={'unit': '只'})
    assert item.unit == '只'
    document = make_supply_document(actor=actor, company=company, warehouse=warehouse, item=item,
                                   quantity='10', unit_cost='100', key='unit-guard-document')
    for posted in (False, True):
        if posted:
            post_supply_document(actor=actor, document=document)
        with pytest.raises(ValidationError, match='计量单位'):
            update_supply_item(actor=actor, item=item, data={'unit': '箱'})
        item.refresh_from_db()
        assert item.unit == '只' and document.lines.get().quantity == Decimal('10.0000')
        assert SupplyItemForm(actor=actor, company=company, instance=item).fields['unit'].disabled
    balance = SupplyStockBalance.objects.get(item=item, warehouse=warehouse)
    assert balance.quantity_on_hand == Decimal('10.0000') and balance.amount_on_hand == Decimal('1000.00')
    updated = update_supply_item(actor=actor, item=item, data={'unit': ' 只 ', 'name': '更正名称'})
    assert updated.unit == '只' and updated.name == '更正名称'


def test_postgresql_unit_guard_rejects_direct_updates_and_round_trips_migration():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL trigger verification requires PostgreSQL')
    company, actor, warehouse, item = stock_context()
    document = make_supply_document(actor=actor, company=company, warehouse=warehouse, item=item,
                                   quantity='10', unit_cost='100', key='unit-db-guard')
    post_supply_document(actor=actor, document=document)
    original = SupplyItem.objects.filter(pk=item.pk).values().get()
    with pytest.raises(IntegrityError, match='referenced supply item unit'):
        with transaction.atomic():
            SupplyItem.objects.filter(pk=item.pk).update(unit='箱')
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([('supplies', '0010_inventory_accounting_invariants')])
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_trigger WHERE tgname='trg_supply_item_unit_u11'")
            assert cursor.fetchone()[0] == 0
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert SupplyItem.objects.filter(pk=item.pk).values().get() == original
    assert SupplyStockBalance.objects.get(item=item, warehouse=warehouse).quantity_on_hand == Decimal('10.0000')
    with pytest.raises(IntegrityError, match='referenced supply item unit'):
        with transaction.atomic():
            SupplyItem.objects.filter(pk=item.pk).update(unit='箱')


def test_count_only_item_unit_is_also_locked_before_any_stock_or_document_exists():
    company, actor, warehouse, item = stock_context()
    task = make_count(actor=actor, company=company, domain='warehouse_stock', warehouse=warehouse, key='unit-count-only')
    publish_supply_count_task(actor=actor, task=task)
    line = add_supply_count_item(actor=actor, task=task, item=item)
    assert line.expected_quantity == Decimal('0.0000')
    assert not item.document_lines.exists() and not item.stock_ledgers.exists()
    with pytest.raises(ValidationError, match='计量单位'):
        update_supply_item(actor=actor, item=item, data={'unit':'箱'})
    if connection.vendor == 'postgresql':
        with pytest.raises(IntegrityError, match='referenced supply item unit'):
            with transaction.atomic():
                SupplyItem.objects.filter(pk=item.pk).update(unit='箱')
