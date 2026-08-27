from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection

from apps.supplies.models import SupplyCountTask, SupplyDocument, SupplyStockBalance
from apps.supplies.services import (
    close_supply_count_task,
    create_supply_count_task,
    create_supply_document,
    post_supply_document,
    publish_supply_count_task,
    record_supply_count,
    stop_supply_count_entry,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_supply_warehouse,
    make_user,
    seed_supply_stock,
)
from tests.test_sprint17_services import issued_custody, make_count


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 17 concurrency acceptance requires PostgreSQL 18.4")


def run_parallel(*callables):
    with ThreadPoolExecutor(max_workers=len(callables)) as pool:
        futures = [pool.submit(callable_) for callable_ in callables]
        return [future.result(timeout=30) for future in futures]


def test_publish_and_posting_serialize_on_warehouse_lock():
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="5",
        unit_cost="10",
        key="s17-race-seed",
    )
    task = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-race-count",
    )
    receipt = create_supply_document(
        actor=actor,
        company=company,
        document_type="receipt",
        data={
            "business_date": date(2026, 8, 27),
            "target_warehouse": warehouse,
            "idempotency_key": "s17-race-receipt",
        },
        lines=[
            {
                "item": item,
                "quantity": Decimal("1"),
                "entered_unit_cost": Decimal("10"),
            }
        ],
    )
    barrier = Barrier(2)

    def publish_worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            publish_supply_count_task(
                task=SupplyCountTask.objects.get(pk=task.pk),
                actor=type(actor).objects.get(pk=actor.pk),
            )
            return "published"
        finally:
            close_old_connections()

    def post_worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                post_supply_document(
                    document=SupplyDocument.objects.get(pk=receipt.pk),
                    actor=type(actor).objects.get(pk=actor.pk),
                )
                return "posted"
            except ValidationError as exc:
                return "frozen" if "正在进行低值物品盘点" in str(exc) else str(exc)
        finally:
            close_old_connections()

    results = run_parallel(publish_worker, post_worker)
    assert "published" in results
    assert results[1] in {"posted", "frozen"}
    task.refresh_from_db()
    receipt.refresh_from_db()
    balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=item)
    snapshot = task.lines.get(item=item)
    if receipt.status == "posted":
        assert balance.quantity_on_hand == Decimal("6.0000")
        assert snapshot.expected_quantity == Decimal("6.0000")
    else:
        assert receipt.status == "draft"
        assert balance.quantity_on_hand == Decimal("5.0000")
        assert snapshot.expected_quantity == Decimal("5.0000")


def test_concurrent_close_creates_one_adjustment_document_and_one_ledger():
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="5",
        unit_cost="10",
        key="s17-close-race-seed",
    )
    task = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-close-race-count",
    )
    publish_supply_count_task(task=task, actor=actor)
    record_supply_count(
        line=task.lines.get(),
        counted_quantity=Decimal("6"),
        remark="并发关闭盘盈",
        actor=actor,
    )
    stop_supply_count_entry(task=task, actor=actor)
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            closed = close_supply_count_task(
                task=SupplyCountTask.objects.get(pk=task.pk),
                actor=type(actor).objects.get(pk=actor.pk),
            )
            return closed.status
        finally:
            close_old_connections()

    assert run_parallel(worker, worker) == ["closed", "closed"]
    assert SupplyDocument.objects.filter(source_count_task=task).count() == 1
    assert SupplyDocument.objects.get(source_count_task=task).stock_ledgers.count() == 1


def test_concurrent_count_numbers_are_unique_and_custody_scope_publish_cannot_overlap():
    require_postgresql()
    company, actor, department, employee, _, _, _, custody = issued_custody()
    equipment = make_user("s17-concurrent-equipment", "equipment")
    second_warehouse = make_supply_warehouse(company, "S17-SECOND")
    barrier = Barrier(2)

    def create_worker(index, warehouse):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            task = create_supply_count_task(
                actor=type(actor).objects.get(pk=actor.pk),
                company=type(company).objects.get(pk=company.pk),
                data={
                    "name": f"并发发号 {index}",
                    "count_domain": "warehouse_stock",
                    "warehouse": type(warehouse).objects.get(pk=warehouse.pk),
                    "planned_start": date(2026, 8, 27),
                    "planned_end": date(2026, 8, 27),
                    "idempotency_key": f"s17-concurrent-no-{index}",
                },
            )
            return task.task_no
        finally:
            close_old_connections()

    numbers = run_parallel(
        lambda: create_worker(1, second_warehouse),
        lambda: create_worker(2, second_warehouse),
    )
    assert len(set(numbers)) == 2

    department_task = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=department,
        key="s17-overlap-department",
    )
    employee_task = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=department,
        employee=employee,
        key="s17-overlap-employee",
    )
    overlap_barrier = Barrier(2)

    def publish_overlap(task_id):
        close_old_connections()
        try:
            overlap_barrier.wait(timeout=10)
            try:
                publish_supply_count_task(
                    task=SupplyCountTask.objects.get(pk=task_id),
                    actor=type(actor).objects.get(pk=equipment.pk),
                )
                return "published"
            except ValidationError:
                return "rejected"
        finally:
            close_old_connections()

    results = run_parallel(
        lambda: publish_overlap(department_task.pk),
        lambda: publish_overlap(employee_task.pk),
    )
    assert sorted(results) == ["published", "rejected"]
    assert SupplyCountTask.objects.filter(
        pk__in=[department_task.pk, employee_task.pk], status="in_progress"
    ).count() == 1
    assert SupplyCountTask.objects.filter(lines__custody=custody).count() == 1
