import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Event

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction

from apps.masterdata.models import Company
from apps.supplies import reconciliation
from apps.supplies.models import SupplyCustody, SupplyDocument, SupplyStockBalance
from apps.supplies.reconciliation import (
    rebuild_custodies,
    rebuild_stock_balances,
    reconcile_custodies,
    reconcile_stock_balances,
)
from apps.supplies.services import (
    _update_balance_values,
    _update_custody_values,
    create_supply_document,
    post_supply_document,
    write_off_custody,
)
from tests.test_sprint18_rebuild import rebuild_context


pytestmark = pytest.mark.django_db(transaction=True)


def _thread(callable_):
    close_old_connections()
    try:
        return callable_()
    finally:
        close_old_connections()


def test_stock_rebuild_serializes_normal_posting_and_remains_consistent(monkeypatch):
    company, actor = rebuild_context()
    balance = SupplyStockBalance.objects.get(company=company)
    with transaction.atomic():
        _update_balance_values(
            balance=balance,
            quantity=Decimal("1.0000"),
            amount=Decimal("80.00"),
            average_unit_cost=Decimal("80.000000"),
            updated_at=balance.updated_at,
        )
    receipt = create_supply_document(
        actor=actor,
        company=company,
        document_type="receipt",
        data={
            "business_date": date(2026, 8, 27),
            "target_warehouse": balance.warehouse,
            "idempotency_key": "s18-concurrent-receipt",
        },
        lines=[
            {
                "item": balance.item,
                "quantity": Decimal("1.0000"),
                "entered_unit_cost": Decimal("80.000000"),
            }
        ],
    )
    entered = Event()
    release = Event()
    original = reconciliation._update_balance_values

    def paused_update(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)

    monkeypatch.setattr(reconciliation, "_update_balance_values", paused_update)

    def run_rebuild():
        local_company = Company.objects.get(pk=company.pk)
        local_actor = get_user_model().objects.get(pk=actor.pk)
        return rebuild_stock_balances(
            company=local_company,
            actor=local_actor,
            reason="并发库存重建",
            confirm=True,
        )

    def run_post():
        local_document = SupplyDocument.objects.get(pk=receipt.pk)
        local_actor = get_user_model().objects.get(pk=actor.pk)
        return post_supply_document(document=local_document, actor=local_actor)

    with ThreadPoolExecutor(max_workers=2) as pool:
        rebuild_future = pool.submit(_thread, run_rebuild)
        assert entered.wait(10)
        post_future = pool.submit(_thread, run_post)
        time.sleep(0.2)
        assert not post_future.done()
        release.set()
        rebuild_future.result(timeout=15)
        post_future.result(timeout=15)
    assert reconcile_stock_balances(company=company).is_consistent
    balance.refresh_from_db()
    assert balance.quantity_on_hand == Decimal("3.0000")


def test_custody_rebuild_serializes_writeoff_and_remains_consistent(monkeypatch):
    company, actor = rebuild_context()
    custody = SupplyCustody.objects.get(company=company)
    with transaction.atomic():
        _update_custody_values(
            custody=custody,
            quantity=Decimal("0.5000"),
            amount=Decimal("40.00"),
            status="open",
            updated_at=custody.updated_at,
        )
    entered = Event()
    release = Event()
    original = reconciliation._update_custody_values

    def paused_update(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)

    monkeypatch.setattr(reconciliation, "_update_custody_values", paused_update)

    def run_rebuild():
        local_company = Company.objects.get(pk=company.pk)
        local_actor = get_user_model().objects.get(pk=actor.pk)
        return rebuild_custodies(
            company=local_company,
            actor=local_actor,
            reason="并发保管重建",
            confirm=True,
        )

    def run_writeoff():
        local_custody = SupplyCustody.objects.get(pk=custody.pk)
        local_actor = get_user_model().objects.get(pk=actor.pk)
        return write_off_custody(
            custody=local_custody,
            quantity=Decimal("0.5000"),
            action="loss",
            business_date=date(2026, 8, 27),
            reason="并发测试报损",
            actor=local_actor,
            idempotency_key="s18-concurrent-loss",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rebuild_future = pool.submit(_thread, run_rebuild)
        assert entered.wait(10)
        action_future = pool.submit(_thread, run_writeoff)
        time.sleep(0.2)
        assert not action_future.done()
        release.set()
        rebuild_future.result(timeout=15)
        action_future.result(timeout=15)
    assert reconcile_custodies(company=company).is_consistent
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("0.5000")
    assert custody.current_amount == Decimal("40.00")
