"""Sprint 6 QR/label permissions and asset scope helpers."""
from django.core.exceptions import PermissionDenied

from apps.assets.permissions import scoped_assets
from apps.masterdata.permissions import role_names_for


QR_ACTION_ROLES = frozenset({"finance", "equipment", "warehouse"})


def can_manage_labels(user, asset) -> bool:
    return bool(
        role_names_for(user).intersection(QR_ACTION_ROLES)
        and scoped_assets(user, asset.company).filter(pk=asset.pk).exists()
    )


def require_label_action(user, asset) -> None:
    if not can_manage_labels(user, asset):
        raise PermissionDenied("您没有对此资产执行标签操作的权限。")


def scoped_printable_assets(user, company, queryset=None):
    if not role_names_for(user).intersection(QR_ACTION_ROLES):
        return scoped_assets(user, company, queryset).none()
    return scoped_assets(user, company, queryset)


def scoped_scannable_assets(user, company, queryset=None):
    """Scanning reuses the single company/department/person asset scope."""
    return scoped_assets(user, company, queryset)
