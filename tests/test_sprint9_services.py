from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.maintenance.models import MaintenanceProblem, MaintenanceRecord
from apps.maintenance.services import (
    close_maintenance_problem,
    complete_maintenance,
    create_maintenance_plan,
    set_maintenance_plan_status,
    update_maintenance_plan,
    void_maintenance_record,
)
from tests.test_sprint3_support import grant_scope, make_department, make_employee, make_user
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def _complete(ctx, key, **overrides):
    values = {
        "actor": ctx["responsible_user"],
        "plan": ctx["plan"],
        "scheduled_date": ctx["plan"].next_maintenance_date,
        "completed_date": timezone.localdate(),
        "actual_content": "已按标准完成检查、清洁和紧固",
        "result": "normal",
        "problem_description": "",
        "remark": "运行状态正常",
        "idempotency_key": key,
    }
    values.update(overrides)
    return complete_maintenance(**values)


def test_create_plan_initializes_first_due_and_update_recalculates_future_only():
    ctx = maintenance_context("S9PLAN")
    plan = ctx["plan"]
    assert plan.last_maintenance_date is None
    assert plan.next_maintenance_date == plan.first_due_date

    updated = update_maintenance_plan(
        actor=ctx["equipment"],
        plan=plan,
        name="更新后的季度保养",
        cycle_value=3,
        cycle_unit="month",
        responsible_employee=ctx["responsible"],
        advance_notice_days=5,
        standard_content="更新后标准内容",
        first_due_date=plan.first_due_date + timedelta(days=1),
    )
    assert updated.next_maintenance_date == updated.first_due_date
    assert updated.records.count() == 0


def test_assigned_employee_completes_once_and_same_request_is_idempotent():
    ctx = maintenance_context("S9DONE")
    record = _complete(ctx, "S9DONE-key")
    repeated = _complete(ctx, "S9DONE-key")
    ctx["plan"].refresh_from_db()

    assert repeated.pk == record.pk
    assert MaintenanceRecord.objects.filter(maintenance_plan=ctx["plan"]).count() == 1
    assert ctx["plan"].last_maintenance_date == record.completed_date
    assert ctx["plan"].next_maintenance_date > record.completed_date
    assert AuditLog.objects.filter(action="maintenance.completed").count() == 1


def test_same_due_instance_conflict_and_same_key_different_payload_are_rejected():
    ctx = maintenance_context("S9IDEM")
    record = _complete(ctx, "S9IDEM-key")
    with pytest.raises(ValidationError, match="不同请求参数"):
        _complete(ctx, "S9IDEM-key", remark="不同请求")
    with pytest.raises(ValidationError, match="已有确认"):
        _complete(ctx, "S9IDEM-other", plan=ctx["plan"], scheduled_date=record.scheduled_date)
    assert ctx["plan"].records.count() == 1


def test_problem_found_creates_exactly_one_open_followup_and_normal_creates_none():
    ctx = maintenance_context("S9PROBLEM")
    record = _complete(
        ctx,
        "S9PROBLEM-key",
        result="problem_found",
        problem_description="发现防护罩松动",
    )
    problem = MaintenanceProblem.objects.get(maintenance_record=record)
    assert problem.status == "open"
    assert problem.description == "发现防护罩松动"
    assert not hasattr(record, "severity")
    with pytest.raises(ValidationError):
        _complete(
            ctx,
            "S9PROBLEM-normal-text",
            scheduled_date=record.scheduled_date + timedelta(days=1),
            result="normal",
            problem_description="不应接受",
        )


def test_department_manager_closes_only_in_scope_and_void_source_disables_open_problem():
    ctx = maintenance_context("S9CLOSE")
    record = _complete(
        ctx,
        "S9CLOSE-complete",
        result="problem_found",
        problem_description="需要后续处理",
    )
    problem = record.problem
    manager = make_user("s9close-manager", "department_manager")
    grant_scope(manager, ctx["company"], ctx["department"], assigned_by=ctx["admin"])
    closed = close_maintenance_problem(
        actor=manager,
        problem=problem,
        closure_note="已复核并完成紧固",
        idempotency_key="S9CLOSE-close",
    )
    assert closed.status == "closed"

    second_plan = create_maintenance_plan(
        actor=ctx["equipment"],
        company=ctx["company"],
        asset=ctx["asset"],
        name="第二个问题保养计划",
        cycle_value=1,
        cycle_unit="month",
        responsible_employee=ctx["responsible"],
        advance_notice_days=3,
        standard_content="第二项检查",
        first_due_date=ctx["plan"].first_due_date,
    )
    other_record = _complete(
        ctx,
        "S9CLOSE-second",
        plan=second_plan,
        scheduled_date=second_plan.next_maintenance_date,
        result="problem_found",
        problem_description="历史问题",
    )
    void_maintenance_record(
        actor=ctx["equipment"],
        record=other_record,
        reason="完成日期录错",
        idempotency_key="S9CLOSE-void",
    )
    with pytest.raises(ValidationError, match="来源保养记录已作废"):
        close_maintenance_problem(
            actor=ctx["equipment"],
            problem=other_record.problem,
            closure_note="不应关闭",
            idempotency_key="S9CLOSE-invalid-close",
        )
    other_record.problem.refresh_from_db()
    assert other_record.problem.status == "open"


def test_void_preserves_history_and_allows_authorized_rebuild_of_same_due_instance():
    ctx = maintenance_context("S9VOID")
    record = _complete(ctx, "S9VOID-original")
    original_due = record.scheduled_date
    voided = void_maintenance_record(
        actor=ctx["equipment"],
        record=record,
        reason="实际内容录入错误",
        idempotency_key="S9VOID-void",
    )
    ctx["plan"].refresh_from_db()
    assert voided.status == "voided"
    assert ctx["plan"].last_maintenance_date is None
    assert ctx["plan"].next_maintenance_date == ctx["plan"].first_due_date

    rebuilt = _complete(
        ctx,
        "S9VOID-rebuild",
        scheduled_date=original_due,
        actual_content="修正后的实际内容",
    )
    assert rebuilt.pk != voided.pk
    assert ctx["plan"].records.count() == 2
    assert ctx["plan"].records.filter(status="confirmed").count() == 1


def test_permissions_reject_system_admin_unassigned_employee_and_cross_department_manager():
    ctx = maintenance_context("S9DENY")
    unassigned_user = make_user("s9deny-other", "employee")
    make_employee(ctx["company"], ctx["department"], "S9DENY-OTHER", user=unassigned_user)
    with pytest.raises(PermissionDenied):
        _complete(ctx, "S9DENY-employee", actor=unassigned_user)
    with pytest.raises(PermissionDenied):
        _complete(ctx, "S9DENY-admin", actor=ctx["admin"])

    other_department = make_department(ctx["company"], "S9DENY-D2")
    out_manager = make_user("s9deny-manager", "department_manager")
    grant_scope(out_manager, ctx["company"], other_department, assigned_by=ctx["admin"])
    with pytest.raises(PermissionDenied):
        _complete(ctx, "S9DENY-manager", actor=out_manager)
    assert not MaintenanceRecord.objects.filter(maintenance_plan=ctx["plan"]).exists()


def test_suspended_and_disposal_processing_plan_cannot_complete():
    ctx = maintenance_context("S9STATE")
    set_maintenance_plan_status(
        actor=ctx["equipment"], plan=ctx["plan"], status="suspended"
    )
    with pytest.raises(ValidationError, match="只有启用计划"):
        _complete(ctx, "S9STATE-suspended")
