from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.assets.models import Asset, AssetCodeHistory, AssetQrIdentity, AssetRegistration, AttachmentLink
from apps.assets.registration import create_registered_asset, register_asset
from apps.assets.qr_services import confirm_label_attachment, generate_print_batch
from apps.assets.services import create_asset_draft, upload_asset_attachment
from apps.audit.models import AuditLog
from apps.finance.models import AssetDepreciationProfile, AssetFinance, DepreciationEntry
from apps.finance.readiness import pending_finance_assets
from apps.finance.services import confirm_asset_finance, create_fixed_asset_category
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_sprint3_support import add_photo, make_user
from tests.test_sprint4_acceptance import _base_context


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def context():
    return _base_context("REGSEP")


def physical_data(context, **overrides):
    return {
        "asset_name": "先使用后收票的设备", "category": context["category"],
        "unit": "台", "department": context["department"],
        "responsible_employee": context["employee"], "location": context["location"],
        "commissioning_date": timezone.localdate() - timedelta(days=90),
        **overrides,
    }


def registered(context, key="physical-create"):
    return create_registered_asset(
        actor=context["equipment"], company=context["company"],
        data=physical_data(context), idempotency_key=key,
    )


def activate(context, asset):
    generate_print_batch(actor=context["equipment"], assets=[asset], idempotency_key="print-" + str(asset.pk))
    qr = asset.qr_identities.get(status="active")
    confirm_label_attachment(
        actor=context["equipment"], asset=asset, scanned_token=qr.public_token,
        target_status="in_use", idempotency_key="attach-" + str(asset.pk),
        confirmation_method="web",
    )
    asset.refresh_from_db()
    return qr


def test_physical_registration_needs_neither_photo_nor_finance(context):
    asset = registered(context)
    assert asset.asset_code and asset.asset_status == "pending_label"
    assert asset.registration.source == "physical"
    assert not AssetFinance.objects.filter(asset=asset).exists()
    assert not AssetDepreciationProfile.objects.filter(asset=asset).exists()
    assert not DepreciationEntry.objects.filter(asset=asset).exists()
    assert not AttachmentLink.objects.filter(asset=asset).exists()
    activate(context, asset)
    assert asset.asset_status == "in_use"
    assert pending_finance_assets(Asset.objects.filter(company=context["company"])).filter(pk=asset.pk).exists()


def test_physical_registration_does_not_require_depreciation_policy():
    context = _base_context("REGNOFIN", include_policy=False)
    asset = registered(context)
    assert asset.asset_code
    assert not AssetFinance.objects.exists()


def test_create_registration_is_idempotent_and_rejects_changed_payload(context):
    first = registered(context)
    second = registered(context)
    assert first.pk == second.pk
    assert Asset.objects.count() == AssetRegistration.objects.count() == IssuedCode.objects.count() == 1
    with pytest.raises(ValidationError):
        create_registered_asset(
            actor=context["equipment"], company=context["company"],
            data=physical_data(context, asset_name="另一项资产"), idempotency_key="physical-create",
        )
    assert Asset.objects.count() == 1


def test_registration_failure_rolls_back_draft_counter_code_qr_and_audit(context, monkeypatch):
    baseline = AuditLog.objects.count()

    def fail(*args, **kwargs):
        raise RuntimeError("simulate registration persistence failure")

    monkeypatch.setattr(AssetRegistration.objects, "create", fail)
    with pytest.raises(RuntimeError):
        registered(context)
    assert not Asset.objects.exists()
    assert not AssetRegistration.objects.exists()
    assert not SequenceCounter.objects.exists()
    assert not IssuedCode.objects.exists()
    assert not AssetCodeHistory.objects.exists()
    assert not AssetQrIdentity.objects.exists()
    assert AuditLog.objects.count() == baseline


def test_registration_checks_role_and_physical_fields(context):
    with pytest.raises(PermissionDenied):
        create_registered_asset(actor=context["admin"], company=context["company"],
                                data=physical_data(context), idempotency_key="admin-refused")
    with pytest.raises(ValidationError):
        create_registered_asset(actor=context["equipment"], company=context["company"],
                                data=physical_data(context, location=None), idempotency_key="missing-location")
    assert not Asset.objects.exists()


def test_existing_draft_registers_without_finance_and_replays(context):
    asset = create_asset_draft(actor=context["equipment"], company=context["company"], data=physical_data(context))
    first = register_asset(actor=context["equipment"], asset=asset, idempotency_key="register-draft")
    again = register_asset(actor=context["equipment"], asset=first, idempotency_key="register-draft")
    assert again.asset_code == first.asset_code
    assert AssetRegistration.objects.count() == IssuedCode.objects.count() == 1


def test_finance_confirms_depreciation_later_without_reissuing_identity(context):
    asset = registered(context)
    qr = activate(context, asset)
    code, issued_id, qr_id = asset.asset_code, asset.current_issued_code_id, qr.pk
    category = create_fixed_asset_category(actor=context["finance"], company=context["company"], data={
        "code": "REGFIXED", "name": "设备", "useful_life_months_default": 12,
    })
    start = (timezone.localdate() - timedelta(days=60)).replace(day=1)
    data = {
        "accounting_treatment": "fixed_asset", "original_cost": Decimal("1200.00"),
        "fixed_asset_category": category, "capitalization_date": start,
    }
    profile = {
        "start_rule": "specified_month", "specified_start": start,
        "actual_continuation_date": start, "useful_life_months": 12,
        "salvage_mode": "amount", "salvage_amount": Decimal("0.00"),
    }
    with pytest.raises(PermissionDenied):
        confirm_asset_finance(actor=context["equipment"], asset=asset, finance_data=data,
                              profile_data=profile, idempotency_key="finance-later", reason="资料齐备")
    confirmed = confirm_asset_finance(actor=context["finance"], asset=asset, finance_data=data,
                                     profile_data=profile, idempotency_key="finance-later", reason="资料齐备")
    assert confirmed.asset_status == "in_use"
    assert (confirmed.asset_code, confirmed.current_issued_code_id) == (code, issued_id)
    assert confirmed.qr_identities.get(status="active").pk == qr_id
    assert AssetRegistration.objects.count() == IssuedCode.objects.count() == 1
    assert confirmed.finance.finance_confirmed_at is not None
    assert confirmed.depreciation_profiles.get(status="active").start_date == start
    assert not pending_finance_assets(Asset.objects.all()).filter(pk=asset.pk).exists()
    replay = confirm_asset_finance(actor=context["finance"], asset=confirmed, finance_data=data,
                                  profile_data=profile, idempotency_key="finance-later", reason="资料齐备")
    assert replay.pk == confirmed.pk
    assert not DepreciationEntry.objects.filter(asset=asset).exists()


def test_photo_can_be_added_after_registration(context):
    asset = registered(context)
    activate(context, asset)
    link = add_photo(actor=context["equipment"], asset=asset)
    assert link.asset_id == asset.pk
    asset.refresh_from_db()
    assert asset.asset_status == "in_use"
    assert not AssetFinance.objects.filter(asset=asset).exists()


def test_finance_failure_keeps_previously_registered_physical_identity(context, monkeypatch):
    asset = registered(context)
    qr = activate(context, asset)
    before = (asset.asset_code, asset.current_issued_code_id, qr.pk, asset.registration.pk)

    def fail_confirmation_audit(**kwargs):
        if kwargs.get("action") == "asset_finance_confirm":
            raise RuntimeError("finance audit failed")

    monkeypatch.setattr("apps.finance.services._audit", fail_confirmation_audit)
    with pytest.raises(RuntimeError, match="finance audit failed"):
        confirm_asset_finance(
            actor=context["finance"], asset=asset,
            finance_data={"accounting_treatment": "controlled_non_fixed", "original_cost": Decimal("1000.00")},
            idempotency_key="finance-rollback", reason="核对资料",
        )
    asset.refresh_from_db()
    assert asset.asset_status == "in_use"
    assert (asset.asset_code, asset.current_issued_code_id,
            asset.qr_identities.get(status="active").pk, asset.registration.pk) == before
    assert not AssetFinance.objects.filter(asset=asset).exists()
    assert not AssetDepreciationProfile.objects.filter(asset=asset).exists()
    assert not DepreciationEntry.objects.filter(asset=asset).exists()


def test_unconfirmed_finance_never_becomes_a_zero_cost_disposal_snapshot(context):
    from apps.assets.lifecycle_services import _balances_at

    asset = registered(context)
    with pytest.raises(ValidationError, match="尚未完成财务与折旧确认"):
        with transaction.atomic():
            _balances_at(asset=asset, cutoff=timezone.localdate())
