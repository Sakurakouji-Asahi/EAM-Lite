"""Sprint 11 report view, export, download and external-reference permissions."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import resolve_department_ids, role_names_for
from apps.reports.schemas import get_report_definition


PHYSICAL_VIEW_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "department_manager", "employee", "warehouse", "management"}
)
PHYSICAL_EXPORT_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "department_manager", "warehouse", "management"}
)
FINANCIAL_VIEW_ROLES = frozenset({"finance", "management"})
FINANCIAL_EXPORT_ROLES = frozenset({"finance", "management"})


def _roles(user):
    return role_names_for(user)


def can_view_report(user, report_key):
    definition = get_report_definition(report_key)
    roles = _roles(user)
    if definition.tplus:
        return "finance" in roles
    if definition.hr_clearance:
        return bool(roles.intersection(PHYSICAL_VIEW_ROLES | {"hr"}))
    if definition.financial:
        return bool(roles.intersection(FINANCIAL_VIEW_ROLES))
    return bool(roles.intersection(PHYSICAL_VIEW_ROLES))


def require_view_report(user, report_key):
    if not can_view_report(user, report_key):
        raise PermissionDenied("您没有查看此报表的权限。")


def can_export_report(user, report_key):
    definition = get_report_definition(report_key)
    roles = _roles(user)
    if definition.tplus:
        return "finance" in roles
    if definition.hr_clearance:
        return bool(roles.intersection(PHYSICAL_EXPORT_ROLES | {"hr"}))
    if definition.financial:
        return bool(roles.intersection(FINANCIAL_EXPORT_ROLES))
    return bool(roles.intersection(PHYSICAL_EXPORT_ROLES))


def require_export_report(user, report_key):
    if not can_export_report(user, report_key):
        raise PermissionDenied("您没有导出此报表的权限。")


def require_tplus_export(user):
    if "finance" not in _roles(user):
        raise PermissionDenied("只有 finance 可以生成 T+ 人工对账文件。")


def scoped_report_assets(user, company, queryset=None):
    """Apply the report P1 scope without letting HR summary access widen it.

    ``apps.assets.scoped_assets`` deliberately gives HR company-wide P0 access
    for clearance summaries.  Reports and Dashboard contain P1 fields, so a
    user who combines HR with employee/department-manager must receive only
    the scope contributed by those non-HR roles.
    """
    from apps.assets.models import Asset
    from apps.masterdata.models import Employee

    queryset = queryset if queryset is not None else Asset.objects.all()
    queryset = queryset.filter(company=company)
    roles = _roles(user)
    if roles.intersection(
        {"system_admin", "finance", "equipment", "warehouse", "management"}
    ):
        return queryset
    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(department_id__in=resolve_department_ids(user, company))
    if "employee" in roles:
        employee_ids = Employee.objects.filter(
            company=company, user=user
        ).values_list("pk", flat=True)
        filters |= Q(responsible_employee_id__in=employee_ids)
    return queryset.filter(filters).distinct()


def can_view_external_reference(user):
    return bool(_roles(user).intersection({"finance", "management"}))


def require_view_external_reference(user):
    if not can_view_external_reference(user):
        raise PermissionDenied("您没有查看 T+ 资产卡片引用的权限。")


def can_manage_external_reference(user):
    return "finance" in _roles(user)


def require_manage_external_reference(user):
    if not can_manage_external_reference(user):
        raise PermissionDenied("只有 finance 可以新增或更正 T+ 资产卡片引用。")


def can_view_export(user, export_log):
    from apps.masterdata.permissions import current_company, resolve_department_ids, role_names_for

    company = current_company()
    if not bool(
        export_log is not None
        and company is not None
        and export_log.company_id == company.pk
        and can_view_report(user, export_log.export_type)
    ):
        return False
    roles = role_names_for(user)
    definition = get_report_definition(export_log.export_type)
    global_roles = (
        {"finance", "equipment", "management", "hr"}
        if definition.hr_clearance
        else {"finance", "equipment", "warehouse", "management", "system_admin"}
    )
    if roles.intersection(global_roles):
        return True
    if export_log.requested_by_id != getattr(user, "pk", None):
        return False
    if definition.hr_clearance and "warehouse" in roles:
        from apps.offboarding.permissions import scoped_clearance_items

        exported_scope = export_log.filters_json.get("_authorized_clearance_item_ids")
        if exported_scope is None:
            return False
        return scoped_clearance_items(user, company).filter(
            pk__in=exported_scope
        ).count() == len(set(exported_scope))
    if "department_manager" in roles:
        exported_scope = export_log.filters_json.get("_authorized_department_ids")
        if exported_scope is None:
            return False
        return set(exported_scope).issubset(resolve_department_ids(user, company))
    return True


def require_view_export(user, export_log):
    if not can_view_export(user, export_log):
        raise PermissionDenied("您没有查看此导出记录的权限。")


def can_download_export(user, export_log):
    return bool(
        can_view_export(user, export_log)
        and getattr(export_log, "status", None) == "completed"
        and can_export_report(user, export_log.export_type)
    )


def require_download_export(user, export_log):
    if not can_download_export(user, export_log):
        raise PermissionDenied("您没有下载此导出文件的权限。")


__all__ = [
    "can_download_export", "can_export_report", "can_manage_external_reference",
    "can_view_export",
    "can_view_external_reference", "can_view_report", "require_download_export",
    "require_export_report", "require_manage_external_reference",
    "require_tplus_export", "require_view_export", "require_view_external_reference",
    "require_view_report", "scoped_report_assets",
]
