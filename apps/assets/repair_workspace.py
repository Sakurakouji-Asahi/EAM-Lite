"""Read-only context for following up the current repair of a visible asset."""
from django import forms
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from apps.assets.models import Asset, AssetMovement
from apps.assets.permissions import ASSET_GLOBAL_P1_VIEW_ROLES, scoped_assets_p1
from apps.masterdata.permissions import role_names_for


def can_open_repair_workspace(actor):
    return bool(role_names_for(actor).intersection(
        ASSET_GLOBAL_P1_VIEW_ROLES | {"department_manager", "employee"}
    ))


def latest_repair_start(queryset):
    # Keep the same pairing and ordering as complete_asset_repair.
    return queryset.filter(movement_type="repair_start").order_by("-effective_at", "-created_at")


def repair_rows(actor, company):
    latest = latest_repair_start(AssetMovement.objects.filter(asset_id=OuterRef("pk")))
    return scoped_assets_p1(actor, company).filter(
        record_status=Asset.RecordStatus.ACTIVE, asset_status=Asset.AssetStatus.UNDER_REPAIR,
    ).select_related("department", "responsible_employee", "location").annotate(
        repair_start_id=Subquery(latest.values("pk")[:1]),
        repair_started_at=Subquery(latest.values("effective_at")[:1]),
        repair_reason=Subquery(latest.values("reason")[:1]),
    )


_NOT_LOADED = object()


def repair_context(asset, *, start=_NOT_LOADED):
    if asset.asset_status != Asset.AssetStatus.UNDER_REPAIR:
        return None
    if start is _NOT_LOADED:
        start = latest_repair_start(asset.movements).first()
    if start is None or start.from_status not in {"in_use", "idle"}:
        return {"start": None}
    days = max(0, (timezone.localdate() - timezone.localdate(start.effective_at)).days)
    return {"start": start, "elapsed_days": days,
            "restore_status": Asset.AssetStatus(start.from_status).label}


class RepairFilter(forms.Form):
    q = forms.CharField(label="资产、责任人或送修原因", required=False, max_length=200,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "资产编号、设备编号、名称、责任人或送修原因"}))
    order = forms.ChoiceField(label="排列方式", required=False,
        choices=(("oldest", "送修时间从早到晚"), ("recent", "最近送修优先")),
        widget=forms.Select(attrs={"class": "form-select"}))
