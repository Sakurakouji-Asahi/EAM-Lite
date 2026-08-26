from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.db.models import Sum

from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from apps.supplies.services import create_supply_document, post_supply_document, reverse_supply_document
from tests.test_sprint15_services import make_return, make_transfer
from tests.test_sprint15_support import (
    make_company,
    make_department,
    make_issue_document,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 15 concurrency acceptance requires PostgreSQL 18.4")


def run_parallel(callables):
    with ThreadPoolExecutor(max_workers=len(callables)) as pool:
        futures = [pool.submit(callable_) for callable_ in callables]
        return [future.result(timeout=20) for future in futures]


def test_postgresql_concurrent_issue_return_transfer_and_reversal_numbers_are_unique():
    require_postgresql()
    company = make_company()
    actor = make_user("s15-number-actor", "warehouse")
    department = make_department(company)
    category = make_supply_category(company)
    source = make_supply_warehouse(company, "A")
    target = make_supply_warehouse(company, "B")
    item = make_supply_item(company, category)
    seed_supply_stock(
        actor=actor, company=company, warehouse=source, item=item, quantity="30"
    )

    def concurrent_create(factory):
        barrier = Barrier(2)

        def worker(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return factory(index).document_no
            finally:
                close_old_connections()

        return run_parallel([lambda: worker(1), lambda: worker(2)])

    issue_numbers = concurrent_create(
        lambda index: make_issue_document(
            actor=type(actor).objects.get(pk=actor.pk),
            company=type(company).objects.get(pk=company.pk),
            warehouse=type(source).objects.get(pk=source.pk),
            item=type(item).objects.get(pk=item.pk),
            department=type(department).objects.get(pk=department.pk),
            key=f"number-issue-{index}",
        )
    )
    assert set(issue_numbers) == {"LY-2026-000001", "LY-2026-000002"}

    posted_issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="5",
        key="number-return-source",
    )
    post_supply_document(document=posted_issue, actor=actor)
    source_line_id = posted_issue.lines.get().pk
    return_numbers = concurrent_create(
        lambda index: make_return(
            actor=type(actor).objects.get(pk=actor.pk),
            company=type(company).objects.get(pk=company.pk),
            target=type(target).objects.get(pk=target.pk),
            source_line=posted_issue.lines.model.objects.select_related("item").get(pk=source_line_id),
            quantity="1",
            key=f"number-return-{index}",
        )
    )
    assert set(return_numbers) == {"TH-2026-000001", "TH-2026-000002"}

    transfer_numbers = concurrent_create(
        lambda index: make_transfer(
            actor=type(actor).objects.get(pk=actor.pk),
            company=type(company).objects.get(pk=company.pk),
            source=type(source).objects.get(pk=source.pk),
            target=type(target).objects.get(pk=target.pk),
            item=type(item).objects.get(pk=item.pk),
            quantity="1",
            key=f"number-transfer-{index}",
        )
    )
    assert set(transfer_numbers) == {"DB-2026-000001", "DB-2026-000002"}

    first = create_supply_document(
        actor=actor,
        company=company,
        document_type="receipt",
        data={
            "business_date": "2026-08-26",
            "target_warehouse": target,
            "idempotency_key": "number-reversal-source-1",
        },
        lines=[{"item": item, "quantity": Decimal("1"), "entered_unit_cost": Decimal("1")}],
    )
    second_item = make_supply_item(company, category, "SECOND")
    second = create_supply_document(
        actor=actor,
        company=company,
        document_type="receipt",
        data={
            "business_date": "2026-08-26",
            "target_warehouse": target,
            "idempotency_key": "number-reversal-source-2",
        },
        lines=[{"item": second_item, "quantity": Decimal("1"), "entered_unit_cost": Decimal("1")}],
    )
    post_supply_document(document=first, actor=actor)
    post_supply_document(document=second, actor=actor)
    barrier = Barrier(2)

    def reverse_worker(document_id, index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return reverse_supply_document(
                document=SupplyDocument.objects.get(pk=document_id),
                actor=type(actor).objects.get(pk=actor.pk),
                idempotency_key=f"number-reversal-{index}",
                reason="编号并发测试",
            ).document_no
        finally:
            close_old_connections()

    reversal_numbers = run_parallel(
        [lambda: reverse_worker(first.pk, 1), lambda: reverse_worker(second.pk, 2)]
    )
    assert set(reversal_numbers) == {"CX-2026-000001", "CX-2026-000002"}


def test_postgresql_concurrent_issues_do_not_oversell():
    require_postgresql()
    company = make_company()
    actor = make_user("s15-oversell-actor", "warehouse")
    department = make_department(company)
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    seed_supply_stock(
        actor=actor, company=company, warehouse=warehouse, item=item, quantity="5"
    )
    documents = [
        make_issue_document(
            actor=actor,
            company=company,
            warehouse=warehouse,
            item=item,
            department=department,
            quantity="4",
            key=f"oversell-{index}",
        )
        for index in (1, 2)
    ]
    barrier = Barrier(2)

    def worker(document_id):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                post_supply_document(
                    document=SupplyDocument.objects.get(pk=document_id),
                    actor=type(actor).objects.get(pk=actor.pk),
                )
                return "posted"
            except ValidationError as exc:
                return "库存不足" if "库存不足" in str(exc) else str(exc)
        finally:
            close_old_connections()

    results = run_parallel(
        [lambda: worker(documents[0].pk), lambda: worker(documents[1].pk)]
    )
    assert results.count("posted") == 1
    assert results.count("库存不足") == 1
    balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=item)
    assert balance.quantity_on_hand == Decimal("1.0000")
    assert balance.amount_on_hand >= 0
    assert SupplyStockLedger.objects.filter(movement_type="issue_out").count() == 1


def test_postgresql_concurrent_returns_cannot_exceed_original_issue():
    require_postgresql()
    company = make_company()
    actor = make_user("s15-return-race-actor", "warehouse")
    department = make_department(company)
    category = make_supply_category(company)
    source = make_supply_warehouse(company, "SOURCE")
    target = make_supply_warehouse(company, "TARGET")
    item = make_supply_item(company, category)
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item, quantity="5")
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="5",
    )
    post_supply_document(document=issue, actor=actor)
    returns = [
        make_return(
            actor=actor,
            company=company,
            target=target,
            source_line=issue.lines.get(),
            quantity="4",
            key=f"return-race-{index}",
        )
        for index in (1, 2)
    ]
    barrier = Barrier(2)

    def worker(document_id):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                post_supply_document(
                    document=SupplyDocument.objects.get(pk=document_id),
                    actor=type(actor).objects.get(pk=actor.pk),
                )
                return "posted"
            except ValidationError:
                return "rejected"
        finally:
            close_old_connections()

    results = run_parallel(
        [lambda: worker(returns[0].pk), lambda: worker(returns[1].pk)]
    )
    assert sorted(results) == ["posted", "rejected"]
    assert issue.lines.get().return_lines.filter(
        document__document_type="return", document__status="posted"
    ).aggregate(total=Sum("quantity"))["total"] == Decimal("4.0000")


def test_postgresql_opposite_direction_transfers_use_stable_lock_order():
    require_postgresql()
    company = make_company()
    actor = make_user("s15-opposite-actor", "warehouse")
    category = make_supply_category(company)
    warehouse_a = make_supply_warehouse(company, "A")
    warehouse_b = make_supply_warehouse(company, "B")
    item = make_supply_item(company, category)
    seed_supply_stock(actor=actor, company=company, warehouse=warehouse_a, item=item, quantity="10", key="seed-a")
    seed_supply_stock(actor=actor, company=company, warehouse=warehouse_b, item=item, quantity="10", key="seed-b")
    a_to_b = make_transfer(
        actor=actor,
        company=company,
        source=warehouse_a,
        target=warehouse_b,
        item=item,
        quantity="3",
        key="a-to-b",
    )
    b_to_a = make_transfer(
        actor=actor,
        company=company,
        source=warehouse_b,
        target=warehouse_a,
        item=item,
        quantity="3",
        key="b-to-a",
    )
    barrier = Barrier(2)

    def worker(document_id):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            post_supply_document(
                document=SupplyDocument.objects.get(pk=document_id),
                actor=type(actor).objects.get(pk=actor.pk),
            )
            return "posted"
        finally:
            close_old_connections()

    assert run_parallel(
        [lambda: worker(a_to_b.pk), lambda: worker(b_to_a.pk)]
    ) == ["posted", "posted"]
    assert SupplyStockBalance.objects.get(warehouse=warehouse_a, item=item).quantity_on_hand == Decimal("10.0000")
    assert SupplyStockBalance.objects.get(warehouse=warehouse_b, item=item).quantity_on_hand == Decimal("10.0000")
    assert SupplyStockLedger.objects.filter(document__in=(a_to_b, b_to_a)).count() == 4
