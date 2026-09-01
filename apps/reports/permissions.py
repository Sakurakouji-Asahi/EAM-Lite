"""Sprint 11 report view, export, download and external-reference permissions."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import resolve_department_ids, role_names_for
from apps.reports.schemas import SUPPLY_REPORT_KEYS, get_report_definition


PHYSICAL_VIEW_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "department_manager", "employee", "warehouse", "management"}
)
PHYSICAL_EXPORT_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "department_manager", "warehouse", "management"}
)
FINANCIAL_VIEW_ROLES = frozenset({"finance", "management"})
FINANCIAL_EXPORT_ROLES = frozenset({"finance", "management"})

SUPPLY_STOCK_REPORTS = frozenset(
    {
        "supply_stock_balance",
        "supply_low_stock",
        "supply_stock_movement",
        "supply_stock_ledger",
    }
)
SUPPLY_RELATION_REPORTS = frozenset(
    {
        "supply_issue_detail",
        "supply_department_issue",
        "supply_employee_issue",
        "supply_custody_balance",
        "supply_custody_movement",
        "supply_count_difference",
    }
)


def _roles(user):
    return role_names_for(user)


def can_view_report(user, report_key):
    definition = get_report_definition(report_key)
    roles = _roles(user)
    if report_key in SUPPLY_REPORT_KEYS:
        from apps.supplies.permissions import (
            can_view_supply_cost,
            can_view_supply_custodies,
            can_view_supply_stock,
        )

        if report_key in SUPPLY_STOCK_REPORTS:
            return can_view_supply_stock(user)
        if report_key in SUPPLY_RELATION_REPORTS:
            return can_view_supply_custodies(user)
        if report_key == "controlled_non_fixed_assets":
            return bool(roles.intersection(PHYSICAL_VIEW_ROLES))
        if report_key == "supply_management_amount":
            return bool(
                roles.intersection(
                    {"system_admin", "finance", "warehouse", "equipment", "management"}
                )
                and can_view_supply_cost(user)
            )
        return False
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
    if report_key in SUPPLY_REPORT_KEYS:
        # Supply exports are the downloadable form of the exact same scoped
        # report. Department managers and employees may therefore export only
        # their already-authorized department/self rows.
        return can_view_report(user, report_key)
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
    if definition.supply:
        from apps.assets.permissions import can_view_financial_fields
        from apps.reports.schemas import visible_report_definition
        from apps.supplies.permissions import can_view_supply_cost

        visible = visible_report_definition(
            export_log.export_type,
            include_supply_cost=can_view_supply_cost(user),
            include_asset_finance=can_view_financial_fields(user),
        )
        allowed_cost_columns = {
            column.key for column in visible.columns if column.access
        }
        exported_cost_columns = set(
            export_log.filters_json.get("_cost_columns") or ()
        )
        if not exported_cost_columns.issubset(allowed_cost_columns):
            return False
        global_roles = {
            "finance", "equipment", "warehouse", "management", "system_admin"
        }
    else:
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
        if not set(exported_scope).issubset(resolve_department_ids(user, company)):
            return False
    if definition.supply and "employee" in roles:
        from apps.masterdata.models import Employee

        exported_scope = export_log.filters_json.get("_authorized_employee_ids")
        if exported_scope is None:
            return False
        current_scope = {
            str(employee_id)
            for employee_id in Employee.objects.filter(
                company=company,
                user=user,
            ).values_list("pk", flat=True)
        }
        if set(exported_scope) != current_scope:
            return False
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
