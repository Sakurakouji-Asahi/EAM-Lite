from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.maintenance.permissions import (
    can_close_maintenance_problem,
    can_complete_maintenance,
    can_manage_maintenance_plan,
    can_view_maintenance_plan,
    scoped_maintenance_plans,
)
from apps.maintenance.services import complete_maintenance, create_maintenance_plan
from tests.test_sprint3_support import (
    grant_scope,
    make_department,
    make_employee,
    make_user,
)
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def test_role_matrix_read_manage_complete_and_cross_department_boundaries():
    ctx = maintenance_context("S9PERMISSIONS")
    management = make_user("s9permissions-management", "management")
    warehouse = make_user("s9permissions-warehouse", "warehouse")
    warehouse_employee = make_employee(
        ctx["company"],
        ctx["department"],
        "S9PERMISSIONS-WH",
        user=warehouse,
    )
    scoped_manager = make_user("s9permissions-in-manager", "department_manager")
    grant_scope(
        scoped_manager,
        ctx["company"],
        ctx["department"],
        assigned_by=ctx["admin"],
    )
    other_department = make_department(ctx["company"], "S9PERMISSIONS-D2")
    out_manager = make_user("s9permissions-out-manager", "department_manager")
    grant_scope(
        out_manager,
        ctx["company"],
        other_department,
        assigned_by=ctx["admin"],
    )

    assert can_manage_maintenance_plan(ctx["equipment"], ctx["plan"])
    for reader in (ctx["finance"], ctx["equipment"], management):
        assert can_view_maintenance_plan(reader, ctx["plan"])
    assert not can_view_maintenance_plan(ctx["admin"], ctx["plan"])
    assert can_complete_maintenance(ctx["responsible_user"], ctx["plan"])
    assert can_complete_maintenance(scoped_manager, ctx["plan"])
    assert not can_complete_maintenance(ctx["finance"], ctx["plan"])
    assert not can_complete_maintenance(management, ctx["plan"])
    assert not can_complete_maintenance(warehouse, ctx["plan"])
    assert not can_complete_maintenance(out_manager, ctx["plan"])

    assigned_warehouse_plan = create_maintenance_plan(
        actor=ctx["equipment"],
        company=ctx["company"],
        asset=ctx["asset"],
        name="仓库角色被明确指派计划",
        cycle_value=1,
        cycle_unit="month",
        responsible_employee=warehouse_employee,
        advance_notice_days=1,
        standard_content="仓库被指派后的保养内容",
        first_due_date=ctx["plan"].first_due_date,
    )
    assert can_complete_maintenance(warehouse, assigned_warehouse_plan)
    assert scoped_maintenance_plans(warehouse, ctx["company"]).filter(
        pk=assigned_warehouse_plan.pk
    ).exists()

    with pytest.raises(PermissionDenied):
        complete_maintenance(
            actor=ctx["finance"],
            plan=ctx["plan"],
            scheduled_date=ctx["plan"].next_maintenance_date,
            completed_date=timezone.localdate(),
            actual_content="财务不得完成保养",
            result="normal",
            idempotency_key="S9PERMISSIONS-finance-denied",
        )


def test_problem_close_is_equipment_or_in_scope_manager_only():
    ctx = maintenance_context("S9PERMCLOSE")
    record = complete_maintenance(
        actor=ctx["responsible_user"],
        plan=ctx["plan"],
        scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="发现问题完成记录",
        result="problem_found",
        problem_description="需要继续处理",
        idempotency_key="S9PERMCLOSE-complete",
    )
    manager = make_user("s9permclose-manager", "department_manager")
    grant_scope(
        manager,
        ctx["company"],
        ctx["department"],
        assigned_by=ctx["admin"],
    )
    assert can_close_maintenance_problem(ctx["equipment"], record.problem)
    assert can_close_maintenance_problem(manager, record.problem)
    assert not can_close_maintenance_problem(ctx["finance"], record.problem)
    assert not can_close_maintenance_problem(ctx["responsible_user"], record.problem)
