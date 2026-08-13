"""Production-path factories shared by Sprint 9 preventive-maintenance tests."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.maintenance.services import create_maintenance_plan
from tests.test_sprint3_support import make_employee, make_user
from tests.test_sprint7_support import active_asset_context


def maintenance_context(prefix: str, *, cycle_unit="month", cycle_value=1):
    context, asset, qr_identity = active_asset_context(prefix)
    asset.is_maintenance_required = True
    asset.save(update_fields={"is_maintenance_required"})
    responsible_user = make_user(f"{prefix.lower()}-maintainer", "employee")
    responsible = make_employee(
        context["company"],
        context["department"],
        f"{prefix}-M",
        user=responsible_user,
    )
    equipment_employee = make_employee(
        context["company"],
        context["department"],
        f"{prefix}-EQ",
        user=context["equipment"],
    )
    first_due = timezone.localdate() + timedelta(days=3)
    plan = create_maintenance_plan(
        actor=context["equipment"],
        company=context["company"],
        asset=asset,
        name=f"{prefix} 月度保养",
        cycle_value=cycle_value,
        cycle_unit=cycle_unit,
        responsible_employee=responsible,
        advance_notice_days=3,
        standard_content="检查、清洁、紧固并记录结果",
        first_due_date=first_due,
    )
    context.update(
        asset=asset,
        qr_identity=qr_identity,
        responsible_user=responsible_user,
        responsible=responsible,
        equipment_employee=equipment_employee,
        plan=plan,
    )
    return context
