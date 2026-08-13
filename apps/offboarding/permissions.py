"""Sprint 10 employee-clearance authorization and object scoping."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import resolve_department_ids, role_names_for
from apps.offboarding.domain import UNRESOLVED_ITEM_RESOLUTIONS


GLOBAL_CLEARANCE_VIEW_ROLES = frozenset(
    {"finance", "equipment", "hr", "management"}
)


def _roles(user) -> set[str]:
    return role_names_for(user)


def _employee_for(user, company):
    from apps.masterdata.models import Employee

    if not getattr(user, "pk", None):
        return None
    return Employee.objects.filter(company=company, user=user).first()


def scoped_clearances(user, company, queryset=None):
    """Apply company scope and the fixed clearance read matrix."""

    from apps.offboarding.models import EmployeeAssetClearance

    queryset = (
        queryset
        if queryset is not None
        else EmployeeAssetClearance.objects.all()
    )
    queryset = queryset.filter(company=company)
    roles = _roles(user)
    if roles.intersection(GLOBAL_CLEARANCE_VIEW_ROLES):
        return queryset

    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(
            items__original_department_id__in=resolve_department_ids(user, company)
        ) | Q(
            items__asset__department_id__in=resolve_department_ids(user, company)
        )
    employee = _employee_for(user, company)
    if employee is not None and "employee" in roles:
        filters |= Q(employee=employee)
    if "warehouse" in roles:
        filters |= (
            Q(items__resolution__in=UNRESOLVED_ITEM_RESOLUTIONS)
            | Q(items__movement__operated_by=user)
            | Q(items__movement__to_employee__user=user)
            | Q(items__source_loan__received_by_employee__user=user)
        )
    return queryset.filter(filters).distinct()


def scoped_clearance_items(user, company, queryset=None):
    from apps.offboarding.models import EmployeeAssetClearanceItem

    queryset = (
        queryset
        if queryset is not None
        else EmployeeAssetClearanceItem.objects.all()
    )
    queryset = queryset.filter(company=company)
    roles = _roles(user)
    if roles.intersection(GLOBAL_CLEARANCE_VIEW_ROLES):
        return queryset
    filters = Q(pk__in=[])
    if "department_manager" in roles:
        allowed = resolve_department_ids(user, company)
        filters |= Q(original_department_id__in=allowed) | Q(
            asset__department_id__in=allowed
        )
    employee = _employee_for(user, company)
    if employee is not None and "employee" in roles:
        filters |= Q(clearance__employee=employee)
    if "warehouse" in roles:
        filters |= (
            Q(resolution__in=UNRESOLVED_ITEM_RESOLUTIONS)
            | Q(movement__operated_by=user)
            | Q(movement__to_employee__user=user)
            | Q(source_loan__received_by_employee__user=user)
        )
    return queryset.filter(filters).distinct()


def can_view_clearance(user, clearance) -> bool:
    return bool(
        clearance is not None
        and getattr(clearance, "pk", None)
        and scoped_clearances(user, clearance.company).filter(pk=clearance.pk).exists()
    )


def require_view_clearance(user, clearance) -> None:
    if not can_view_clearance(user, clearance):
        raise PermissionDenied("您没有查看此离职资产清退单的权限。")


def can_initiate_clearance(user, employee=None) -> bool:
    return bool("hr" in _roles(user) and (employee is None or getattr(employee, "pk", None)))


def require_initiate_clearance(user, employee=None) -> None:
    if not can_initiate_clearance(user, employee):
        raise PermissionDenied("只有 hr 可以发起员工离职资产清退。")


def can_refresh_clearance(user, clearance=None) -> bool:
    return bool(
        "hr" in _roles(user)
        and (clearance is None or can_view_clearance(user, clearance))
    )


def require_refresh_clearance(user, clearance=None) -> None:
    if not can_refresh_clearance(user, clearance):
        raise PermissionDenied("只有 hr 可以手工刷新或核对清退单。")


def can_complete_clearance(user, clearance=None) -> bool:
    return can_refresh_clearance(user, clearance)


def require_complete_clearance(user, clearance=None) -> None:
    if not can_complete_clearance(user, clearance):
        raise PermissionDenied("只有 hr 可以完成员工离职资产清退。")


def can_create_supplemental_clearance(user, clearance=None) -> bool:
    return can_refresh_clearance(user, clearance)


def require_create_supplemental_clearance(user, clearance=None) -> None:
    if not can_create_supplemental_clearance(user, clearance):
        raise PermissionDenied("只有 hr 可以建立补充清退单。")


def can_view_clearance_attachment(user, link) -> bool:
    if getattr(link, "status", None) != "active":
        return False
    item = getattr(link, "clearance_item", None)
    clearance = getattr(link, "clearance", None)
    if item is not None:
        if not scoped_clearance_items(user, item.company).filter(pk=item.pk).exists():
            return False
    elif clearance is not None:
        if not can_view_clearance(user, clearance):
            return False
    else:
        return False
    if getattr(link, "security_class", None) == "A1":
        return bool(_roles(user).intersection({"finance", "management"}))
    return True


def require_view_clearance_attachment(user, link) -> None:
    if not can_view_clearance_attachment(user, link):
        raise PermissionDenied("您没有查看或下载此清退附件的权限。")


def can_manage_clearance_attachment(user, target, *, security_class="A0") -> bool:
    clearance = (
        target
        if getattr(getattr(target, "_meta", None), "model_name", None)
        == "employeeassetclearance"
        else target.clearance
    )
    if clearance.status not in {"open", "blocked"}:
        return False
    roles = _roles(user)
    if security_class == "A1":
        return "finance" in roles and can_view_clearance(user, clearance)
    if security_class != "A0" or not can_view_clearance(user, clearance):
        return False
    if roles.intersection({"hr", "finance", "equipment"}):
        return True
    if "department_manager" in roles and target is not clearance:
        allowed = resolve_department_ids(user, clearance.company)
        return bool(
            target.asset.department_id in allowed
            or target.original_department_id in allowed
        )
    if "warehouse" in roles and target is not clearance:
        employee_id = getattr(_employee_for(user, clearance.company), "pk", None)
        return bool(
            target.movement_id
            and (
                target.movement.operated_by_id == user.pk
                or target.movement.to_employee_id == employee_id
            )
            or target.source_loan_id
            and target.source_loan.received_by_employee_id == employee_id
        )
    return False


__all__ = [
    "can_complete_clearance",
    "can_create_supplemental_clearance",
    "can_initiate_clearance",
    "can_manage_clearance_attachment",
    "can_refresh_clearance",
    "can_view_clearance",
    "can_view_clearance_attachment",
    "require_complete_clearance",
    "require_create_supplemental_clearance",
    "require_initiate_clearance",
    "require_refresh_clearance",
    "require_view_clearance",
    "require_view_clearance_attachment",
    "scoped_clearance_items",
    "scoped_clearances",
]
