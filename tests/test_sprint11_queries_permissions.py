from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.assets.lifecycle_services import change_asset_assignment, set_asset_idle
from apps.assets.models import Asset
from apps.finance.models import DepreciationSchedule
from apps.finance.services import create_fixed_asset_category
from apps.maintenance.services import complete_maintenance
from apps.masterdata.services import revoke_department_scope
from apps.reports.permissions import can_export_report, can_view_report
from apps.reports.queries import (
    ReportValidationError,
    build_dashboard,
    build_report_dataset,
)
from apps.reports.schemas import REPORT_REGISTRY
from tests.test_sprint3_support import (
    grant_scope,
    make_category,
    make_department,
    make_employee,
    make_user,
)
from tests.test_sprint4_acceptance import _base_context
from tests.test_sprint7_support import (
    active_asset_context,
    active_fixed_asset_context,
    add_target_assignment,
)
from tests.test_sprint8_services import _published
from tests.test_sprint8_support import add_active_asset, inventory_context
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db(transaction=True)


GENERIC_REPORT_TYPES = tuple(
    key for key, definition in REPORT_REGISTRY.items() if not definition.tplus
)


@pytest.mark.parametrize("report_key", GENERIC_REPORT_TYPES)
def test_each_approved_generic_report_builds_its_fixed_schema_on_empty_data(report_key):
    context = _base_context(f"S11EMPTY{GENERIC_REPORT_TYPES.index(report_key)}")
    dataset = build_report_dataset(
        actor=context["finance"],
        company=context["company"],
        report_key=report_key,
        filters={},
    )
    assert dataset.definition is REPORT_REGISTRY[report_key]
    assert dataset.definition.schema_version == "report_v1"
    assert dataset.totals == {}
    assert dataset.data_snapshot_at is not None


def test_report_role_matrix_keeps_financial_tplus_and_export_boundaries():
    context = _base_context("S11PERM")
    roles = {
        "system_admin": context["admin"],
        "finance": context["finance"],
        "equipment": context["equipment"],
        "management": make_user("s11-perm-management", "management"),
        "department_manager": make_user("s11-perm-manager", "department_manager"),
        "employee": make_user("s11-perm-employee", "employee"),
        "hr": make_user("s11-perm-hr", "hr"),
    }
    assert can_view_report(roles["finance"], "fixed_asset_detail")
    assert can_export_report(roles["finance"], "fixed_asset_detail")
    assert can_view_report(roles["management"], "fixed_asset_detail")
    assert can_export_report(roles["management"], "fixed_asset_detail")
    assert not can_view_report(roles["system_admin"], "fixed_asset_detail")
    assert not can_export_report(roles["system_admin"], "fixed_asset_detail")
    assert can_view_report(roles["finance"], "tplus_reconciliation")
    assert not can_view_report(roles["management"], "tplus_reconciliation")
    assert can_view_report(roles["department_manager"], "asset_ledger")
    assert can_export_report(roles["department_manager"], "asset_ledger")
    assert can_view_report(roles["employee"], "asset_ledger")
    assert not can_export_report(roles["employee"], "asset_ledger")
    assert can_view_report(roles["hr"], "offboarding_unresolved")
    assert can_export_report(roles["hr"], "offboarding_unresolved")


@pytest.mark.parametrize(
    "role",
    ("system_admin", "equipment", "warehouse", "department_manager", "employee"),
)
def test_nonfinance_generic_report_rejects_fixed_asset_category_filter(role):
    context = _base_context(f"S11F1{role[:3].upper()}")
    fixed_category = create_fixed_asset_category(
        actor=context["finance"],
        company=context["company"],
        data={
            "code": f"S11F1-{role}",
            "name": f"Sprint11 {role} 越权会计类别",
            "useful_life_months_default": 60,
        },
    )
    if role == "system_admin":
        actor = context["admin"]
    elif role == "equipment":
        actor = context["equipment"]
    else:
        actor = make_user(f"s11-f1-{role}", role)
    if role == "department_manager":
        grant_scope(
            actor,
            context["company"],
            context["department"],
            descendants=False,
            assigned_by=context["admin"],
        )
    elif role == "employee":
        context["employee"].user = actor
        context["employee"].save(update_fields=["user", "updated_at"])

    with pytest.raises(ReportValidationError):
        build_report_dataset(
            actor=actor,
            company=context["company"],
            report_key="asset_ledger",
            filters={"fixed_asset_category": fixed_category.pk},
        )


def test_finance_fixed_asset_report_filters_by_accounting_category():
    context, asset, _qr, _profile, _policy = active_fixed_asset_context("S11F1FIN")
    selected_category_id = asset.finance.fixed_asset_category_id
    other_category = create_fixed_asset_category(
        actor=context["finance"],
        company=context["company"],
        data={
            "code": "S11F1FIN-OTHER",
            "name": "Sprint11 其他会计类别",
            "useful_life_months_default": 60,
        },
    )

    selected = build_report_dataset(
        actor=context["finance"],
        company=context["company"],
        report_key="fixed_asset_detail",
        filters={"fixed_asset_category": selected_category_id},
    )
    excluded = build_report_dataset(
        actor=context["finance"],
        company=context["company"],
        report_key="fixed_asset_detail",
        filters={"fixed_asset_category": other_category.pk},
    )
    assert [row["asset_code"] for row in selected.rows] == [asset.asset_code]
    assert excluded.rows == ()


def test_department_manager_scope_and_cross_department_filter_tampering_are_rejected():
    context, first_asset, _qr = active_asset_context("S11SCOPE")
    second_department = make_department(context["company"], "S11SCOPE-D2")
    second_employee = make_employee(
        context["company"], second_department, "S11SCOPE-E2"
    )
    manager = make_user("s11-scope-manager", "department_manager")
    grant_scope(
        manager,
        context["company"],
        context["department"],
        descendants=False,
        assigned_by=context["admin"],
    )
    visible = build_report_dataset(
        actor=manager,
        company=context["company"],
        report_key="asset_ledger",
        filters={"department": context["department"].pk},
    )
    assert [row["asset_code"] for row in visible.rows] == [first_asset.asset_code]
    with pytest.raises(ReportValidationError):
        build_report_dataset(
            actor=manager,
            company=context["company"],
            report_key="asset_ledger",
            filters={"department": second_department.pk},
        )
    employee_user = make_user("s11-scope-employee", "employee")
    second_employee.user = employee_user
    second_employee.save(update_fields=["user", "updated_at"])
    employee_dataset = build_report_dataset(
        actor=employee_user,
        company=context["company"],
        report_key="asset_ledger",
        filters={},
    )
    assert employee_dataset.rows == ()


def test_as_of_report_uses_movement_before_values_not_current_assignment():
    real_now = timezone.now()
    created_at = real_now - timedelta(days=3)
    context, asset, _qr = active_asset_context("S11HIST")
    Asset._base_manager.filter(pk=asset.pk).update(created_at=created_at)
    old_department = asset.department
    old_employee = asset.responsible_employee
    old_location = asset.location
    moved_at = real_now - timedelta(days=1)
    boundary_day = timezone.localdate(real_now)
    new_department, new_employee, new_location = add_target_assignment(
        context, "S11HIST-N"
    )
    change_asset_assignment(
        actor=context["equipment"],
        asset=asset,
        to_department=new_department,
        to_responsible_employee=new_employee,
        to_location=new_location,
        effective_at=moved_at,
        reason="Sprint11 历史归属验收",
        idempotency_key="S11HIST-move",
        expected_status=asset.asset_status,
        expected_department_id=old_department.pk,
        expected_responsible_employee_id=old_employee.pk,
        expected_location_id=old_location.pk,
    )
    current = build_report_dataset(
        actor=context["equipment"],
        company=context["company"],
        report_key="asset_ledger",
        filters={"as_of_date": boundary_day},
    ).rows[0]
    historic = build_report_dataset(
        actor=context["equipment"],
        company=context["company"],
        report_key="asset_ledger",
        filters={
            "as_of_date": boundary_day - timedelta(days=2),
            "include_drafts": True,
        },
    ).rows[0]
    assert current["department"] == new_department.name
    assert current["responsible_employee"] == new_employee.name
    assert new_location.name in current["location"]
    assert historic["department"] == old_department.name
    assert historic["responsible_employee"] == old_employee.name
    assert old_location.name in historic["location"]


def test_historical_rows_use_current_department_scope_and_revocation():
    real_now = timezone.now()
    context, asset, _qr = active_asset_context("S11HISTSCOPE")
    Asset._base_manager.filter(pk=asset.pk).update(
        created_at=real_now - timedelta(days=3)
    )
    old_department = asset.department
    old_employee = asset.responsible_employee
    old_location = asset.location
    new_department, new_employee, new_location = add_target_assignment(
        context, "S11HISTSCOPE-N"
    )
    old_manager = make_user("s11-history-old-manager", "department_manager")
    new_manager = make_user("s11-history-new-manager", "department_manager")
    grant_scope(
        old_manager,
        context["company"],
        old_department,
        descendants=False,
        assigned_by=context["admin"],
    )
    new_scope = grant_scope(
        new_manager,
        context["company"],
        new_department,
        descendants=False,
        assigned_by=context["admin"],
    )
    change_asset_assignment(
        actor=context["equipment"],
        asset=asset,
        to_department=new_department,
        to_responsible_employee=new_employee,
        to_location=new_location,
        effective_at=real_now - timedelta(days=1),
        reason="Sprint11 历史范围验收",
        idempotency_key="S11HISTSCOPE-move",
        expected_status=asset.asset_status,
        expected_department_id=old_department.pk,
        expected_responsible_employee_id=old_employee.pk,
        expected_location_id=old_location.pk,
    )
    filters = {
        "as_of_date": timezone.localdate(real_now) - timedelta(days=2),
        "include_drafts": True,
    }
    old_rows = build_report_dataset(
        actor=old_manager,
        company=context["company"],
        report_key="asset_ledger",
        filters=filters,
    ).rows
    new_rows = build_report_dataset(
        actor=new_manager,
        company=context["company"],
        report_key="asset_ledger",
        filters=filters,
    ).rows
    assert old_rows == ()
    assert len(new_rows) == 1
    assert new_rows[0]["department"] == old_department.name
    assert new_rows[0]["responsible_employee"] == old_employee.name
    assert old_location.name in new_rows[0]["location"]

    unused_department = make_department(context["company"], "S11HISTSCOPE-EMPTY")
    grant_scope(
        new_manager,
        context["company"],
        unused_department,
        descendants=False,
        assigned_by=context["admin"],
    )
    revoke_department_scope(
        actor=context["admin"],
        scope=new_scope,
        reason="Sprint11 撤销历史查询范围",
    )
    assert build_report_dataset(
        actor=new_manager,
        company=context["company"],
        report_key="asset_ledger",
        filters=filters,
    ).rows == ()


@pytest.mark.parametrize("scope_mode", ("employee", "department_manager"))
def test_hr_combined_role_does_not_expand_report_or_dashboard_scope(scope_mode):
    context, own_asset, _qr = active_asset_context(f"S11HRCOMBO{scope_mode[0]}")
    other_department, other_employee, other_location = add_target_assignment(
        context, f"S11HRCOMBO{scope_mode[0]}-N"
    )
    other_asset, _other_qr = add_active_asset(
        context,
        f"S11HRCOMBO{scope_mode[0]}-OTHER",
        department=other_department,
        employee=other_employee,
        location=other_location,
    )
    actor = make_user(f"s11-hr-{scope_mode}", "hr", scope_mode)
    if scope_mode == "employee":
        context["employee"].user = actor
        context["employee"].save(update_fields=["user", "updated_at"])
    else:
        grant_scope(
            actor,
            context["company"],
            context["department"],
            descendants=False,
            assigned_by=context["admin"],
        )

    dataset = build_report_dataset(
        actor=actor,
        company=context["company"],
        report_key="asset_ledger",
        filters={},
    )
    dashboard = build_dashboard(actor=actor, company=context["company"])
    assert {row["asset_code"] for row in dataset.rows} == {own_asset.asset_code}
    assert other_asset.asset_code not in {row["asset_code"] for row in dataset.rows}
    assert dashboard["physical"]["asset_total"] == 1
    assert sum(row["count"] for row in dashboard["by_department"]) == 1
    assert sum(row["count"] for row in dashboard["by_category"]) == 1


def test_depreciation_schedule_is_theoretical_and_detail_monthly_are_actual():
    from tests.test_sprint4_services import _confirmed_entry, _profile_context

    company, actor, _management, _admin, asset, _finance, profile = _profile_context()
    DepreciationSchedule.objects.create(
        company=company,
        asset=asset,
        depreciation_profile=profile,
        sequence_no=1,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        eligible_fraction=Decimal("1"),
        opening_book_value=Decimal("12000.00"),
        calculated_unrounded=Decimal("200.00"),
        planned_amount=Decimal("200.00"),
        planned_accumulated=Decimal("200.00"),
        closing_book_value=Decimal("11800.00"),
        formula_snapshot_json={},
    )
    # Match the production confirmation boundary: the posted entry and its
    # final confirmed source become visible atomically.
    with transaction.atomic():
        _confirmed_entry(
            profile=profile,
            start=date(2024, 1, 1),
            amount=Decimal("190.00"),
            actor=actor,
        )
    filters = {
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 1, 31),
        "include_drafts": True,
    }
    schedule = build_report_dataset(
        actor=actor,
        company=company,
        report_key="depreciation_schedule",
        filters=filters,
    )
    detail = build_report_dataset(
        actor=actor,
        company=company,
        report_key="depreciation_detail",
        filters=filters,
    )
    monthly = build_report_dataset(
        actor=actor,
        company=company,
        report_key="monthly_depreciation",
        filters=filters,
    )
    assert schedule.rows[0]["theoretical_amount"] == Decimal("200.00")
    assert schedule.rows[0]["actual_amount"] == Decimal("0.00")
    assert detail.rows[0]["theoretical_amount"] is None
    assert detail.rows[0]["actual_amount"] == Decimal("190.00")
    assert monthly.rows[0]["actual_amount"] == Decimal("190.00")
    assert monthly.row_count == 1


def test_inventory_report_reads_published_snapshot_after_master_assignment_changes():
    context, asset, _qr = inventory_context("S11INVSNAP")
    task = _published(context, "S11INVSNAP-T")
    snapshot = task.task_assets.get()
    old_values = (
        snapshot.expected_department_snapshot,
        snapshot.expected_employee_snapshot,
        snapshot.expected_location_path_snapshot,
    )
    department, employee, location = add_target_assignment(context, "S11INVSNAP-N")
    change_asset_assignment(
        actor=context["equipment"],
        asset=asset,
        to_department=department,
        to_responsible_employee=employee,
        to_location=location,
        effective_at=timezone.now(),
        reason="盘点快照后调拨",
        idempotency_key="S11INVSNAP-move",
        expected_status=asset.asset_status,
        expected_department_id=asset.department_id,
        expected_responsible_employee_id=asset.responsible_employee_id,
        expected_location_id=asset.location_id,
    )
    dataset = build_report_dataset(
        actor=context["finance"],
        company=context["company"],
        report_key="inventory_results",
        filters={},
    )
    row = dataset.rows[0]
    assert (
        row["expected_department"],
        row["expected_employee"],
        row["expected_location"],
    ) == old_values


@pytest.mark.parametrize("report_key", ("inventory_results", "inventory_differences"))
def test_inventory_filters_use_published_snapshot_and_task_period(report_key):
    context, asset, _qr = inventory_context(f"S11INVFILTER{report_key[-1]}")
    task = _published(context, f"S11INVFILTER-{report_key[-1]}-T")
    snapshot = task.task_assets.get()
    old_department = asset.department
    old_employee = asset.responsible_employee
    new_department, new_employee, new_location = add_target_assignment(
        context, f"S11INVFILTER-{report_key[-1]}-N"
    )
    change_asset_assignment(
        actor=context["equipment"],
        asset=asset,
        to_department=new_department,
        to_responsible_employee=new_employee,
        to_location=new_location,
        effective_at=timezone.now(),
        reason="盘点快照后调拨",
        idempotency_key=f"S11INVFILTER-{report_key}-move",
        expected_status=asset.asset_status,
        expected_department_id=old_department.pk,
        expected_responsible_employee_id=old_employee.pk,
        expected_location_id=asset.location_id,
    )
    set_asset_idle(
        actor=context["equipment"],
        asset=asset,
        effective_at=timezone.now(),
        reason="盘点快照后闲置",
        idempotency_key=f"S11INVFILTER-{report_key}-idle",
    )
    filters = {
        "department": old_department.pk,
        "category": context["category"].pk,
        "responsible_employee": old_employee.pk,
        "asset_status": "in_use",
        "period_start": task.planned_start,
        "period_end": task.planned_end,
    }
    dataset = build_report_dataset(
        actor=context["finance"],
        company=context["company"],
        report_key=report_key,
        filters=filters,
    )
    assert [row["asset_code"] for row in dataset.rows] == [
        snapshot.expected_code_snapshot
    ]
    for rejected in (
        {"department": new_department.pk},
        {"responsible_employee": new_employee.pk},
        {"asset_status": "idle"},
        {"category": make_category(context["company"], f"{report_key}-other").pk},
        {
            "period_start": task.planned_end + timedelta(days=1),
            "period_end": task.planned_end + timedelta(days=2),
        },
    ):
        assert not build_report_dataset(
            actor=context["finance"],
            company=context["company"],
            report_key=report_key,
            filters={**filters, **rejected},
        ).rows


def test_maintenance_filters_apply_to_plans_records_and_completed_period():
    context = maintenance_context("S11MAINTFILTER")
    base_filters = {
        "category": context["asset"].category_id,
        "responsible_employee": context["responsible"].pk,
        "asset_status": "in_use",
    }
    for report_key in ("maintenance_plans", "maintenance_due"):
        assert build_report_dataset(
            actor=context["equipment"],
            company=context["company"],
            report_key=report_key,
            filters=base_filters,
        ).row_count == 1

    other_category = make_category(context["company"], "S11MAINTFILTER-OTHER")
    other_employee = make_employee(
        context["company"], context["department"], "S11MAINTFILTER-OTHER"
    )
    for rejected in (
        {"category": other_category.pk},
        {"responsible_employee": other_employee.pk},
        {"asset_status": "idle"},
    ):
        assert not build_report_dataset(
            actor=context["equipment"],
            company=context["company"],
            report_key="maintenance_plans",
            filters={**base_filters, **rejected},
        ).rows

    record = complete_maintenance(
        actor=context["responsible_user"],
        plan=context["plan"],
        scheduled_date=context["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="完成 Sprint11 报表筛选验收",
        result="normal",
        problem_description="",
        remark="",
        idempotency_key="S11MAINTFILTER-complete",
    )
    records = build_report_dataset(
        actor=context["equipment"],
        company=context["company"],
        report_key="maintenance_records",
        filters={
            **base_filters,
            "period_start": record.completed_date,
            "period_end": record.completed_date,
        },
    )
    assert records.row_count == 1
    assert records.rows[0]["completed_date"] == record.completed_date
    assert not build_report_dataset(
        actor=context["equipment"],
        company=context["company"],
        report_key="maintenance_records",
        filters={
            **base_filters,
            "period_start": record.completed_date - timedelta(days=2),
            "period_end": record.completed_date - timedelta(days=1),
        },
    ).rows


def test_tplus_requires_dedicated_query_and_nonfinance_is_denied():
    context = _base_context("S11TPLUSPERM")
    with pytest.raises(ReportValidationError):
        build_report_dataset(
            actor=context["finance"],
            company=context["company"],
            report_key="tplus_reconciliation",
            filters={},
        )
    with pytest.raises(PermissionDenied):
        from apps.reports.queries import build_tplus_dataset

        build_tplus_dataset(
            actor=context["equipment"],
            company=context["company"],
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
        )
