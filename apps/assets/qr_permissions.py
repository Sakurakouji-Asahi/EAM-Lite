"""Sprint 6 QR/label permissions and asset scope helpers."""
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.assets.permissions import scoped_assets
from apps.masterdata.permissions import role_names_for


QR_ACTION_ROLES = frozenset({"finance", "equipment", "warehouse"})
QR_TERMINAL_ASSET_STATUSES = frozenset({"disposed", "sold", "other_disposed"})


def can_manage_labels(user, asset) -> bool:
    return bool(
        getattr(asset, "record_status", None) == "active"
        and getattr(asset, "asset_status", None) not in QR_TERMINAL_ASSET_STATUSES
        and role_names_for(user).intersection(QR_ACTION_ROLES)
        and scoped_assets(user, asset.company).filter(pk=asset.pk).exists()
    )


def require_label_action(user, asset) -> None:
    if not can_manage_labels(user, asset):
        raise PermissionDenied("您没有对此资产执行标签操作的权限。")


def scoped_printable_assets(user, company, queryset=None):
    if not role_names_for(user).intersection(QR_ACTION_ROLES):
        return scoped_assets(user, company, queryset).none()
    return scoped_assets(user, company, queryset).filter(
        record_status="active"
    ).exclude(asset_status__in=QR_TERMINAL_ASSET_STATUSES)


def scoped_scannable_assets(user, company, queryset=None):
    """Add only current maintenance assignments to the normal asset scope.

    A maintenance assignee needs the QR landing page to perform the assigned
    action, but this does not grant ordinary asset-ledger or financial access.
    """

    from apps.assets.models import Asset
    from apps.maintenance.models import MaintenancePlan
    from apps.maintenance.permissions import scoped_maintenance_plans

    source = queryset if queryset is not None else Asset.objects.all()
    ordinary_ids = scoped_assets(user, company, source).values("pk")
    assigned_asset_ids = scoped_maintenance_plans(
        user,
        company,
        MaintenancePlan.objects.filter(status=MaintenancePlan.Status.ACTIVE),
    ).values("asset_id")
    return source.filter(company=company).filter(
        Q(pk__in=ordinary_ids) | Q(pk__in=assigned_asset_ids)
    ).distinct()
