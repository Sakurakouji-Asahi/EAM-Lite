"""Authorization helpers for Sprint 4 finance and depreciation actions.

The finance boundary is intentionally narrower than the general asset scope:
``system_admin`` is not a finance role, and ``management`` is read-only.  The
helpers in this module are also used by services, so a forged HTTP request or a
direct service invocation receives the same denial as the UI.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied

from apps.masterdata.permissions import current_company, role_names_for


FINANCE_READ_ROLES = frozenset({"finance", "management"})
FINANCE_WRITE_ROLES = frozenset({"finance"})


def _roles(user) -> set[str]:
    return role_names_for(user)


def can_view_finance(user) -> bool:
    """Return whether *user* may receive F1 financial fields."""

    return bool(_roles(user).intersection(FINANCE_READ_ROLES))


def can_manage_finance(user) -> bool:
    """Return whether *user* may perform a finance business mutation."""

    return "finance" in _roles(user)


def require_view_finance(user) -> None:
    if not can_view_finance(user):
        raise PermissionDenied("您没有查看资产财务信息的权限。")


def require_manage_finance(user) -> None:
    if not can_manage_finance(user):
        raise PermissionDenied("只有 finance 可以执行此财务操作。")


def can_view_finance_object(user, obj) -> bool:
    """Apply the F1 role gate and the V1 current-company boundary."""

    if not can_view_finance(user) or obj is None:
        return False
    company_id = getattr(obj, "company_id", None)
    if company_id is None:
        asset = getattr(obj, "asset", None)
        company_id = getattr(asset, "company_id", None)
    company = current_company(include_inactive=True)
    return bool(company is not None and company_id == company.pk)


def require_view_finance_object(user, obj) -> None:
    if not can_view_finance_object(user, obj):
        raise PermissionDenied("您没有查看此财务对象的权限。")


def can_manage_finance_object(user, obj) -> bool:
    return can_manage_finance(user) and can_view_finance_object(user, obj)


def require_manage_finance_object(user, obj) -> None:
    require_manage_finance(user)
    if not can_view_finance_object(user, obj):
        raise PermissionDenied("目标财务对象不属于当前公司。")


def scoped_finance_assets(user, company, queryset=None):
    """Return only assets whose F1 data the caller may receive.

    Both approved finance-reading roles have company-wide F1 scope.  Returning
    ``none()`` for every other role prevents accidental serialization of
    financial fields even if a view forgot its explicit role check.
    """

    from apps.assets.models import Asset

    queryset = queryset if queryset is not None else Asset.objects.all()
    queryset = queryset.filter(company=company)
    if not can_view_finance(user):
        return queryset.none()
    selected = current_company(include_inactive=True)
    if selected is None or company is None or selected.pk != company.pk:
        return queryset.none()
    return queryset


def scoped_finance_objects(user, company, queryset):
    """Apply the shared company/F1 gate to any company-scoped queryset."""

    queryset = queryset.filter(company=company)
    if not can_view_finance(user):
        return queryset.none()
    selected = current_company(include_inactive=True)
    if selected is None or company is None or selected.pk != company.pk:
        return queryset.none()
    return queryset


# Explicit action aliases keep call sites readable and make the matrix hard to
# accidentally widen when more finance services are added.
can_confirm_asset_finance = can_manage_finance
can_manage_depreciation_policy = can_manage_finance
can_confirm_depreciation_batch = can_manage_finance
can_reverse_depreciation = can_manage_finance
can_adjust_asset_value = can_manage_finance
can_record_work_usage = can_manage_finance
can_save_theoretical_run = can_manage_finance


__all__ = [
    "FINANCE_READ_ROLES",
    "FINANCE_WRITE_ROLES",
    "can_adjust_asset_value",
    "can_confirm_asset_finance",
    "can_confirm_depreciation_batch",
    "can_manage_depreciation_policy",
    "can_manage_finance",
    "can_manage_finance_object",
    "can_record_work_usage",
    "can_reverse_depreciation",
    "can_save_theoretical_run",
    "can_view_finance",
    "can_view_finance_object",
    "require_manage_finance",
    "require_manage_finance_object",
    "require_view_finance",
    "require_view_finance_object",
    "scoped_finance_assets",
    "scoped_finance_objects",
]
