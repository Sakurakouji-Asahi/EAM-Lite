from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection

from apps.audit.models import AuditLog
from apps.supplies import reconciliation
from apps.supplies.models import (
    SupplyCustody,
    SupplyDocument,
    SupplyStockBalance,
    SupplyStockLedger,
)
from apps.supplies.reconciliation import reconcile_stock_balances
from apps.supplies.services import (
    deactivate_supply_item,
    post_supply_document,
    reverse_supply_document,
    transfer_custody,
    write_off_custody,
)
from tests.test_sprint14_support import make_supply_document
from tests.test_sprint15_services import make_return
from tests.test_sprint15_support import (
    make_company,
    make_department,
    make_employee,
    make_issue_document,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("库存核算并发验收必须在 PostgreSQL 上运行。")


def _parallel(worker, count, *, timeout=30):
    barrier = Barrier(count)

    def wrapped(index):
        close_old_connections()
        try:
            barrier.wait(timeout=timeout)
            return worker(index)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(wrapped, index) for index in range(count)]
        return [future.result(timeout=timeout) for future in futures]


def test_two_distinct_users_cannot_oversell_one_balance():
    _require_postgresql()
    company = make_company("INV-CONC-SELL")
    first_actor = make_user("inventory-race-user-a", "warehouse")
    second_actor = make_user("inventory-race-user-b", "warehouse")
    department = make_department(company, "USE")
    category = make_supply_category(company, "OFFICE")
    warehouse = make_supply_warehouse(company, "MAIN")
    item = make_supply_item(company, category, "PAPER")
    seed_supply_stock(
        actor=first_actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="5",
        unit_cost="3.336667",
        key="inventory-race-opening",
    )
    documents = [
        make_issue_document(
            actor=actor,
            company=company,
            warehouse=warehouse,
            item=item,
            department=department,
            quantity="4",
            key=f"inventory-race-issue-{index}",
        )
        for index, actor in enumerate((first_actor, second_actor), 1)
    ]

    def worker(index):
        actor_id = (first_actor.pk, second_actor.pk)[index]
        try:
            post_supply_document(
                document=SupplyDocument.objects.get(pk=documents[index].pk),
                actor=get_user_model().objects.get(pk=actor_id),
                idempotency_key=documents[index].idempotency_key,
            )
            return "posted"
        except ValidationError as exc:
            return "shortage" if "库存不足" in str(exc) else f"unexpected:{exc}"

    results = _parallel(worker, 2)
    assert sorted(results) == ["posted", "shortage"]
    balance = SupplyStockBalance.objects.get(warehouse=warehouse, item=item)
    assert balance.quantity_on_hand == Decimal("1.0000")
    assert balance.amount_on_hand >= Decimal("0.00")
    assert SupplyStockLedger.objects.filter(movement_type="issue_out").count() == 1
    assert reconcile_stock_balances(company=company).is_consistent


def test_eight_concurrent_retries_post_one_document_once():
    _require_postgresql()
    company = make_company("INV-CONC-POST")
    actor = make_user("inventory-post-retry", "warehouse")
    warehouse = make_supply_warehouse(company, "MAIN")
    category = make_supply_category(company, "OFFICE")
    item = make_supply_item(company, category, "PAPER")
    document = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="7.0000",
        unit_cost="1.234567",
        key="same-document-post-key",
    )

    def worker(_index):
        result = post_supply_document(
            document=SupplyDocument.objects.get(pk=document.pk),
            actor=get_user_model().objects.get(pk=actor.pk),
            idempotency_key=document.idempotency_key,
        )
        return str(result.pk)

    results = _parallel(worker, 8)
    assert len(set(results)) == 1
    document.refresh_from_db()
    assert document.status == "posted"
    assert document.stock_ledgers.count() == 1
    assert AuditLog.objects.filter(
        company=company,
        action="supply_document_post",
        object_id=str(document.pk),
    ).count() == 1
    assert SupplyStockBalance.objects.get(
        warehouse=warehouse,
        item=item,
    ).quantity_on_hand == Decimal("7.0000")


def test_eight_concurrent_reversal_retries_create_one_exact_chain():
    _require_postgresql()
    company = make_company("INV-CONC-REV")
    actor = make_user("inventory-reverse-retry", "warehouse")
    warehouse = make_supply_warehouse(company, "MAIN")
    category = make_supply_category(company, "OFFICE")
    item = make_supply_item(company, category, "PAPER")
    original = seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="3",
        unit_cost="3.336667",
        key="same-reversal-source",
    )
    reversal_key = "same-reversal-request"
    reversal_reason = "并发网络重试"

    def worker(_index):
        result = reverse_supply_document(
            document=SupplyDocument.objects.get(pk=original.pk),
            actor=get_user_model().objects.get(pk=actor.pk),
            idempotency_key=reversal_key,
            reason=reversal_reason,
        )
        return str(result.pk)

    results = _parallel(worker, 8)
    assert len(set(results)) == 1
    original.refresh_from_db()
    assert original.status == "reversed"
    reversal = SupplyDocument.objects.get(reversal_of=original)
    assert reversal.idempotency_key == reversal_key
    assert reversal.stock_ledgers.count() == original.stock_ledgers.count() == 1
    assert SupplyStockBalance.objects.get(
        warehouse=warehouse,
        item=item,
    ).quantity_on_hand == Decimal("0.0000")
    assert reconcile_stock_balances(company=company).is_consistent


def test_return_post_and_source_issue_reversal_have_one_safe_winner_without_deadlock():
    _require_postgresql()
    company = make_company("INV-CONC-LOCK")
    actor = make_user("inventory-lock-order", "warehouse")
    department = make_department(company, "USE")
    category = make_supply_category(company, "OFFICE")
    source = make_supply_warehouse(company, "SOURCE")
    target = make_supply_warehouse(company, "TARGET")
    item = make_supply_item(company, category, "PAPER")
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        quantity="2",
        key="lock-order-opening",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="2",
        key="lock-order-issue",
    )
    post_supply_document(document=issue, actor=actor)
    returned = make_return(
        actor=actor,
        company=company,
        target=target,
        source_line=issue.lines.get(),
        quantity="1",
        key="lock-order-return",
    )

    def worker(index):
        local_actor = get_user_model().objects.get(pk=actor.pk)
        try:
            if index == 0:
                post_supply_document(
                    document=SupplyDocument.objects.get(pk=returned.pk),
                    actor=local_actor,
                    idempotency_key=returned.idempotency_key,
                )
                return "return-posted"
            reverse_supply_document(
                document=SupplyDocument.objects.get(pk=issue.pk),
                actor=local_actor,
                idempotency_key="lock-order-reversal",
                reason="并发锁顺序验证",
            )
            return "issue-reversed"
        except ValidationError:
            return "rejected"

    results = _parallel(worker, 2)
    assert results.count("rejected") == 1
    assert len(set(results) - {"rejected"}) == 1
    assert reconcile_stock_balances(company=company).is_consistent


def test_first_receipt_and_item_deactivation_cannot_commit_an_inactive_live_balance():
    _require_postgresql()
    company = make_company("INV-CONC-OFF")
    actor = make_user("inventory-deactivate-race", "warehouse")
    warehouse = make_supply_warehouse(company, "MAIN")
    category = make_supply_category(company, "OFFICE")
    item = make_supply_item(company, category, "PAPER")
    receipt = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        document_type="receipt",
        quantity="1",
        unit_cost="1",
        key="deactivate-race-receipt",
    )

    def worker(index):
        local_actor = get_user_model().objects.get(pk=actor.pk)
        local_item = type(item).objects.get(pk=item.pk)
        try:
            if index == 0:
                post_supply_document(
                    document=SupplyDocument.objects.get(pk=receipt.pk),
                    actor=local_actor,
                    idempotency_key=receipt.idempotency_key,
                )
                return "posted"
            deactivate_supply_item(
                actor=local_actor,
                item=local_item,
                reason="并发停用验证",
            )
            return "deactivated"
        except ValidationError:
            return "rejected"

    results = _parallel(worker, 2)
    assert results.count("rejected") == 1
    item.refresh_from_db()
    balance = SupplyStockBalance.objects.filter(warehouse=warehouse, item=item).first()
    if balance is None:
        assert results.count("deactivated") == 1
        assert not item.is_active
    else:
        assert results.count("posted") == 1
        assert item.is_active
        assert balance.quantity_on_hand == Decimal("1.0000")
    assert reconcile_stock_balances(company=company).is_consistent


def test_repeatable_read_reconcile_does_not_block_or_deadlock_concurrent_post(monkeypatch):
    _require_postgresql()
    company = make_company("INV-CONC-RECON")
    actor = make_user("inventory-reconcile-race", "warehouse")
    warehouse = make_supply_warehouse(company, "MAIN")
    category = make_supply_category(company, "OFFICE")
    item = make_supply_item(company, category, "PAPER")
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="2",
        unit_cost="10",
        key="reconcile-race-opening",
    )
    receipt = make_supply_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        document_type="receipt",
        quantity="1",
        unit_cost="20",
        key="reconcile-race-receipt",
    )
    reached_second_phase = Event()
    release_reconcile = Event()
    original_document_integrity = reconciliation._document_integrity

    def paused_document_integrity(*args, **kwargs):
        reached_second_phase.set()
        assert release_reconcile.wait(20)
        return original_document_integrity(*args, **kwargs)

    monkeypatch.setattr(
        reconciliation,
        "_document_integrity",
        paused_document_integrity,
    )

    def run_reconcile():
        close_old_connections()
        try:
            local_company = type(company).objects.get(pk=company.pk)
            return reconciliation.reconcile_stock_balances(company=local_company)
        finally:
            close_old_connections()

    def run_post():
        close_old_connections()
        try:
            return post_supply_document(
                document=SupplyDocument.objects.get(pk=receipt.pk),
                actor=get_user_model().objects.get(pk=actor.pk),
                idempotency_key=receipt.idempotency_key,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reconcile_future = pool.submit(run_reconcile)
        assert reached_second_phase.wait(20)
        post_future = pool.submit(run_post)
        # A table SHARE lock would block here and create the lock-order cycle
        # found in review. MVCC lets the writer commit while reconciliation is
        # paused on its older, internally consistent snapshot.
        assert post_future.result(timeout=20).status == "posted"
        release_reconcile.set()
        snapshot_result = reconcile_future.result(timeout=20)

    assert snapshot_result.is_consistent
    assert reconcile_stock_balances(company=company).is_consistent


@pytest.mark.parametrize("custody_action", ("transfer", "loss"))
def test_item_deactivation_and_custody_action_share_one_lock_order(custody_action):
    _require_postgresql()
    company = make_company(f"INV-CONC-CUST-{custody_action.upper()}")
    actor = make_user(f"inventory-custody-{custody_action}", "warehouse")
    department = make_department(company, "SOURCE-DEPT")
    employee = make_employee(company, department, "SOURCE-EMP")
    category = make_supply_category(company, "DURABLE")
    warehouse = make_supply_warehouse(company, "MAIN")
    item = make_supply_item(
        company,
        category,
        "CHAIR",
        item_type="durable_quantity",
        unit="把",
    )
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="1",
        unit_cost="80",
        key=f"custody-deactivate-{custody_action}-opening",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        department=department,
        employee=employee,
        quantity="1",
        key=f"custody-deactivate-{custody_action}-issue",
    )
    post_supply_document(document=issue, actor=actor)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    target_department = make_department(company, "TARGET-DEPT")
    target_employee = make_employee(company, target_department, "TARGET-EMP")

    def worker(index):
        local_actor = get_user_model().objects.get(pk=actor.pk)
        local_item = type(item).objects.get(pk=item.pk)
        local_custody = SupplyCustody.objects.get(pk=custody.pk)
        try:
            if index == 1:
                deactivate_supply_item(
                    actor=local_actor,
                    item=local_item,
                    reason="并发停用锁序验证",
                )
                return "deactivated"
            if custody_action == "transfer":
                transfer_custody(
                    custody=local_custody,
                    quantity=Decimal("1"),
                    target_department=type(target_department).objects.get(
                        pk=target_department.pk
                    ),
                    target_employee=type(target_employee).objects.get(
                        pk=target_employee.pk
                    ),
                    business_date=issue.business_date,
                    reason="并发转交锁序验证",
                    actor=local_actor,
                    idempotency_key="custody-deactivate-transfer",
                )
                return "custody-action"
            write_off_custody(
                custody=local_custody,
                quantity=Decimal("1"),
                action="loss",
                business_date=issue.business_date,
                reason="并发报损锁序验证",
                actor=local_actor,
                idempotency_key="custody-deactivate-loss",
            )
            return "custody-action"
        except ValidationError:
            return "rejected"

    results = _parallel(worker, 2)
    assert "custody-action" in results
    item.refresh_from_db()
    open_custodies = SupplyCustody.objects.filter(
        company=company,
        item=item,
        status="open",
        current_quantity__gt=0,
    )
    if item.is_active:
        assert results.count("rejected") == 1
    else:
        assert not open_custodies.exists()
