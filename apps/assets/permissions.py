"""Sprint 3 asset permissions and object-scope helpers."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import resolve_department_ids, role_names_for


ASSET_GLOBAL_VIEW_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "warehouse", "hr", "management"}
)
ASSET_GLOBAL_P1_VIEW_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "warehouse", "management"}
)
ASSET_GLOBAL_WRITE_ROLES = frozenset({"finance", "equipment", "warehouse"})


def _roles(user) -> set[str]:
    return role_names_for(user)


def scoped_assets(user, company, queryset=None):
    """Apply company scope first, then the role/department asset scope."""
    from apps.assets.models import Asset

    queryset = queryset if queryset is not None else Asset.objects.all()
    queryset = queryset.filter(company=company)
    roles = _roles(user)
    if roles.intersection(ASSET_GLOBAL_VIEW_ROLES):
        return queryset

    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(department_id__in=resolve_department_ids(user, company))
    if "employee" in roles:
        from apps.masterdata.models import Employee

        employee_ids = Employee.objects.filter(
            company=company, user=user
        ).values_list("pk", flat=True)
        filters |= Q(responsible_employee_id__in=employee_ids)
    return queryset.filter(filters).distinct()


def scoped_assets_p1(user, company, queryset=None):
    """Apply the P1 role grant before combining department/object scope."""
    from apps.assets.models import Asset

    queryset = queryset if queryset is not None else Asset.objects.all()
    queryset = queryset.filter(company=company)
    roles = _roles(user)
    if roles.intersection(ASSET_GLOBAL_P1_VIEW_ROLES):
        return queryset

    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(department_id__in=resolve_department_ids(user, company))
    if "employee" in roles:
        from apps.masterdata.models import Employee

        employee_ids = Employee.objects.filter(
            company=company, user=user
        ).values_list("pk", flat=True)
        filters |= Q(responsible_employee_id__in=employee_ids)
    return queryset.filter(filters).distinct()


def can_view_asset(user, asset) -> bool:
    if (
        asset is None
        or asset.company_id is None
        or not asset.pk
        or asset._state.adding
    ):
        return False
    return scoped_assets(user, asset.company).filter(pk=asset.pk).exists()


def require_view_asset(user, asset) -> None:
    if not can_view_asset(user, asset):
        raise PermissionDenied("您没有查看此资产的权限。")


def can_view_asset_p1(user, asset) -> bool:
    """HR only receives the P0 clearance summary, not normal P1 details."""
    if (
        asset is None
        or asset.company_id is None
        or not asset.pk
        or asset._state.adding
    ):
        return False
    return scoped_assets_p1(user, asset.company).filter(pk=asset.pk).exists()


def can_view_asset_summary_fields(user, asset) -> bool:
    """HR receives P0 plus the explicitly approved responsibility summary."""
    return can_view_asset(user, asset) and "hr" in _roles(user)


def can_create_asset_draft(user, company, department=None) -> bool:
    roles = _roles(user)
    if roles.intersection(ASSET_GLOBAL_WRITE_ROLES):
        return True
    if "department_manager" not in roles or department is None:
        return False
    return (
        department.company_id == company.pk
        and department.pk in resolve_department_ids(user, company)
    )


def can_edit_asset_draft(user, asset) -> bool:
    if asset._state.adding or asset.asset_status != "draft":
        return False
    roles = _roles(user)
    if roles.intersection(ASSET_GLOBAL_WRITE_ROLES):
        return True
    return bool(
        "department_manager" in roles
        and asset.department_id
        and asset.department_id in resolve_department_ids(user, asset.company)
    )


def require_edit_asset_draft(user, asset) -> None:
    if not can_edit_asset_draft(user, asset):
        raise PermissionDenied("您没有维护此资产草稿的权限。")


def can_submit_asset(user, asset) -> bool:
    return asset.asset_status == "draft" and can_edit_asset_draft(user, asset)


def can_withdraw_asset(user, asset) -> bool:
    if asset.asset_status != "pending_finance":
        return False
    roles = _roles(user)
    if "finance" in roles:
        return True
    return bool(
        asset.submitted_by_id == getattr(user, "pk", None)
        and can_view_asset(user, asset)
    )


def can_delete_asset_draft(user, asset) -> bool:
    if asset.asset_status != "draft":
        return False
    roles = _roles(user)
    if roles.intersection({"finance", "equipment"}):
        return True
    if "warehouse" in roles and asset.created_by_id == getattr(user, "pk", None):
        return True
    return bool(
        "department_manager" in roles
        and asset.created_by_id == getattr(user, "pk", None)
        and asset.department_id
        and asset.department_id in resolve_department_ids(user, asset.company)
    )


def can_set_requested_coding_scheme(user, asset) -> bool:
    return bool(
        "system_admin" in _roles(user)
        and asset.asset_status in {"draft", "pending_finance"}
        and not asset._state.adding
        and asset.current_issued_code_id is None
        and asset.asset_code is None
    )


def can_view_financial_fields(user) -> bool:
    return bool(_roles(user).intersection({"finance", "management"}))


def can_write_financial_fields(user) -> bool:
    return "finance" in _roles(user)


def can_view_attachment(user, link) -> bool:
    if getattr(link, "status", None) != "active" or link.asset_id is None:
        return False
    if not can_view_asset(user, link.asset):
        return False
    if link.security_class == "A1":
        return bool(_roles(user).intersection({"finance", "management"}))
    return can_view_asset_p1(user, link.asset)


def require_view_attachment(user, link) -> None:
    if not can_view_attachment(user, link):
        raise PermissionDenied("您没有查看或下载此附件的权限。")


def can_create_attachment_link(user, asset, security_class) -> bool:
    if security_class == "A1":
        return bool(
            "finance" in _roles(user)
            and asset.asset_status in {"draft", "pending_finance"}
        )
    return security_class == "A0" and can_edit_asset_draft(user, asset)


def can_void_attachment_link(user, link) -> bool:
    if link.status != "active" or link.asset_id is None:
        return False
    if link.security_class == "A1":
        return bool(
            "finance" in _roles(user)
            and link.asset.asset_status in {"draft", "pending_finance"}
        )
    if link.asset.asset_status == "pending_finance":
        return bool(
            can_view_asset(user, link.asset)
            and (
                "finance" in _roles(user)
                or link.asset.submitted_by_id == getattr(user, "pk", None)
            )
        )
    return can_edit_asset_draft(user, link.asset)
