from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections, connection

from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from apps.supplies.services import create_supply_document, post_supply_document
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
        pytest.skip("Sprint 14 concurrency acceptance requires PostgreSQL")


def test_postgresql_concurrent_document_number_generation_is_unique():
    require_postgresql()
    company = make_company()
    actor = make_user("s14-number-actor", "warehouse")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    barrier = Barrier(2)

    def worker(index):
        close_old_connections()
        try:
            local_actor = type(actor).objects.get(pk=actor.pk)
            local_company = type(company).objects.get(pk=company.pk)
            local_warehouse = type(warehouse).objects.get(pk=warehouse.pk)
            local_item = type(item).objects.get(pk=item.pk)
            barrier.wait(timeout=10)
            document = create_supply_document(
                actor=local_actor,
                company=local_company,
                document_type="receipt",
                data={
                    "business_date": "2026-08-26",
                    "target_warehouse": local_warehouse,
                    "idempotency_key": f"concurrent-number-{index}",
                },
                lines=[
                    {
                        "item": local_item,
                        "quantity": Decimal("1"),
                        "entered_unit_cost": Decimal("1"),
                    }
                ],
            )
            return document.document_no
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = list(pool.map(worker, (1, 2)))
    assert set(numbers) == {"RK-2026-000001", "RK-2026-000002"}
    assert SupplyDocument.objects.filter(company=company).count() == 2


def test_postgresql_concurrent_receipts_same_balance_have_no_lost_update():
    require_postgresql()
    company = make_company()
    actor = make_user("s14-post-actor", "warehouse")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    first = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        document_type="receipt",
        unit_cost="100",
        key="concurrent-post-1",
    )
    second = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        document_type="receipt",
        unit_cost="120",
        key="concurrent-post-2",
    )
    barrier = Barrier(2)

    def worker(document_id):
        close_old_connections()
        try:
            local_actor = type(actor).objects.get(pk=actor.pk)
            local_document = SupplyDocument.objects.get(pk=document_id)
            barrier.wait(timeout=10)
            post_supply_document(document=local_document, actor=local_actor)
            return local_document.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (first.pk, second.pk)))
    assert set(results) == {first.pk, second.pk}
    balance = SupplyStockBalance.objects.get(
        company=company, warehouse=warehouse, item=item
    )
    assert balance.quantity_on_hand == Decimal("20.0000")
    assert balance.amount_on_hand == Decimal("2200.00")
    assert balance.average_unit_cost == Decimal("110.000000")
    assert SupplyStockLedger.objects.filter(company=company).count() == 2
