"""PostgreSQL draft FK compatibility without weakening custody write exclusion."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Event, local
from time import monotonic, sleep

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connection

from apps.supplies import services
from apps.supplies.models import (
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyDocument,
    SupplyStockBalance,
    SupplyStockLedger,
)
from tests.test_sprint16_services import issued_custody


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Draft FK and custody write exclusion require PostgreSQL.")


def stock_snapshot():
    return list(SupplyStockBalance.objects.order_by("pk").values(
        "pk", "warehouse_id", "item_id", "quantity_on_hand", "amount_on_hand", "average_unit_cost",
    ))


def custody_snapshot(custody):
    custody.refresh_from_db()
    return (custody.current_quantity, custody.current_amount, custody.status)


def run_request(state, kind, callback):
    close_old_connections()
    state.kind = kind
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '8s'")
            cursor.execute("SET statement_timeout = '15s'")
            cursor.execute("SELECT pg_backend_pid()")
            state.pid = cursor.fetchone()[0]
        try:
            return {"request": kind, "created": str(callback().pk)}
        except OperationalError as exc:
            return {"request": kind, "sqlstate": getattr(exc.__cause__, "sqlstate", None), "error": str(exc)}
    finally:
        close_old_connections()


@pytest.mark.parametrize("document_type", ["receipt", "return"])
def test_return_draft_and_normal_create_both_finish_without_fk_deadlock(monkeypatch, document_type):
    require_postgresql()
    company, actor, _, _, _, target, item, _, custody = issued_custody(quantity="2", unit_cost="80")
    before_documents = SupplyDocument.objects.count()
    before_stock = stock_snapshot()
    before_custody = custody_snapshot(custody)
    before_ledgers = SupplyStockLedger.objects.count()
    before_movements = SupplyCustodyMovement.objects.count()
    return_locked = Event()
    creator_locked = Event()
    state = local()
    required_values = services._required_custody_action_values
    create_lines = services._create_document_lines

    def observe_return_locks(**kwargs):
        result = required_values(**kwargs)
        if getattr(state, "kind", None) == "custody-return":
            # The return already holds its real item and custody locks here,
            # before calling the normal document creator in both implementations.
            return_locked.set()
            assert creator_locked.wait(timeout=8), "Normal creator did not reach its Company lock."
        return result

    def observe_creator_lock(*, document, prepared_lines):
        if getattr(state, "kind", None) == "normal-create":
            creator_locked.set()  # Reached only after the real Company lock.
        return create_lines(document=document, prepared_lines=prepared_lines)

    def custody_return():
        return services.return_custody_to_warehouse(
            custody=SupplyCustody.objects.get(pk=custody.pk),
            target_warehouse=type(target).objects.get(pk=target.pk),
            quantity=Decimal("1"), business_date=date(2026, 8, 26),
            reason="归还草稿并发", actor=type(actor).objects.get(pk=actor.pk),
            idempotency_key="concurrent-custody-return",
        )

    def normal_create():
        assert return_locked.wait(timeout=8), "Return did not reach its custody lock."
        line = {"item": type(item).objects.get(pk=item.pk), "quantity": Decimal("1")}
        data = {
            "business_date": date(2026, 8, 26), "target_warehouse": type(target).objects.get(pk=target.pk),
            "idempotency_key": f"concurrent-normal-{document_type}",
        }
        if document_type == "receipt":
            line["entered_unit_cost"] = Decimal("80")
        else:
            local_custody = SupplyCustody.objects.get(pk=custody.pk)
            data["remark"] = "普通退回草稿并发"
            line.update({"entered_unit_cost": None, "source_custody": local_custody,
                         "source_issue_line": local_custody.origin_issue_line, "line_remark": data["remark"]})
        return services.create_supply_document(
            actor=type(actor).objects.get(pk=actor.pk), company=type(company).objects.get(pk=company.pk),
            document_type=document_type, data=data, lines=[line],
        )

    with monkeypatch.context() as patch:
        patch.setattr(services, "_required_custody_action_values", observe_return_locks)
        patch.setattr(services, "_create_document_lines", observe_creator_lock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_request, state, kind, callback) for kind, callback in (
                ("custody-return", custody_return), ("normal-create", normal_create),
            )]
            results = [future.result(timeout=25) for future in futures]
    print(json.dumps({"document_type": document_type, "requests": results}, ensure_ascii=False))
    assert all("created" in result for result in results), results
    assert len({result["created"] for result in results}) == 2
    created = SupplyDocument.objects.filter(pk__in=[result["created"] for result in results])
    assert created.count() == 2
    assert all(document.status == "draft" for document in created)
    assert len(set(created.values_list("document_no", flat=True))) == 2

    # Each normal service retry returns its original draft, without a new number.
    assert str(custody_return().pk) == results[0]["created"]
    assert str(normal_create().pk) == results[1]["created"]
    assert SupplyDocument.objects.count() == before_documents + 2
    assert stock_snapshot() == before_stock
    assert custody_snapshot(custody) == before_custody
    assert SupplyStockLedger.objects.count() == before_ledgers
    assert SupplyCustodyMovement.objects.count() == before_movements


def test_return_draft_still_blocks_real_custody_consumption_until_commit(monkeypatch):
    require_postgresql()
    _, actor, _, _, _, target, _, _, custody = issued_custody(quantity="2", unit_cost="80")
    before_stock = stock_snapshot()
    before_custody = custody_snapshot(custody)
    before_ledgers = SupplyStockLedger.objects.count()
    before_movements = SupplyCustodyMovement.objects.count()
    held = Event()
    attempted = Event()
    release = Event()
    pids = {}
    state = local()
    required_values = services._required_custody_action_values
    lock_item = services._lock_custody_item

    def observe_return_locks(**kwargs):
        result = required_values(**kwargs)
        if getattr(state, "kind", None) == "custody-return":
            pids["return"] = state.pid
            held.set()
            assert release.wait(timeout=8), "Return was not released after the write exclusion check."
        return result

    def observe_consumption_attempt(local_custody):
        if getattr(state, "kind", None) == "writeoff":
            pids["writeoff"] = state.pid
            attempted.set()
        return lock_item(local_custody)

    def custody_return():
        return services.return_custody_to_warehouse(
            custody=SupplyCustody.objects.get(pk=custody.pk),
            target_warehouse=type(target).objects.get(pk=target.pk),
            quantity=Decimal("1"), business_date=date(2026, 8, 26), reason="消费前归还草稿",
            actor=type(actor).objects.get(pk=actor.pk), idempotency_key="return-before-consumption",
        )

    def writeoff():
        return services.write_off_custody(
            custody=SupplyCustody.objects.get(pk=custody.pk), quantity=Decimal("2"),
            action="loss", business_date=date(2026, 8, 26), reason="真实消费互斥验证",
            actor=type(actor).objects.get(pk=actor.pk), idempotency_key="consume-after-return-draft",
        )

    with monkeypatch.context() as patch:
        patch.setattr(services, "_required_custody_action_values", observe_return_locks)
        patch.setattr(services, "_lock_custody_item", observe_consumption_attempt)
        with ThreadPoolExecutor(max_workers=2) as pool:
            return_future = pool.submit(run_request, state, "custody-return", custody_return)
            try:
                assert held.wait(timeout=8)
                loss_future = pool.submit(run_request, state, "writeoff", writeoff)
                assert attempted.wait(timeout=8)
                blocked = False
                deadline = monotonic() + 3
                while monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_blocking_pids(%s)", [pids["writeoff"]])
                        blocked = pids["return"] in cursor.fetchone()[0]
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked, "PostgreSQL did not block the real custody write behind the return."
                assert not loss_future.done()
                assert stock_snapshot() == before_stock
                assert custody_snapshot(custody) == before_custody
            finally:
                release.set()
            results = [return_future.result(timeout=25), loss_future.result(timeout=25)]
    assert all("created" in result for result in results), results
    assert SupplyDocument.objects.get(pk=results[0]["created"]).status == "draft"
    movement = SupplyCustodyMovement.objects.get(pk=results[1]["created"])
    assert movement.action == "loss" and movement.quantity == Decimal("2") and movement.amount == Decimal("160")
    assert custody_snapshot(custody) == (Decimal("0"), Decimal("0"), "closed")
    assert stock_snapshot() == before_stock
    assert SupplyStockLedger.objects.count() == before_ledgers
    assert SupplyCustodyMovement.objects.count() == before_movements + 1
    assert writeoff().pk == movement.pk
    assert SupplyCustodyMovement.objects.count() == before_movements + 1

    # A saved draft reserves nothing: posting must revalidate the now-consumed custody.
    with pytest.raises(ValidationError):
        services.post_supply_document(
            document=SupplyDocument.objects.get(pk=results[0]["created"]), actor=actor,
        )
    assert SupplyDocument.objects.get(pk=results[0]["created"]).status == "draft"
    assert SupplyStockLedger.objects.count() == before_ledgers
    assert custody_snapshot(custody) == (Decimal("0"), Decimal("0"), "closed")
