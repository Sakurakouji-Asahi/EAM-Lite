from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection

from apps.supplies.models import SupplyCustody, SupplyDocument, SupplyStockBalance
from apps.supplies.services import (
    post_supply_document,
    return_custody_to_warehouse,
    transfer_custody,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_department,
    make_issue_document,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 16 custody concurrency acceptance requires PostgreSQL 18.4")


def run_parallel(callables):
    with ThreadPoolExecutor(max_workers=len(callables)) as pool:
        futures = [pool.submit(callable_) for callable_ in callables]
        return [future.result(timeout=20) for future in futures]


def concurrent_custody(quantity="2"):
    company, actor, department, employee, source, target, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity=quantity,
        unit_cost="80",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity=quantity,
    )
    post_supply_document(document=issue, actor=actor)
    return (
        company,
        actor,
        target,
        SupplyCustody.objects.get(origin_issue_line=issue.lines.get()),
    )


def test_postgresql_concurrent_durable_returns_do_not_exceed_custody():
    require_postgresql()
    company, actor, target, custody = concurrent_custody()
    documents = [
        return_custody_to_warehouse(
            custody=custody,
            target_warehouse=target,
            quantity=Decimal("1.5"),
            business_date=date(2026, 8, 26),
            reason="并发归还",
            actor=actor,
            idempotency_key=f"s16-return-race-{index}",
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
                return "rejected" if "超过当前保管" in str(exc) else str(exc)
        finally:
            close_old_connections()

    results = run_parallel(
        [lambda: worker(documents[0].pk), lambda: worker(documents[1].pk)]
    )
    assert sorted(results) == ["posted", "rejected"]
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("0.5000")
    assert custody.current_amount == Decimal("40.00")
    assert SupplyStockBalance.objects.get(
        warehouse=target, item=custody.item
    ).quantity_on_hand == Decimal("1.5000")


def test_postgresql_concurrent_transfers_do_not_exceed_source_custody():
    require_postgresql()
    company, actor, _, custody = concurrent_custody()
    target_department = make_department(company, "TARGET")
    barrier = Barrier(2)

    def worker(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                target = transfer_custody(
                    custody=SupplyCustody.objects.get(pk=custody.pk),
                    quantity=Decimal("1.5"),
                    target_department=type(target_department).objects.get(
                        pk=target_department.pk
                    ),
                    business_date=date(2026, 8, 26),
                    reason="并发转交",
                    actor=type(actor).objects.get(pk=actor.pk),
                    idempotency_key=f"s16-transfer-race-{index}",
                )
                return str(target.pk)
            except ValidationError as exc:
                return "rejected" if "超过当前保管" in str(exc) else str(exc)
        finally:
            close_old_connections()

    results = run_parallel([lambda: worker(1), lambda: worker(2)])
    assert results.count("rejected") == 1
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("0.5000")
    assert SupplyCustody.objects.filter(parent_custody=custody).count() == 1
