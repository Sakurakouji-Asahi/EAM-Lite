"""Preventive-maintenance authorization and object-scope helpers."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import resolve_department_ids, role_names_for


VIEW_ROLES = frozenset(
    {
        "finance",
        "equipment",
        "department_manager",
        "employee",
        "warehouse",
        "management",
    }
)


def _roles(user):
    return role_names_for(user)


def _employee_for(user, company):
    from apps.masterdata.models import Employee

    if not getattr(user, "pk", None):
        return None
    return Employee.objects.filter(company=company, user=user).first()


def scoped_maintenance_plans(user, company, queryset=None):
    from apps.maintenance.models import MaintenancePlan

    queryset = queryset if queryset is not None else MaintenancePlan.objects.all()
    queryset = queryset.filter(company=company)
    roles = _roles(user)
    if roles.intersection({"finance", "equipment", "management"}):
        return queryset
    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(asset__department_id__in=resolve_department_ids(user, company))
    if roles.intersection({"employee", "warehouse", "department_manager"}):
        employee = _employee_for(user, company)
        if employee is not None:
            filters |= Q(responsible_employee=employee)
    return queryset.filter(filters).distinct()


def can_view_maintenance_asset_summary(user, asset) -> bool:
    """Return whether a QR page may expose this asset's maintenance facts."""

    if asset is None or not getattr(asset, "pk", None):
        return False
    return scoped_maintenance_plans(user, asset.company).filter(
        asset=asset,
        status="active",
    ).exists()


def can_view_maintenance_plan(user, plan) -> bool:
    return bool(
        plan is not None
        and getattr(plan, "pk", None)
        and scoped_maintenance_plans(user, plan.company).filter(pk=plan.pk).exists()
    )


def require_view_maintenance_plan(user, plan) -> None:
    if not can_view_maintenance_plan(user, plan):
        raise PermissionDenied("您没有查看此保养计划的权限。")


def can_manage_maintenance_plan(user, plan_or_asset) -> bool:
    asset = getattr(plan_or_asset, "asset", plan_or_asset)
    return bool(asset is not None and "equipment" in _roles(user))


def require_manage_maintenance_plan(user, plan_or_asset) -> None:
    if not can_manage_maintenance_plan(user, plan_or_asset):
        raise PermissionDenied("只有 equipment 可以维护保养计划。")


def can_complete_maintenance(user, plan) -> bool:
    if plan is None or not getattr(plan, "pk", None):
        return False
    roles = _roles(user)
    if "equipment" in roles:
        return True
    employee = _employee_for(user, plan.company)
    assigned = bool(
        employee is not None and employee.pk == plan.responsible_employee_id
    )
    if roles.intersection({"employee", "warehouse"}):
        return assigned
    if "department_manager" in roles:
        return assigned or plan.asset.department_id in resolve_department_ids(
            user, plan.company
        )
    return False


def require_complete_maintenance(user, plan) -> None:
    if not can_complete_maintenance(user, plan):
        raise PermissionDenied("您不是当前责任人，也不在允许的保养范围内。")


def can_void_maintenance_record(user, record) -> bool:
    return bool(
        record is not None
        and getattr(record, "pk", None)
        and "equipment" in _roles(user)
    )


def require_void_maintenance_record(user, record) -> None:
    if not can_void_maintenance_record(user, record):
        raise PermissionDenied("只有 equipment 可以作废保养完成记录。")


def can_close_maintenance_problem(user, problem) -> bool:
    if problem is None or not getattr(problem, "pk", None):
        return False
    if "equipment" in _roles(user):
        return True
    return bool(
        "department_manager" in _roles(user)
        and problem.asset.department_id
        in resolve_department_ids(user, problem.company)
    )


def require_close_maintenance_problem(user, problem) -> None:
    if not can_close_maintenance_problem(user, problem):
        raise PermissionDenied("您没有关闭此保养问题的权限。")


def can_view_maintenance_attachment(user, link) -> bool:
    if getattr(link, "status", None) != "active":
        return False
    target = getattr(link, "maintenance_record", None) or getattr(
        link, "maintenance_problem", None
    )
    if target is None:
        return False
    plan = (
        target.maintenance_plan
        if hasattr(target, "maintenance_plan")
        else target.maintenance_record.maintenance_plan
    )
    if not can_view_maintenance_plan(user, plan):
        return False
    if getattr(link, "security_class", None) == "A1":
        return bool(_roles(user).intersection({"finance", "management"}))
    return bool(_roles(user).intersection(VIEW_ROLES))


def require_view_maintenance_attachment(user, link) -> None:
    if not can_view_maintenance_attachment(user, link):
        raise PermissionDenied("您没有查看或下载此保养附件的权限。")


def can_manage_maintenance_attachment(user, target, *, security_class="A0") -> bool:
    record = (
        target
        if getattr(target, "_meta", None)
        and target._meta.model_name == "maintenancerecord"
        else getattr(target, "maintenance_record", None)
    )
    if record is None or record.status != "confirmed":
        return False
    if security_class == "A1":
        return "finance" in _roles(user)
    if security_class != "A0":
        return False
    if target._meta.model_name == "maintenanceproblem":
        return bool(record.status == "confirmed" and can_close_maintenance_problem(user, target))
    return can_complete_maintenance(user, record.maintenance_plan)


__all__ = [
    "can_close_maintenance_problem",
    "can_complete_maintenance",
    "can_view_maintenance_asset_summary",
    "can_manage_maintenance_plan",
    "can_manage_maintenance_attachment",
    "can_view_maintenance_attachment",
    "can_view_maintenance_plan",
    "can_void_maintenance_record",
    "require_close_maintenance_problem",
    "require_complete_maintenance",
    "require_manage_maintenance_plan",
    "require_view_maintenance_attachment",
    "require_view_maintenance_plan",
    "require_void_maintenance_record",
    "scoped_maintenance_plans",
]
