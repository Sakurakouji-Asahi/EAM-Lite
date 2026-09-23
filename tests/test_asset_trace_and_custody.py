from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.urls import reverse
from django.utils import timezone

from apps.assets.custody_services import return_custody_asset
from apps.assets.models import Asset, AssetCompositionRevision, AssetCustodyReturn, AssetMovement, AssetOriginLink
from apps.assets.trace_services import normalize_members, record_asset_composition, record_asset_origin
from apps.finance.models import AssetFinance, DepreciationEntry
from apps.finance.services import confirm_asset_finance, create_fixed_asset_category
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_unified_asset_identity import context, registered

pytestmark = pytest.mark.django_db(transaction=True)


def test_origin_keeps_both_codes_and_does_not_move_financial_values(context):
    source, target = registered(context, "source"), registered(context, "target")
    initial = list(IssuedCode.objects.order_by("pk").values_list("display_code", flat=True))
    relation = record_asset_origin(actor=context["equipment"], source=source, target=target,
        relation_type="split", reason="依据拆分记录建立来源", idempotency_key="split-source")
    replay = record_asset_origin(actor=context["equipment"], source=source, target=target,
        relation_type="split", reason="依据拆分记录建立来源", idempotency_key="split-source")
    assert relation.pk == replay.pk
    assert list(IssuedCode.objects.order_by("pk").values_list("display_code", flat=True)) == initial
    source.refresh_from_db()
    assert source.asset_status == "pending_label"
    assert not AssetFinance.objects.exists() and not DepreciationEntry.objects.exists()
    with pytest.raises(ValidationError, match="循环"):
        record_asset_origin(actor=context["equipment"], source=target, target=source,
            relation_type="merge", reason="不能形成循环", idempotency_key="cycle")


def test_composition_appends_versions_and_preserves_original_member_list(context):
    asset = registered(context, unit="组", management_attribute="LV")
    first = record_asset_composition(actor=context["equipment"], asset=asset,
        members=[{"name":"椅子","quantity":"4","unit":"把"}], reason="初始组合清单", idempotency_key="members-1")
    second = record_asset_composition(actor=context["equipment"], asset=asset,
        members=[{"name":"椅子","quantity":"3","unit":"把"}], reason="成员移出一把", idempotency_key="members-2")
    assert second.previous_revision_id == first.pk and second.revision == 2
    first.refresh_from_db()
    assert first.members[0]["quantity"] == "4.0000"
    assert asset.quantity == 1 and SequenceCounter.objects.get().current_value == 1
    assert not AssetFinance.objects.exists()
    with pytest.raises(ValidationError):
        AssetCompositionRevision.objects.filter(pk=first.pk).update(reason="覆盖旧历史")


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "0", "-1", "1.00001", "100000000000000"])
def test_composition_rejects_nonfinite_or_invalid_quantity(quantity):
    with pytest.raises(ValidationError):
        normalize_members([{"name":"测试成员","unit":"个","quantity":quantity}])


def return_values():
    return {"returned_on":timezone.localdate(), "counterparty":"测试出租方", "contract_reference":"LEASE-TEST-01",
            "acceptance_evidence":"归还交接单 RET-TEST-01 已核对", "reason":"租期结束归还", "idempotency_key":"leased-return"}


def test_leased_return_closes_custody_without_finance_or_new_code(context):
    asset = registered(context, management_attribute="LS")
    code, qr = asset.asset_code, asset.qr_identities.get(status="active")
    result = return_custody_asset(actor=context["equipment"], asset=asset, **return_values())
    replay = return_custody_asset(actor=context["equipment"], asset=asset, **return_values())
    asset.refresh_from_db()
    assert result.pk == replay.pk
    assert asset.asset_status == "other_disposed" and asset.get_asset_status_display() == "已归还"
    assert asset.asset_code == code and asset.qr_identities.get(status="active").pk == qr.pk
    assert AssetMovement.objects.get().movement_type == "custody_return"
    assert not AssetFinance.objects.exists() and not DepreciationEntry.objects.exists()
    assert IssuedCode.objects.count() == 1


def test_leased_return_checks_attribute_date_and_component_completion(context):
    plain = registered(context, "plain")
    with pytest.raises(ValidationError, match="LS"):
        return_custody_asset(actor=context["equipment"], asset=plain, **return_values())
    parent = registered(context, "parent", management_attribute="LS")
    registered(context, "component", management_attribute="", component_of=parent)
    with pytest.raises(ValidationError, match="组件"):
        return_custody_asset(actor=context["equipment"], asset=parent, **return_values())
    values = {**return_values(), "returned_on":timezone.localdate()+timedelta(days=1)}
    with pytest.raises(ValidationError, match="日期"):
        return_custody_asset(actor=context["equipment"], asset=parent, **values)
    assert not AssetCustodyReturn.objects.exists()


def test_financially_confirmed_asset_cannot_skip_financial_closing(context):
    asset = registered(context, management_attribute="LS", acquisition_date=timezone.localdate())
    category = create_fixed_asset_category(actor=context["finance"], company=context["company"],
        data={"code":"RETURN-ACC","name":"财务类别","useful_life_months_default":12})
    confirm_asset_finance(actor=context["finance"], asset=asset, finance_data={
        "accounting_treatment":"fixed_asset", "original_cost":Decimal("1200.00"),
        "fixed_asset_category":category, "capitalization_date":timezone.localdate(),
    }, profile_data={}, idempotency_key="lease-finance", reason="确认账务")
    with pytest.raises(ValidationError, match="财务"):
        return_custody_asset(actor=context["equipment"], asset=asset, **return_values())
    asset.refresh_from_db()
    assert asset.asset_status == "pending_label"
    assert asset.finance.original_cost == Decimal("1200.00")
    assert not AssetCustodyReturn.objects.exists()


def test_trace_and_return_views_preserve_readonly_get_and_apply_authorized_post(client, context):
    source, target = registered(context, "source"), registered(context, "target")
    client.force_login(context["equipment"])
    origin_url = reverse("assets:asset-origin-edit", args=[target.pk])
    assert client.get(origin_url).status_code == 200
    assert not AssetOriginLink.objects.exists()
    assert client.post(origin_url, {"source":str(source.pk), "relation_type":"merge",
        "reason":"登记合并来源，财务另办", "idempotency_key":"origin-http"}).status_code == 302
    composition_url = reverse("assets:asset-composition-edit", args=[target.pk])
    assert client.post(composition_url, {"members":"部件甲 | 2 | 个 | 现场核对", "reason":"核对组合成员",
        "idempotency_key":"composition-http"}).status_code == 302
    assert "组合成员" in client.get(reverse("assets:asset-detail", args=[target.pk])).content.decode()
    leased = registered(context, "leased", management_attribute="LS")
    url = reverse("assets:asset-custody-return", args=[leased.pk])
    assert client.get(url).status_code == 200
    assert client.post(url, return_values()).status_code == 302
    assert "已归还" in client.get(reverse("assets:asset-detail", args=[leased.pk])).content.decode()
