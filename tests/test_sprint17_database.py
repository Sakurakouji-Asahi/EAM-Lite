from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet

from apps.supplies.models import (
    EmployeeSupplyClearanceItem,
    SupplyCountLine,
    SupplyCountTask,
    SupplyCustodyMovement,
    SupplyDocument,
)
from apps.supplies.services import (
    create_supply_count_task,
    publish_supply_count_task,
    record_supply_count,
    stop_supply_count_entry,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import seed_supply_stock
from tests.test_sprint17_services import make_count


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 17 database guards require PostgreSQL 18.4")


def test_sprint17_postgresql_guards_are_installed():
    require_postgresql()
    expected = {
        "trg_supply_count_task_s17",
        "trg_supply_count_line_s17",
        "trg_supply_count_adjustment_line_s17",
        "trg_supply_count_adjustment_ledger_s17",
        "trg_employee_supply_clearance_item_s17",
        "trg_employee_supply_clearance_commit_s17",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname=ANY(%s) AND NOT tgisinternal",
            [list(expected)],
        )
        assert expected <= {row[0] for row in cursor.fetchall()}
    assert {
        "uq_supply_count_active_warehouse",
        "uq_supply_count_active_employee",
        "ck_supply_count_task_scope",
    } <= {constraint.name for constraint in SupplyCountTask._meta.constraints}
    assert "ck_supply_count_line_evidence" in {
        constraint.name for constraint in SupplyCountLine._meta.constraints
    }


def test_postgresql_rejects_direct_task_transition_snapshot_edit_and_manual_adjustment():
    require_postgresql()
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="1",
        unit_cost="10",
        key="s17-db-stock",
    )
    task = make_count(
        actor=actor,
        company=company,
        domain="warehouse_stock",
        warehouse=warehouse,
        key="s17-db-count",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyCountTask._base_manager.filter(pk=task.pk),
            status="in_progress",
        )
    publish_supply_count_task(task=task, actor=actor)
    line = task.lines.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyCountLine._base_manager.filter(pk=line.pk),
            expected_quantity=Decimal("99"),
        )
    record_supply_count(
        line=line,
        counted_quantity=line.expected_quantity,
        remark="",
        actor=actor,
    )
    stop_supply_count_entry(task=task, actor=actor)
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplyDocument.objects.create(
            company=company,
            document_no="PD-2026-999999",
            document_type="count_adjustment",
            business_date=date(2026, 8, 27),
            source_count_task=task,
            status="draft",
            idempotency_key="s17-manual-adjustment",
        )


def test_postgresql_count_task_company_boundary_and_append_only_evidence():
    require_postgresql()
    company, actor, _, _, warehouse, _, _, _ = supply_context()
    # Direct inserts cannot self-authorize merely by satisfying ordinary model fields.
    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        SupplyCountTask.objects.create(
            company=company,
            task_no="PDRW-2026-999999",
            name="绕过服务",
            count_domain="warehouse_stock",
            warehouse=warehouse,
            planned_start=date(2026, 8, 27),
            planned_end=date(2026, 8, 27),
            idempotency_key="s17-direct-task",
            created_by=actor,
        )
    for model in (SupplyCustodyMovement, EmployeeSupplyClearanceItem):
        with pytest.raises(ValidationError):
            model.objects.all().delete()
