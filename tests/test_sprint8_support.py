"""Production-path factories shared by the Sprint 8 inventory tests.

The helpers deliberately create formal, printed, attached assets through the
Sprint 4/6 services.  Inventory acceptance tests therefore exercise genuine
business preconditions instead of manufacturing formal assets with ORM
updates.
"""

from __future__ import annotations

from decimal import Decimal

from apps.assets.models import AssetQrIdentity
from apps.assets.qr_services import (
    confirm_label_attachment,
    confirm_print_batch,
    generate_print_batch,
)
from tests.test_sprint4_acceptance import _confirm_nonfixed, _pending_asset
from tests.test_sprint7_support import active_asset_context


def inventory_context(prefix: str):
    """Return one initialized company with a genuine in-use asset and QR."""

    return active_asset_context(prefix)


def add_active_asset(
    context: dict,
    prefix: str,
    *,
    department=None,
    employee=None,
    location=None,
    category=None,
    status: str = "in_use",
    cost=Decimal("100.00"),
):
    """Create another attached formal asset in an existing test company."""

    asset_context = dict(context)
    asset_context.update(
        department=department or context["department"],
        employee=employee or context["employee"],
        location=location or context["location"],
        category=category or context["category"],
    )
    asset = _pending_asset(asset_context, prefix)
    asset = _confirm_nonfixed(
        asset_context,
        asset,
        cost=cost,
        key=f"{prefix}-formalize",
    )
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key=f"{prefix}-print",
    )
    confirm_print_batch(actor=context["finance"], batch=batch)
    identity = AssetQrIdentity.objects.get(
        asset=asset,
        status=AssetQrIdentity.Status.ACTIVE,
    )
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=identity.public_token,
        target_status=status,
        idempotency_key=f"{prefix}-attach",
    )
    asset.refresh_from_db()
    identity.refresh_from_db()
    return asset, identity
