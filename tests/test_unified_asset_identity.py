from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.assets.models import Asset, AssetIdentity, AssetRegistration, AssetQrIdentity
from apps.assets.registration import create_registered_asset
from apps.coding.services import activate_scheme, create_scheme, set_default_scheme
from apps.coding.standard import standard_segments
from apps.finance.models import AssetFinance, DepreciationEntry
from apps.finance.services import confirm_asset_finance, create_fixed_asset_category
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_sprint3_support import make_category, make_user
from tests.test_sprint4_acceptance import _base_context, _mark_initialized

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def context():
    context = _base_context("UNIFIED", initialize=False)
    category = context["category"]
    category.code = "02"
    category.normalized_code = "02"
    category.name = "机器设备"
    category.save()
    rule = create_scheme(actor=context["admin"], company=context["company"], data={
        "scheme_key": "UNIFIED", "name": "统一资产编码", "reset_mode": "category_yearly",
        "category_scope_level": "major", "sequence_start": 1, "effective_from": timezone.localdate(),
    }, segments=standard_segments())
    rule = activate_scheme(actor=context["admin"], scheme=rule)
    set_default_scheme(actor=context["admin"], scheme=rule)
    _mark_initialized(context["company"], context["admin"])
    context["standard_scheme"] = rule
    return context


def physical_data(context, **overrides):
    return {"asset_name": "统一编码设备", "category": context["category"], "unit": "台",
            "department": context["department"], "responsible_employee": context["employee"],
            "location": context["location"], "management_attribute": "FA",
            "acquisition_date": date(2020, 8, 1), "commissioning_date": timezone.localdate(), **overrides}


def registered(context, key="standard-asset", **overrides):
    return create_registered_asset(actor=context["equipment"], company=context["company"],
                                   data=physical_data(context, **overrides), idempotency_key=key)


def test_first_identity_uses_acquisition_year_not_rule_effective_year(context):
    asset = registered(context)
    assert asset.asset_code == "FA-02-2020-000001-00"
    assert asset.current_issued_code.effective_date == timezone.localdate()
    assert asset.identity.year_source == "acquisition_date"
    assert asset.management_attribute == "FA" and asset.coding_year == 2020
    assert asset.asset_status == "pending_label"
    assert not AssetFinance.objects.exists()
    assert not asset.attachment_links.exists()


def test_attributes_categories_and_years_have_independent_sequences(context):
    first = registered(context, "fa-1")
    second = registered(context, "fa-2")
    lv = registered(context, "lv-1", management_attribute="LV")
    year = registered(context, "fa-2021", acquisition_date=date(2021, 1, 1))
    other_category = make_category(context["company"], "03")
    other = registered(context, "fa-other", category=other_category)
    assert [item.asset_code for item in (first, second, lv, year, other)] == [
        "FA-02-2020-000001-00", "FA-02-2020-000002-00", "LV-02-2020-000001-00",
        "FA-02-2021-000001-00", "FA-03-2020-000001-00",
    ]
    assert SequenceCounter.objects.count() == 4


def test_components_share_parent_prefix_without_consuming_root_sequence(context):
    parent = registered(context, "parent")
    child = registered(context, "child", component_of=parent, management_attribute="",
                       acquisition_date=timezone.localdate())
    another = registered(context, "child-2", component_of=parent, management_attribute="")
    next_root = registered(context, "root-2")
    assert child.asset_code == "FA-02-2020-000001-01"
    assert another.asset_code == "FA-02-2020-000001-02"
    assert next_root.asset_code == "FA-02-2020-000002-00"
    assert child.identity.parent_identity_id == parent.identity.pk
    assert child.identity.year_source == "parent"
    assert child.acquisition_date == timezone.localdate()
    assert not AssetFinance.objects.exists()
    assert SequenceCounter.objects.get().current_value == 2


@pytest.mark.parametrize("treatment", ["controlled_non_fixed", "fixed_asset"])
def test_finance_recognition_does_not_change_original_management_identity(context, treatment):
    attribute = "FA" if treatment == "controlled_non_fixed" else "LV"
    asset = registered(context, "before-finance", management_attribute=attribute,
                       acquisition_date=timezone.localdate())
    original = (asset.asset_code, asset.current_issued_code_id, asset.identity.pk,
                asset.qr_identities.get(status="active").pk)
    finance_data = {"accounting_treatment": treatment, "original_cost": Decimal("1200.00"),
                    "accounting_treatment_reason": "依据票据确认，与建档管理属性独立保存"}
    if treatment == "fixed_asset":
        category = create_fixed_asset_category(actor=context["finance"], company=context["company"],
            data={"code": "ACC", "name": "会计设备类", "useful_life_months_default": 12})
        finance_data.update({"fixed_asset_category": category, "capitalization_date": timezone.localdate()})
    confirmed = confirm_asset_finance(actor=context["finance"], asset=asset, finance_data=finance_data,
        profile_data={}, idempotency_key="finance-independent", reason="发票资料齐备")
    assert (confirmed.asset_code, confirmed.current_issued_code_id, confirmed.identity.pk,
            confirmed.qr_identities.get(status="active").pk) == original
    assert confirmed.management_attribute == attribute
    assert confirmed.finance.accounting_treatment == treatment
    assert AssetIdentity.objects.count() == IssuedCode.objects.count() == 1
    assert not DepreciationEntry.objects.exists()


def test_replay_does_not_take_another_identity(context):
    first = registered(context)
    replay = registered(context)
    assert first.pk == replay.pk
    assert Asset.objects.count() == AssetIdentity.objects.count() == AssetRegistration.objects.count() == 1
    assert SequenceCounter.objects.get().current_value == 1


def test_failed_identity_creation_rolls_back_everything(context, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("identity persistence failed")
    monkeypatch.setattr(AssetIdentity, "save", fail)
    with pytest.raises(RuntimeError, match="identity persistence failed"):
        registered(context)
    assert not Asset.objects.exists()
    assert not AssetIdentity.objects.exists()
    assert not SequenceCounter.objects.exists()
    assert not IssuedCode.objects.exists()
    assert not AssetQrIdentity.objects.exists()


def test_management_fields_are_immutable_after_registration(context):
    asset = registered(context)
    asset.management_attribute = "LV"
    with pytest.raises(ValidationError, match="冻结"):
        asset.save(update_fields=["management_attribute"])
    if connection.vendor == "postgresql":
        with pytest.raises(IntegrityError), transaction.atomic():
            QuerySet.update(Asset.objects.filter(pk=asset.pk), management_attribute="LV")
    asset.refresh_from_db()
    assert asset.management_attribute == "FA"


def test_component_requires_parent_access_and_matching_management_attribute(context):
    parent = registered(context, "parent")
    with pytest.raises(ValidationError, match="沿用"):
        registered(context, "mismatched", component_of=parent, management_attribute="LV")
    outsider = make_user("outsider", "employee")
    with pytest.raises(PermissionDenied):
        create_registered_asset(actor=outsider, company=context["company"],
            data=physical_data(context, component_of=parent), idempotency_key="outside")
    assert Asset.objects.count() == 1


def test_historical_year_override_requires_evidence_and_unknown_year_records_source(context):
    with pytest.raises(ValidationError, match="依据"):
        registered(context, "missing-year-evidence", coding_year=2019)
    historical = registered(context, "historic", coding_year=2019, coding_year_note="依据原验收单年份")
    unknown = registered(context, "unknown", acquisition_date=None)
    assert historical.asset_code == "FA-02-2019-000001-00"
    assert historical.identity.year_source == "specified"
    assert unknown.coding_year == timezone.localdate().year
    assert unknown.identity.year_source == "inclusion_year"


def test_electronic_equipment_requires_serial_under_standard_rule(context):
    category = make_category(context["company"], "04")
    with pytest.raises(ValidationError, match="序列号"):
        registered(context, "no-serial", category=category)
    asset = registered(context, "with-serial", category=category, serial_number="SN-001")
    assert asset.asset_code == "FA-04-2020-000001-00"
