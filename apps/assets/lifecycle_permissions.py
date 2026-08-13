"""Sprint 7 lifecycle/disposal authorization and scope helpers.

The helpers in this module are deliberately independent from views and forms.
Every mutating lifecycle service calls the corresponding ``require_*`` helper
again after locking the authoritative row.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied

from apps.assets.permissions import scoped_assets
from apps.masterdata.permissions import resolve_department_ids, role_names_for


TERMINAL_STATUSES = frozenset({"disposed", "sold", "other_disposed"})
DISPOSAL_VIEW_ROLES = frozenset(
    {
        "system_admin",
        "finance",
        "equipment",
        "department_manager",
        "employee",
        "warehouse",
        "management",
    }
)
LIFECYCLE_ACTIONS = frozenset(
    {
        "assignment",
        "assignment_return",
        "transfer",
        "idle",
        "activate",
        "repair_start",
        "repair_complete",
        "loan",
        "loan_return",
        "disposal_start",
        "disposal_actual_details",
        "disposal_cancel",
        "disposal_finance_lock",
        "disposal_complete",
        "disposal_reversal",
        "archive",
        "restore_visibility",
        "code_correction",
    }
)


def _roles(user) -> set[str]:
    return role_names_for(user)


def _asset_in_company(asset) -> bool:
    return bool(
        asset is not None
        and getattr(asset, "pk", None)
        and getattr(asset, "company_id", None)
        and not asset._state.adding
    )


def scoped_lifecycle_assets(user, company, queryset=None, *, include_archived=False):
    """Return authorized assets, excluding archived rows by default."""

    queryset = scoped_assets(user, company, queryset)
    if not include_archived:
        queryset = queryset.filter(record_status="active")
    return queryset


def scoped_lifecycle_candidates(user, company, queryset=None):
    """Exclude drafts, terminal and archived records from action candidates."""

    return scoped_lifecycle_assets(user, company, queryset).exclude(
        asset_status__in=(
            "draft",
            "pending_finance",
            "pending_label",
            *TERMINAL_STATUSES,
        )
    )


def _department_manager_can_move(user, asset, target_department=None) -> bool:
    allowed = resolve_department_ids(user, asset.company)
    if not asset.department_id or asset.department_id not in allowed:
        return False
    if target_department is None:
        return True
    return bool(
        target_department.company_id == asset.company_id
        and target_department.pk in allowed
    )


def can_lifecycle_action(user, asset, action, *, target_department=None) -> bool:
    """Apply the fixed Sprint 7 action matrix and department scope."""

    if action not in LIFECYCLE_ACTIONS or not _asset_in_company(asset):
        return False
    roles = _roles(user)

    if action == "code_correction":
        return "system_admin" in roles
    if action in {"archive", "restore_visibility"}:
        return bool(roles.intersection({"system_admin", "finance"}))
    if action in {"disposal_finance_lock", "disposal_reversal"}:
        return "finance" in roles
    if action in {
        "disposal_actual_details",
        "disposal_cancel",
        "disposal_complete",
    }:
        return bool(roles.intersection({"finance", "equipment"}))
    if action == "disposal_start":
        return bool(
            roles.intersection({"finance", "equipment"})
            or (
                "department_manager" in roles
                and _department_manager_can_move(user, asset)
            )
        )
    if action == "loan_return" and "warehouse" in roles:
        return bool(
            target_department is None
            or target_department.company_id == asset.company_id
        )
    if roles.intersection({"finance", "equipment"}):
        return True
    if "department_manager" in roles:
        return _department_manager_can_move(user, asset, target_department)
    if action == "assignment_return" and "warehouse" in roles:
        return bool(
            target_department is not None
            and target_department.company_id == asset.company_id
        )
    return False


def require_lifecycle_action(user, asset, action, *, target_department=None) -> None:
    if not can_lifecycle_action(
        user, asset, action, target_department=target_department
    ):
        raise PermissionDenied("您没有在此资产或目标部门执行该生命周期动作的权限。")


def scoped_disposals(user, company, queryset=None):
    """Apply company and asset object scope to disposal records."""

    from apps.assets.models import AssetDisposal

    queryset = queryset if queryset is not None else AssetDisposal.objects.all()
    if not _roles(user).intersection(DISPOSAL_VIEW_ROLES):
        return queryset.none()
    asset_ids = scoped_assets(user, company).values_list("pk", flat=True)
    return queryset.filter(company=company, asset_id__in=asset_ids)


def can_view_disposal(user, disposal) -> bool:
    if disposal is None or not getattr(disposal, "pk", None):
        return False
    return scoped_disposals(user, disposal.company).filter(pk=disposal.pk).exists()


def require_view_disposal(user, disposal) -> None:
    if not can_view_disposal(user, disposal):
        raise PermissionDenied("您没有查看此处置记录的权限。")


def can_view_disposal_financial_fields(user, disposal=None) -> bool:
    if not _roles(user).intersection({"finance", "management"}):
        return False
    return disposal is None or can_view_disposal(user, disposal)


def require_view_disposal_financial_fields(user, disposal=None) -> None:
    if not can_view_disposal_financial_fields(user, disposal):
        raise PermissionDenied("您没有查看处置财务快照的权限。")


def can_manage_disposal_attachment(user, disposal, *, security_class="A0") -> bool:
    roles = _roles(user)
    if disposal.status not in {"draft", "finance_locked"}:
        return False
    if security_class == "A1":
        return "finance" in roles and can_view_disposal(user, disposal)
    return bool(
        security_class == "A0"
        and roles.intersection({"finance", "equipment"})
        and can_view_disposal(user, disposal)
    )


def can_view_disposal_attachment(user, link) -> bool:
    disposal = getattr(link, "asset_disposal", None)
    if disposal is None or getattr(link, "status", None) != "active":
        return False
    if getattr(link, "security_class", None) == "A1":
        return can_view_disposal_financial_fields(user, disposal)
    return can_view_disposal(user, disposal)


def require_view_disposal_attachment(user, link) -> None:
    if not can_view_disposal_attachment(user, link):
        raise PermissionDenied("您没有查看或下载此处置附件的权限。")


__all__ = [
    "LIFECYCLE_ACTIONS",
    "TERMINAL_STATUSES",
    "can_lifecycle_action",
    "can_manage_disposal_attachment",
    "can_view_disposal",
    "can_view_disposal_attachment",
    "can_view_disposal_financial_fields",
    "require_lifecycle_action",
    "require_view_disposal",
    "require_view_disposal_attachment",
    "require_view_disposal_financial_fields",
    "scoped_disposals",
    "scoped_lifecycle_assets",
    "scoped_lifecycle_candidates",
]
