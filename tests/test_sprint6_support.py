"""Shared production-path fixtures for Sprint 6 QR label tests."""

from __future__ import annotations

from decimal import Decimal

from apps.assets.models import AssetQrIdentity
from tests.test_sprint4_acceptance import (
    _base_context,
    _confirm_nonfixed,
    _pending_asset,
)


def formal_asset_context(prefix: str, *, cost=Decimal("1234.56")):
    context = _base_context(prefix)
    asset = _pending_asset(context, prefix)
    asset = _confirm_nonfixed(
        context,
        asset,
        cost=cost,
        key=f"{prefix}-formalize",
    )
    asset.refresh_from_db()
    qr_identity = AssetQrIdentity.objects.get(
        asset=asset,
        status=AssetQrIdentity.Status.ACTIVE,
    )
    return context, asset, qr_identity
