"""Production-path factories shared by the Sprint 7 acceptance tests.

The helpers deliberately formalize, print, and attach an asset through the
existing Sprint 4/6 services.  Sprint 7 tests therefore start from a genuine
formal asset instead of bypassing the lifecycle preconditions with ORM
updates.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from apps.assets.qr_services import (
    confirm_label_attachment,
    confirm_print_batch,
    generate_print_batch,
)
from apps.assets.models import AssetQrIdentity
from apps.finance.models import AssetDepreciationProfile, DepreciationPolicy
from apps.finance.services import (
    activate_depreciation_policy,
    confirm_asset_finance,
    create_depreciation_policy,
    create_fixed_asset_category,
)
from apps.masterdata.services import set_system_setting
from tests.test_sprint4_acceptance import _base_context, _pending_asset
from tests.test_sprint3_support import (
    grant_scope,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
)
from tests.test_sprint6_support import formal_asset_context


def active_asset_context(
    prefix: str,
    *,
    status: str = "in_use",
    cost=Decimal("1234.56"),
):
    """Return a real attached formal asset and its current QR identity."""

    context, asset, qr_identity = formal_asset_context(prefix, cost=cost)
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key=f"{prefix}-print",
    )
    confirm_print_batch(actor=context["finance"], batch=batch)
    qr_identity.refresh_from_db()
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr_identity.public_token,
        target_status=status,
        idempotency_key=f"{prefix}-attach",
    )
    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    return context, asset, qr_identity


def add_target_assignment(context: dict, prefix: str):
    """Create an enabled same-company department/employee/leaf location set."""

    department = make_department(context["company"], f"{prefix}-D2")
    employee = make_employee(
        context["company"], department, f"{prefix}-E2"
    )
    _site, _area, location = make_location_tree(
        context["company"], f"{prefix}-L2"
    )
    return department, employee, location


def add_department_manager(context: dict, prefix: str, *departments):
    """Create a department manager with explicit non-descendant scopes."""

    user = make_user(f"{prefix.lower()}-manager", "department_manager")
    for department in departments:
        grant_scope(
            user,
            context["company"],
            department,
            descendants=False,
            assigned_by=context["admin"],
        )
    return user


def active_fixed_asset_context(prefix: str, *, stop_rule="event_date"):
    """Formalize and attach a fixed asset with a current-month profile."""

    context = _base_context(prefix, include_policy=False)
    set_system_setting(
        actor=context["finance"], company=context["company"],
        key="fixed_asset_warning_amount", value="5000.00",
    )
    policy = create_depreciation_policy(
        actor=context["finance"], company=context["company"],
        data={
            "policy_key": f"{prefix}-POLICY",
            "name": f"{prefix} 当月计提政策",
            "method": "straight_line",
            "posting_period": "monthly",
            "start_rule": "current_month",
            "stop_rule": stop_rule,
            "default_useful_life_months": 60,
            "default_salvage_mode": "rate",
            "default_salvage_rate": Decimal("0.05"),
            "default_salvage_amount": None,
            "annual_posting_month": None,
            "work_unit": "",
            "effective_from": date(2020, 1, 1),
            "effective_to": None,
        },
    )
    policy = activate_depreciation_policy(
        actor=context["finance"], policy=policy, make_default=True,
        reason="Sprint 7 固定资产测试启用政策",
    )
    fixed_category = create_fixed_asset_category(
        actor=context["finance"], company=context["company"],
        data={
            "code": f"{prefix}-FA",
            "name": f"{prefix} 固定资产类别",
            "useful_life_months_default": 60,
        },
    )
    asset = _pending_asset(context, prefix)
    asset = confirm_asset_finance(
        actor=context["finance"], asset=asset,
        finance_data={
            "accounting_treatment": "fixed_asset",
            "fixed_asset_category": fixed_category,
            "original_cost": Decimal("12000.00"),
            "capitalization_date": timezone.localdate(),
        },
        code_effective_date=timezone.localdate(),
        idempotency_key=f"{prefix}-formalize",
        reason="Sprint 7 固定资产处置测试正式化",
    )
    batch = generate_print_batch(
        actor=context["finance"], assets=[asset],
        idempotency_key=f"{prefix}-print",
    )
    confirm_print_batch(actor=context["finance"], batch=batch)
    qr_identity = AssetQrIdentity.objects.get(asset=asset, status="active")
    confirm_label_attachment(
        actor=context["finance"], asset=asset,
        scanned_token=qr_identity.public_token, target_status="in_use",
        idempotency_key=f"{prefix}-attach",
    )
    asset.refresh_from_db()
    profile = AssetDepreciationProfile.objects.get(asset=asset, status="active")
    return context, asset, qr_identity, profile, policy
