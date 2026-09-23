"""Shared business labels for current pages and dated reports."""
from django.db.models import Exists, OuterRef, Subquery


def with_identity_display(queryset):
    from apps.assets.models import AssetCustodyReturn, AssetLabelAttachmentRequest
    returns = AssetCustodyReturn.objects.filter(asset_id=OuterRef("pk"), reversal__isnull=True)
    confirmations = AssetLabelAttachmentRequest.objects.filter(
        asset_id=OuterRef("pk"), qr_identity__status="active",
    ).order_by("-completed_at", "-pk")
    return queryset.annotate(
        _has_custody_return=Exists(returns),
        _custody_returned_on=Subquery(returns.values("returned_on")[:1]),
        _identification_method=Subquery(confirmations.values("identification_method")[:1]),
    )


def asset_status_display(asset, status=None, *, as_of=None):
    status = asset.asset_status if status is None else status
    if status == "pending_label" and asset.management_attribute == "IA":
        return "待电子台账确认"
    if status == "other_disposed" and asset.management_attribute == "LS":
        if hasattr(asset, "_has_custody_return"):
            returned = asset._has_custody_return and (as_of is None or asset._custody_returned_on <= as_of)
        else:
            receipt = asset.custody_return_record
            returned = receipt is not None and (as_of is None or receipt.returned_on <= as_of)
        if returned:
            return "已归还"
    return dict(asset.AssetStatus.choices).get(status, status)


def label_status_display(asset, status, method=None):
    from apps.assets.models import AssetQrIdentity
    if asset_status_display(asset) == "已归还":
        return "资产已归还"
    if status == "attached":
        if method == "electronic":
            return "电子台账已确认"
        if method == "alternative":
            return "替代标识已确认"
    if asset.management_attribute == "IA" and status in {"ready_to_print", "printed"}:
        return "待电子台账确认"
    return dict(AssetQrIdentity.LabelStatus.choices).get(status, "未生成")
