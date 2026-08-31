from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet

from apps.supplies.models import SupplyCustody, SupplyCustodyMovement
from apps.supplies.services import post_supply_document, transfer_custody, write_off_custody
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_department, make_issue_document, seed_supply_stock


pytestmark = pytest.mark.django_db(transaction=True)


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 16 database guards require PostgreSQL 18.6")


def base_custody():
    company, actor, department, employee, source, _, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity="2",
        unit_cost="80",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity="2",
    )
    post_supply_document(document=issue, actor=actor)
    return company, actor, department, durable, SupplyCustody.objects.get()


def test_postgresql_sprint16_guards_and_constraints_are_installed():
    require_postgresql()
    expected_triggers = {
        "trg_supply_custody_mutation_s15",
        "trg_supply_custody_refs_s15",
        "trg_supply_custody_move_mutation_s15",
        "trg_supply_custody_move_refs_s15",
        "trg_supply_line_refs_s14",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND NOT tgisinternal",
            [list(expected_triggers)],
        )
        assert expected_triggers <= {row[0] for row in cursor.fetchall()}
    names = {constraint.name for constraint in SupplyCustody._meta.constraints}
    assert "ck_supply_custody_source_shape" in names
    assert "ck_supply_custody_parent_not_self" in names
    movement_names = {
        constraint.name for constraint in SupplyCustodyMovement._meta.constraints
    }
    assert "uq_supply_custody_move_company_idem" in movement_names


def test_postgresql_parent_item_boundary_and_all_movement_actions_are_append_only():
    require_postgresql()
    company, actor, _, _, custody = base_custody()
    target_department = make_department(company, "TARGET")
    child = transfer_custody(
        custody=custody,
        quantity=Decimal("0.5"),
        target_department=target_department,
        business_date="2026-08-26",
        reason="数据库保护",
        actor=actor,
        idempotency_key="s16-db-transfer",
    )
    loss = write_off_custody(
        custody=child,
        quantity=Decimal("0.25"),
        action="loss",
        business_date="2026-08-26",
        reason="数据库保护",
        actor=actor,
        idempotency_key="s16-db-loss",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(
            SupplyCustodyMovement._base_manager.filter(pk=loss.pk),
            amount=Decimal("99.00"),
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM supplies_supplycustodymovement WHERE id=%s", [loss.pk]
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('eam_lite.controlled_supply_custody_mutation','on',true)"
        )
        cursor.execute(
            "UPDATE supplies_supplycustody SET parent_custody_id=id, origin_issue_line_id=NULL WHERE id=%s",
            [custody.pk],
        )
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_model_source_shape_and_reversal_direction_reject_invalid_records_on_all_backends():
    _, actor, _, _, custody = base_custody()
    invalid_child = SupplyCustody(
        company=custody.company,
        item=custody.item,
        origin_issue_line=custody.origin_issue_line,
        parent_custody=custody,
        department=custody.department,
        current_quantity=Decimal("1"),
        current_amount=Decimal("80"),
        unit_cost_snapshot=Decimal("80"),
        started_on="2026-08-26",
        status="open",
    )
    with pytest.raises(ValidationError, match="不得重复占用根来源"):
        invalid_child.full_clean()
    issue_movement = custody.incoming_movements.get(action="issue")
    invalid_reversal = SupplyCustodyMovement(
        company=custody.company,
        item=custody.item,
        from_custody=None,
        to_custody=custody,
        action="reversal",
        quantity=issue_movement.quantity,
        amount=issue_movement.amount,
        unit_cost=issue_movement.unit_cost,
        business_date="2026-08-26",
        reverses_movement=issue_movement,
        created_by=actor,
    )
    with pytest.raises(ValidationError, match="精确反转"):
        invalid_reversal.full_clean()
