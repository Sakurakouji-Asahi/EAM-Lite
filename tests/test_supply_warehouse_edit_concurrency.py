"""Isolated PostgreSQL probes: real warehouse service statements and commits."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, close_old_connections, connection

from apps.audit.models import AuditLog
from apps.supplies import services
from apps.supplies.models import SupplyWarehouse
from tests.test_sprint3_support import make_company, make_user

pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Warehouse commit lock probes require PostgreSQL.")


def worker(state, role, callback, pids, wrapper=None):
    close_old_connections()
    state.role = role
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '8s'")
            cursor.execute("SET statement_timeout = '15s'")
            cursor.execute("SELECT pg_backend_pid()")
            pids[role] = cursor.fetchone()[0]
        def call():
            try:
                result = callback()
                return {"role": role, "result": "success", "code": result.code}
            except ValidationError as exc:
                return {"role": role, "result": "validation-error", "messages": exc.messages}
            except DatabaseError as exc:
                return {"role": role, "result": "database-error",
                        "sqlstate": getattr(exc.__cause__, "sqlstate", None), "detail": str(exc)}
        if wrapper is None:
            return call()
        with connection.execute_wrapper(wrapper):
            return call()
    finally:
        close_old_connections()


def activity(pids):
    identifiers = list(pids.values())
    if not identifiers:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pid, pg_blocking_pids(pid), state, wait_event_type, wait_event, query "
            "FROM pg_stat_activity WHERE datname = current_database() AND pid = ANY(%s)",
            [identifiers],
        )
        return [dict(zip(("pid", "blocking_pids", "state", "wait_type", "wait_event", "query"), row))
                for row in cursor.fetchall()]


def wait_for_block(pids, waiter, holder, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = activity(pids)
        if any(row["pid"] == pids[waiter] and pids[holder] in row["blocking_pids"] for row in rows):
            return rows
        time.sleep(0.02)
    raise AssertionError(f"Real {waiter} statement did not block on {holder}: {activity(pids)}")


def emit(name, results, evidence):
    print("WAREHOUSE_LOCK_EVIDENCE=" + json.dumps(
        {"case": name, "results": results, "evidence": evidence}, ensure_ascii=False, default=str,
    ), flush=True)


def test_manual_edit_and_automatic_insert_finish_without_fk_unique_deadlock(monkeypatch):
    require_postgresql()
    company = make_company("WAREHOUSE-COMMIT-RACE")
    actor = make_user("warehouse-commit-race", "warehouse")
    original = services.create_supply_warehouse(actor=actor, company=company, data={"name": "测试原仓库"})
    state = local()
    pids = {}
    manual_validated = Event()
    auto_inserted = Event()
    manual_update_started = Event()
    allow_auto_commit = Event()
    real_clean = SupplyWarehouse.full_clean
    real_save = SupplyWarehouse.save

    def clean(instance, *args, **kwargs):
        result = real_clean(instance, *args, **kwargs)
        if getattr(state, "role", None) == "manual":
            # The original service SELECT FOR UPDATE and real validation already ran.
            manual_validated.set()
            assert auto_inserted.wait(8), "Automatic real INSERT was not reached."
        return result

    def save(instance, *args, **kwargs):
        result = real_save(instance, *args, **kwargs)
        if getattr(state, "role", None) == "automatic":
            # Pause only after the original INSERT returned, inside its real transaction.
            auto_inserted.set()
            assert allow_auto_commit.wait(8), "Manual UPDATE did not reach its real index wait."
        return result

    def observe(execute, sql, params, many, context):
        if getattr(state, "role", None) == "manual" and sql.startswith('UPDATE "supplies_supplywarehouse"'):
            manual_update_started.set()
        return execute(sql, params, many, context)

    monkeypatch.setattr(SupplyWarehouse, "full_clean", clean)
    monkeypatch.setattr(SupplyWarehouse, "save", save)

    def auto_request():
        assert manual_validated.wait(8), "Manual service did not complete real validation."
        return services.create_supply_warehouse(actor=actor, company=company, data={"name": "测试自动提交仓库"})

    evidence = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        manual = executor.submit(worker, state, "manual", lambda: services.update_supply_warehouse(
            actor=actor, warehouse=original, data={"code": "WH000002"}), pids, observe)
        automatic = executor.submit(worker, state, "automatic", auto_request, pids, observe)
        try:
            assert manual_update_started.wait(8), "Manual original UPDATE was not submitted."
            evidence["manual_update_waiting_on_insert"] = wait_for_block(pids, "manual", "automatic")
            allow_auto_commit.set()
            # The before implementation has a genuine FK/index cycle; the fixed one commits.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (manual.done() and automatic.done()):
                rows = activity(pids)
                waits = {row["pid"]: row["blocking_pids"] for row in rows}
                if (pids["automatic"] in waits.get(pids["manual"], [])
                        and pids["manual"] in waits.get(pids["automatic"], [])):
                    evidence["mutual_blocking_cycle"] = rows
                    break
                time.sleep(0.02)
        finally:
            allow_auto_commit.set()
        results = [manual.result(20), automatic.result(20)]
    emit("fk_unique_commit", results, evidence)
    assert not [row for row in results if row["result"] == "database-error"], results
    assert results[0]["result"] == "validation-error", results
    assert results[1] == {"role": "automatic", "result": "success", "code": "WH000002"}
    original.refresh_from_db()
    assert original.code == "WH000001"
    assert set(SupplyWarehouse.objects.values_list("code", flat=True)) == {"WH000001", "WH000002"}
    assert sorted(AuditLog.objects.filter(action="supply_warehouse_create").values_list(
        "new_data_json__code", flat=True)) == ["WH000001", "WH000002"]
    assert not AuditLog.objects.filter(action="supply_warehouse_update").exists()


def test_real_warehouse_edits_remain_serialized_and_audit_observes_committed_change(monkeypatch):
    require_postgresql()
    company = make_company("WAREHOUSE-EDIT-MUTEX")
    actor = make_user("warehouse-edit-mutex", "warehouse")
    original = services.create_supply_warehouse(actor=actor, company=company, data={"name": "测试互斥仓库"})
    state = local()
    pids = {}
    first_held = Event()
    second_query = Event()
    release_first = Event()
    real_clean = SupplyWarehouse.full_clean

    def clean(instance, *args, **kwargs):
        result = real_clean(instance, *args, **kwargs)
        if getattr(state, "role", None) == "first":
            first_held.set()
            assert release_first.wait(8), "Second real edit did not block on the warehouse."
        return result

    def observe(execute, sql, params, many, context):
        if (getattr(state, "role", None) == "second" and "FOR " in sql and "UPDATE" in sql
                and ('FROM "supplies_supplywarehouse"' in sql or 'FROM "masterdata_company"' in sql)):
            second_query.set()
        return execute(sql, params, many, context)

    monkeypatch.setattr(SupplyWarehouse, "full_clean", clean)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(worker, state, "first", lambda: services.update_supply_warehouse(
            actor=actor, warehouse=original, data={"remark": "第一笔真实修改"}), pids, observe)
        assert first_held.wait(8), "First service did not acquire and validate the warehouse."
        second = executor.submit(worker, state, "second", lambda: services.update_supply_warehouse(
            actor=actor, warehouse=original, data={"remark": "第二笔真实修改"}), pids, observe)
        try:
            assert second_query.wait(8), "Second original locking query was not submitted."
            evidence = wait_for_block(pids, "second", "first")
            assert not second.done()
        finally:
            release_first.set()
        results = [first.result(20), second.result(20)]
    emit("warehouse_edit_mutex", results, evidence)
    assert [row["result"] for row in results] == ["success", "success"]
    original.refresh_from_db()
    assert original.remark == "第二笔真实修改"
    updates = list(AuditLog.objects.filter(action="supply_warehouse_update").order_by("created_at", "pk"))
    assert len(updates) == 2
    assert updates[0].old_data_json["remark"] == ""
    assert updates[0].new_data_json["remark"] == "第一笔真实修改"
    assert updates[1].old_data_json["remark"] == "第一笔真实修改"
    assert updates[1].new_data_json["remark"] == "第二笔真实修改"


def test_manual_edit_wins_number_and_automatic_retries_without_phantom_audit(monkeypatch):
    require_postgresql()
    company = make_company("WAREHOUSE-EDIT-RETRY")
    actor = make_user("warehouse-edit-retry", "warehouse")
    original = services.create_supply_warehouse(actor=actor, company=company, data={"name": "测试先提交仓库"})
    state = local()
    pids = {}
    automatic_validated = Event()
    manual_committed = Event()
    attempts = []
    conflicts = []
    real_clean = SupplyWarehouse.full_clean
    real_save = SupplyWarehouse.save

    def clean(instance, *args, **kwargs):
        result = real_clean(instance, *args, **kwargs)
        if getattr(state, "role", None) == "automatic" and instance.code == "WH000002":
            automatic_validated.set()
            assert manual_committed.wait(8), "Manual real edit did not commit."
        return result

    def save(instance, *args, **kwargs):
        if getattr(state, "role", None) != "automatic":
            return real_save(instance, *args, **kwargs)
        attempts.append(instance.code)
        try:
            return real_save(instance, *args, **kwargs)
        except IntegrityError as exc:
            conflicts.append(exc.__cause__.diag.constraint_name)
            raise

    monkeypatch.setattr(SupplyWarehouse, "full_clean", clean)
    monkeypatch.setattr(SupplyWarehouse, "save", save)

    def manual_request():
        try:
            assert automatic_validated.wait(8), "Automatic real validation did not finish."
            return services.update_supply_warehouse(actor=actor, warehouse=original, data={
                "code": "WH000002", "remark": "手填先提交内容"})
        finally:
            manual_committed.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        automatic = executor.submit(worker, state, "automatic", lambda: services.create_supply_warehouse(
            actor=actor, company=company, data={"name": "测试自动重试仓库"}), pids)
        manual = executor.submit(worker, state, "manual", manual_request, pids)
        results = [manual.result(20), automatic.result(20)]
    emit("manual_edit_auto_retry", results, {"insert_attempts": attempts, "conflicts": conflicts})
    assert results == [{"role": "manual", "result": "success", "code": "WH000002"},
                       {"role": "automatic", "result": "success", "code": "WH000003"}]
    assert attempts == ["WH000002", "WH000003"]
    assert conflicts == ["uq_supply_warehouse_company_code"]
    original.refresh_from_db()
    assert original.code == "WH000002" and original.remark == "手填先提交内容"
    assert set(SupplyWarehouse.objects.values_list("normalized_code", flat=True)) == {"wh000002", "wh000003"}
    assert sorted(AuditLog.objects.filter(action="supply_warehouse_create").values_list(
        "new_data_json__code", flat=True)) == ["WH000001", "WH000003"]
    update = AuditLog.objects.get(action="supply_warehouse_update")
    assert update.old_data_json["code"] == "WH000001" and update.new_data_json["code"] == "WH000002"
    assert update.new_data_json["remark"] == "手填先提交内容"

from datetime import date
from decimal import Decimal
from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from tests.test_sprint15_services import supply_context


def test_warehouse_code_edit_and_real_receipt_draft_both_commit_without_fk_deadlock(monkeypatch):
    require_postgresql()
    company, actor, _, _, _, warehouse, item, _ = supply_context()
    state = local()
    pids = {}
    manual_held = Event()
    document_company_requested = Event()
    document_company_held = Event()
    release_manual = Event()
    allow_document = Event()
    real_clean = type(warehouse).full_clean
    before_balances = list(SupplyStockBalance.objects.values())
    before_ledgers = SupplyStockLedger.objects.count()

    def clean(instance, *args, **kwargs):
        result = real_clean(instance, *args, **kwargs)
        if getattr(state, "role", None) == "manual":
            # The original warehouse locking query and full validation already completed.
            manual_held.set()
            assert release_manual.wait(8), "Document's actual Company lock was not observed."
        return result

    def observe(execute, sql, params, many, context):
        relevant = (getattr(state, "role", None) == "document"
                    and 'FROM "masterdata_company"' in sql and "FOR UPDATE" in sql)
        if relevant:
            document_company_requested.set()
        result = execute(sql, params, many, context)
        if relevant:
            document_company_held.set()
            assert allow_document.wait(8), "Manual's actual commit wait was not observed."
        return result

    def request(role, callback):
        close_old_connections()
        state.role = role
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '8s'")
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[role] = cursor.fetchone()[0]
            with connection.execute_wrapper(observe):
                try:
                    result = callback()
                    return {"role": role, "result": "success", "pk": str(result.pk)}
                except ValidationError as exc:
                    return {"role": role, "result": "validation-error", "messages": exc.messages}
                except DatabaseError as exc:
                    return {"role": role, "result": "database-error",
                            "sqlstate": getattr(exc.__cause__, "sqlstate", None), "detail": str(exc)}
        finally:
            close_old_connections()

    def document_request():
        return services.create_supply_document(
            actor=actor, company=company, document_type="receipt",
            data={"business_date": date(2026, 10, 3), "target_warehouse": warehouse,
                  "idempotency_key": "warehouse-edit-document-lock-order"},
            lines=[{"item": item, "quantity": Decimal("1"), "entered_unit_cost": Decimal("3.50")}],
        )

    monkeypatch.setattr(type(warehouse), "full_clean", clean)
    evidence = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        manual = executor.submit(request, "manual", lambda: services.update_supply_warehouse(
            actor=actor, warehouse=warehouse, data={"code": "WAREHOUSE-RENAMED", "remark": "真实仓库修改"}))
        assert manual_held.wait(8), "Manual did not obtain its real warehouse lock."
        document = executor.submit(request, "document", document_request)
        try:
            assert document_company_requested.wait(8), "Receipt creator did not submit its real Company query."
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                rows = activity(pids)
                if document_company_held.is_set():
                    evidence["document_company_acquired_while_manual_holds_warehouse"] = rows
                    release_manual.set()
                    evidence["manual_commit_waiting_on_document"] = wait_for_block(pids, "manual", "document")
                    allow_document.set()
                    cycle_deadline = time.monotonic() + 3
                    while time.monotonic() < cycle_deadline and not (manual.done() and document.done()):
                        cycle_rows = activity(pids)
                        waits = {row["pid"]: row["blocking_pids"] for row in cycle_rows}
                        if (pids["manual"] in waits.get(pids["document"], [])
                                and pids["document"] in waits.get(pids["manual"], [])):
                            evidence["mutual_blocking_cycle"] = cycle_rows
                            break
                        time.sleep(0.02)
                    break
                if any(row["pid"] == pids["document"] and pids["manual"] in row["blocking_pids"] for row in rows):
                    evidence["document_company_query_waiting_on_manual"] = rows
                    # Safe Company-first order: release real manual work before document resumes.
                    release_manual.set()
                    allow_document.set()
                    break
                time.sleep(0.02)
            else:
                raise AssertionError(f"Neither actual Company lock nor its real wait observed: {activity(pids)}")
        finally:
            release_manual.set()
            allow_document.set()
        results = [manual.result(20), document.result(20)]
    emit("warehouse_edit_receipt_draft", results, evidence)
    assert [result["result"] for result in results] == ["success", "success"], results
    warehouse.refresh_from_db()
    assert warehouse.code == "WAREHOUSE-RENAMED" and warehouse.remark == "真实仓库修改"
    receipt = SupplyDocument.objects.get(idempotency_key="warehouse-edit-document-lock-order")
    assert receipt.status == "draft" and receipt.target_warehouse_id == warehouse.pk
    assert receipt.lines.count() == 1
    assert receipt.lines.get().item_id == item.pk and receipt.lines.get().quantity == Decimal("1")
    assert list(SupplyStockBalance.objects.values()) == before_balances
    assert SupplyStockLedger.objects.count() == before_ledgers
    assert AuditLog.objects.filter(action="supply_warehouse_update").count() == 1
    assert AuditLog.objects.filter(action="supply_document_create").count() == 1

