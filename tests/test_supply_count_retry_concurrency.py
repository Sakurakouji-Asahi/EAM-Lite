"""PostgreSQL count creation retries remain read-only during task transitions."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier, Event, local

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connection

from apps.supplies import services
from apps.supplies.models import (
    SupplyCountTask,
    SupplyDocument,
    SupplyDocumentSequence,
    SupplyStockBalance,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import seed_supply_stock


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Count retry lock compatibility requires PostgreSQL.")


def count_data(warehouse):
    return {
        "name": "盘点创建幂等重试", "count_domain": "warehouse_stock",
        "warehouse": warehouse, "planned_start": date(2026, 8, 27),
        "planned_end": date(2026, 8, 28), "idempotency_key": "count-create-retry",
        "remark": "原始创建内容",
    }


def run_count_request(state, kind, callback):
    close_old_connections()
    state.kind = kind
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '8s'")
            cursor.execute("SET statement_timeout = '15s'")
        try:
            task = callback()
            return {"request": kind, "result": "success", "task_id": str(task.pk), "status": task.status}
        except ValidationError as exc:
            return {"request": kind, "result": "validation-error", "messages": exc.messages}
        except OperationalError as exc:
            return {"request": kind, "result": "database-error",
                    "sqlstate": getattr(exc.__cause__, "sqlstate", None), "detail": str(exc)}
    finally:
        close_old_connections()


@pytest.mark.parametrize("conflicting_content", [False, True], ids=["matching-retry", "conflicting-retry"])
def test_create_retry_and_close_both_finish_without_count_company_deadlock(monkeypatch, conflicting_content):
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor, company=company, warehouse=warehouse, item=item,
        quantity="5", unit_cost="10", key="count-create-retry-seed",
    )
    data = count_data(warehouse)
    task = services.create_supply_count_task(actor=actor, company=company, data=data)
    services.publish_supply_count_task(task=task, actor=actor)
    services.record_supply_count(
        line=task.lines.get(), counted_quantity=Decimal("6"),
        remark="盘盈一单位", actor=actor,
    )
    services.stop_supply_count_entry(task=task, actor=actor)
    task_held = Event()
    company_held = Event()
    state = local()
    create_adjustment = services._create_count_adjustment_document

    def observe_close_before_adjustment(**kwargs):
        if getattr(state, "kind", None) == "close":
            # Both implementations reach this point with the real Task/Line locks.
            task_held.set()
            assert company_held.wait(timeout=8), "Create retry did not acquire Company."
        return create_adjustment(**kwargs)

    def observe_actual_company_lock(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if ('FROM "masterdata_company"' in sql and "FOR UPDATE" in sql
                and getattr(state, "kind", None) == "create-retry"):
            # Observe the completed business query, without adding a lock.
            company_held.set()
        return result

    def close_request():
        return services.close_supply_count_task(
            task=SupplyCountTask.objects.get(pk=task.pk),
            actor=type(actor).objects.get(pk=actor.pk),
        )

    def retry_request():
        assert task_held.wait(timeout=8), "Close did not acquire Task."
        local_data = {**data, "warehouse": type(warehouse).objects.get(pk=warehouse.pk)}
        if conflicting_content:
            local_data["name"] = "不同的盘点任务名称"
        with connection.execute_wrapper(observe_actual_company_lock):
            return services.create_supply_count_task(
                actor=type(actor).objects.get(pk=actor.pk),
                company=type(company).objects.get(pk=company.pk), data=local_data,
            )

    with monkeypatch.context() as patch:
        patch.setattr(services, "_create_count_adjustment_document", observe_close_before_adjustment)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_count_request, state, kind, callback) for kind, callback in (
                ("close", close_request), ("create-retry", retry_request),
            )]
            results = [future.result(timeout=25) for future in futures]
    print(json.dumps({"count_close_and_retry": results}, ensure_ascii=False))
    assert task_held.is_set() and company_held.is_set()
    assert results[0]["result"] == "success", results
    assert results[0]["status"] == "closed"
    if conflicting_content:
        assert results[1]["result"] == "validation-error", results
        assert results[1]["messages"] == ["同一盘点任务幂等键已用于不同内容。"]
    else:
        assert results[1]["result"] == "success", results
        assert results[1]["task_id"] == str(task.pk)
    task.refresh_from_db()
    assert task.status == "closed"
    assert task.name == data["name"] and task.remark == data["remark"]
    assert SupplyCountTask.objects.filter(company=company, idempotency_key=data["idempotency_key"]).count() == 1
    assert SupplyStockBalance.objects.get(warehouse=warehouse, item=item).quantity_on_hand == Decimal("6")
    assert SupplyDocument.objects.filter(source_count_task=task).count() == 1
    adjustment = SupplyDocument.objects.get(source_count_task=task)
    assert adjustment.status == "posted" and adjustment.stock_ledgers.count() == 1
    # Replaying either completed action must not create another document or ledger.
    assert services.create_supply_count_task(actor=actor, company=company, data=data).pk == task.pk
    assert services.close_supply_count_task(task=task, actor=actor).pk == task.pk
    assert SupplyDocument.objects.filter(source_count_task=task).count() == 1
    assert adjustment.stock_ledgers.count() == 1


def test_concurrent_first_creates_with_same_key_share_one_task_and_number():
    require_postgresql()
    company, actor, _, _, warehouse, _, _, _ = supply_context()
    data = count_data(warehouse)
    barrier = Barrier(2)
    state = local()

    def create_request():
        local_actor = type(actor).objects.get(pk=actor.pk)
        local_company = type(company).objects.get(pk=company.pk)
        local_data = {**data, "warehouse": type(warehouse).objects.get(pk=warehouse.pk)}
        barrier.wait(timeout=8)
        return services.create_supply_count_task(actor=local_actor, company=local_company, data=local_data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_count_request, state, "first-create", create_request) for _ in range(2)]
        results = [future.result(timeout=25) for future in futures]
    assert [result["result"] for result in results] == ["success", "success"], results
    assert len({result["task_id"] for result in results}) == 1
    task = SupplyCountTask.objects.get(company=company, idempotency_key=data["idempotency_key"])
    assert results[0]["task_id"] == str(task.pk) and task.status == "draft"
    assert task.lines.count() == 0
    assert SupplyCountTask.objects.filter(company=company).count() == 1
    assert SupplyDocumentSequence.objects.get(company=company, sequence_type="count_task", year=2026).current_value == 1
    assert not SupplyDocument.objects.filter(company=company).exists()
    assert not SupplyStockBalance.objects.filter(company=company).exists()
