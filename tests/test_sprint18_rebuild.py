from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.supplies.models import SupplyCustody, SupplyStockBalance, SupplyStockLedger
from apps.supplies.reconciliation import (
    rebuild_custodies,
    rebuild_stock_balances,
    reconcile_custodies,
    reconcile_stock_balances,
)
from apps.supplies.services import (
    _update_balance_values,
    _update_custody_values,
    create_supply_count_task,
    post_supply_document,
    publish_supply_count_task,
)
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


def rebuild_context():
    company = make_company("S18B")
    actor = make_user("s18-rebuild-finance", "finance")
    department = make_department(company, "S18BD")
    category = make_supply_category(company, "S18BC")
    warehouse = make_supply_warehouse(company, "S18BW")
    durable = make_supply_item(
        company,
        category,
        "S18BDUR",
        item_type="durable_quantity",
        unit="把",
    )
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="3",
        unit_cost="80",
        key="s18-rebuild-opening",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=department,
        quantity="1",
        key="s18-rebuild-issue",
    )
    post_supply_document(document=issue, actor=actor)
    return company, actor


def test_stock_rebuild_dry_run_confirm_ledger_immutability_and_idempotency():
    company, actor = rebuild_context()
    balance = SupplyStockBalance.objects.get(company=company)
    ledger_snapshot = list(
        SupplyStockLedger.objects.filter(company=company).values_list(
            "pk", "quantity_delta", "amount_delta"
        )
    )
    with transaction.atomic():
        _update_balance_values(
            balance=balance,
            quantity=Decimal("1.0000"),
            amount=Decimal("80.00"),
            average_unit_cost=Decimal("80.000000"),
            updated_at=timezone.now(),
        )
    dry = rebuild_stock_balances(
        company=company, actor=actor, reason="隔离测试差异", confirm=False
    )
    assert len(dry.differences) == 1
    balance.refresh_from_db()
    assert balance.quantity_on_hand == Decimal("1.0000")

    confirmed = rebuild_stock_balances(
        company=company, actor=actor, reason="隔离测试差异", confirm=True
    )
    assert confirmed.is_consistent
    assert reconcile_stock_balances(company=company).is_consistent
    assert list(
        SupplyStockLedger.objects.filter(company=company).values_list(
            "pk", "quantity_delta", "amount_delta"
        )
    ) == ledger_snapshot
    assert AuditLog.objects.filter(action="supply_stock_balances_rebuilt").count() == 1
    repeated = rebuild_stock_balances(
        company=company, actor=actor, reason="重复确认", confirm=True
    )
    assert repeated.is_consistent
    assert AuditLog.objects.filter(action="supply_stock_balances_rebuilt").count() == 1


def test_custody_rebuild_dry_run_confirm_preserves_cost_and_history():
    company, actor = rebuild_context()
    custody = SupplyCustody.objects.get(company=company)
    cost = custody.unit_cost_snapshot
    movement_snapshot = list(
        custody.incoming_movements.values_list("pk", "action", "quantity", "amount")
    )
    with transaction.atomic():
        _update_custody_values(
            custody=custody,
            quantity=Decimal("0.5000"),
            amount=Decimal("40.00"),
            status="open",
            updated_at=timezone.now(),
        )
    dry = rebuild_custodies(
        company=company, actor=actor, reason="隔离测试保管差异", confirm=False
    )
    assert len(dry.differences) == 1
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("0.5000")

    confirmed = rebuild_custodies(
        company=company, actor=actor, reason="隔离测试保管差异", confirm=True
    )
    assert confirmed.is_consistent
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("1.0000")
    assert custody.current_amount == Decimal("80.00")
    assert custody.unit_cost_snapshot == cost
    assert list(
        custody.incoming_movements.values_list("pk", "action", "quantity", "amount")
    ) == movement_snapshot
    assert reconcile_custodies(company=company).is_consistent


def test_rebuild_rejects_empty_reason():
    company, actor = rebuild_context()
    with pytest.raises(ValidationError, match="必须填写原因"):
        rebuild_stock_balances(company=company, actor=actor, reason="", confirm=True)
    with pytest.raises(ValidationError, match="必须填写原因"):
        rebuild_custodies(company=company, actor=actor, reason="", confirm=True)


def test_active_stock_and_custody_counts_block_confirmed_rebuild():
    company, actor = rebuild_context()
    balance = SupplyStockBalance.objects.get(company=company)
    custody = SupplyCustody.objects.get(company=company)
    stock_task = create_supply_count_task(
        actor=actor,
        company=company,
        data={
            "name": "库存重建阻断盘点",
            "count_domain": "warehouse_stock",
            "warehouse": balance.warehouse,
            "planned_start": timezone.localdate(),
            "planned_end": timezone.localdate(),
            "idempotency_key": "s18-rebuild-stock-count",
        },
    )
    publish_supply_count_task(task=stock_task, actor=actor)
    custody_task = create_supply_count_task(
        actor=actor,
        company=company,
        data={
            "name": "保管重建阻断盘点",
            "count_domain": "custody",
            "department": custody.department,
            "planned_start": timezone.localdate(),
            "planned_end": timezone.localdate(),
            "idempotency_key": "s18-rebuild-custody-count",
        },
    )
    publish_supply_count_task(task=custody_task, actor=actor)
    with pytest.raises(ValidationError, match="活动仓库盘点"):
        rebuild_stock_balances(
            company=company, actor=actor, reason="应被盘点阻断", confirm=True
        )
    with pytest.raises(ValidationError, match="活动保管盘点"):
        rebuild_custodies(
            company=company, actor=actor, reason="应被盘点阻断", confirm=True
        )
