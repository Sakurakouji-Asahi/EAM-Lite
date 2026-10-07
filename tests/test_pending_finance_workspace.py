from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.assets.registration import create_registered_asset
from apps.audit.models import AuditLog
from apps.finance.models import AssetFinance, DepreciationEntry, FinanceFormalizationRequest
from apps.finance.pending_workspace import pending_navigation
from tests.test_bulk_finance_confirmation import simple_asset
from tests.test_unified_asset_identity import context, physical_data
from tests.test_usability_workflows import values


pytestmark = pytest.mark.django_db(transaction=True)


def query_of(url):
    return {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}


def test_status_shortcuts_count_with_other_filters_and_remove_one_filter(context, client):
    saved = simple_asset(context, "目标已保存")
    missing = create_registered_asset(actor=context["equipment"], company=context["company"],
        data=physical_data(context, asset_name="目标未填写"), idempotency_key="missing")
    simple_asset(context, "其他设备")
    client.force_login(context["finance"])
    response = client.get(reverse("finance:pending-list"), {
        "q": "目标", "department": context["department"].pk, "data_status": "saved", "page": "7",
    })
    assert response.status_code == 200
    assert [asset.pk for asset in response.context["assets"]] == [saved.pk]
    cards = response.context["pending_status_cards"]
    assert [card["count"] for card in cards] == [2, 1, 1]
    assert [card["active"] for card in cards] == [False, False, True]
    for card in cards:
        query = query_of(card["url"])
        assert query["q"] == "目标" and query["department"] == str(context["department"].pk)
        assert "page" not in query
    status_chip = response.context["pending_filter_summary"][-1]
    assert "已保存财务资料" in status_chip["label"]
    assert "data_status" not in query_of(status_chip["remove_url"])
    assert query_of(status_chip["remove_url"])["q"] == "目标"
    restored = client.get(cards[1]["url"])
    assert [asset.pk for asset in restored.context["assets"]] == [missing.pk]
    confirm_link = response.context["assets"][0].finance_confirm_url
    assert query_of(confirm_link)["pending_query"] == response.context["pending_query"]


def test_save_confirm_and_completed_reentry_keep_original_list_navigation(context, client):
    asset = simple_asset(context, "继续办理")
    client.force_login(context["finance"])
    pending_query = urlencode({"q": "继续办理", "department": context["department"].pk,
        "data_status": "saved", "page_size": "25", "page": "2"})
    url = reverse("finance:finance-confirm", args=[asset.pk])
    data = {"action": "save", "accounting_treatment": "controlled_non_fixed", "original_cost": "1250.50",
        "pending_query": pending_query}
    saved = client.post(url, data)
    assert saved.status_code == 302 and query_of(saved.url)["pending_query"] == pending_query
    page = client.get(saved.url)
    assert query_of(page.context["pending_return_url"])["page"] == "2"
    assert 'data-unsaved-guard=""' in page.content.decode()
    data.update(action="confirm", confirm_permanent_code="on", action_reason="已核对本项资料")
    confirmed = client.post(url, data)
    assert confirmed.status_code == 302 and query_of(confirmed.url)["pending_query"] == pending_query
    detail = client.get(confirmed.url)
    assert "继续办理财务待办" in detail.content.decode()
    assert detail.context["pending_return_url"] == reverse("finance:pending-list") + "?" + pending_query
    assert AssetFinance.objects.get(asset=asset).original_cost == Decimal("1250.50")
    count = FinanceFormalizationRequest.objects.count()
    replay = client.get(url, {"pending_query": pending_query})
    assert replay.url == confirmed.url and FinanceFormalizationRequest.objects.count() == count


def test_trial_and_validation_errors_keep_navigation_and_mark_unsaved(context, client):
    asset, finance, profile = values(context)
    client.force_login(context["finance"])
    pending_query = "q=trial&page=3"
    data = {"accounting_treatment": "fixed_asset", "original_cost": "12000.00",
        "fixed_asset_category": finance["fixed_asset_category"].pk, "capitalization_date": "2026-08-01",
        "depreciation_policy": context["policy"].pk, "useful_life_months": "72", "salvage_mode": "amount",
        "salvage_amount": "600.00", "method": "manual", "start_rule": "specified_date",
        "specified_start_date": "2026-09-01", "actual_continuation_date": "2026-09-01",
        "opening_actual_accumulated_depreciation": "0.00", "opening_impairment": "0.00",
        "pending_query": pending_query}
    trial = client.post(reverse("finance:finance-preview", args=[asset.pk]), data)
    assert trial.status_code == 200 and not trial.context["form"].errors
    assert trial.context["pending_query"] == pending_query
    assert 'data-unsaved-guard="true"' in trial.content.decode()
    assert "本页试算与填写内容尚未保存" in trial.content.decode()
    assert not AssetFinance.objects.filter(asset=asset).exists() and not DepreciationEntry.objects.exists()
    data.update(action="save", useful_life_months="-2", finance_remark="核对后继续保留此备注")
    invalid = client.post(reverse("finance:finance-confirm", args=[asset.pk]), data)
    assert invalid.context["form"].errors and invalid.context["pending_query"] == pending_query
    assert invalid.context["form"]["useful_life_months"].value() == "-2"
    assert "核对后继续保留此备注" in invalid.content.decode()
    assert 'data-unsaved-guard="true"' in invalid.content.decode()
    assert not AssetFinance.objects.filter(asset=asset).exists()


def test_bulk_review_and_result_keep_navigation_without_changing_signed_confirmation(context, client):
    ready = simple_asset(context, "批量已保存")
    missing = create_registered_asset(actor=context["equipment"], company=context["company"],
        data=physical_data(context, asset_name="批量未填写"), idempotency_key="bulk-missing")
    client.force_login(context["finance"])
    pending_query = "q=bulk&data_status=saved&page=2"
    url = reverse("finance:bulk-confirmation")
    before = AuditLog.objects.count()
    preview = client.post(url, {"action": "preview", "assets": [ready.pk, missing.pk], "pending_query": pending_query})
    assert preview.status_code == 200 and preview.context["preview"]["ready_count"] == 1
    assert AuditLog.objects.count() == before
    assert query_of(preview.context["preview"]["rows"][1]["finance_confirm_url"])["pending_query"] == pending_query
    result = client.post(url, {"action": "confirm", "token": preview.context["preview"]["token"],
        "acknowledge": "on", "pending_query": pending_query})
    assert result.status_code == 200 and result.context["result"]["success_count"] == 1
    success = next(row for row in result.context["result"]["rows"] if not row["error"])
    assert query_of(success["finance_detail_url"])["pending_query"] == pending_query
    assert query_of(result.context["pending_return_url"])["page"] == "2"
    assert not AssetFinance.objects.filter(asset=missing).exists()


def test_return_metadata_is_limited_to_list_fields():
    request = RequestFactory().get("/finance/confirm/", {"pending_query":
        "next=https%3A%2F%2Fexample.com&q=keep&page=4&token=secret&data_status=invented&page_size=1"})
    navigation = pending_navigation(request)
    assert navigation["pending_query"] == "q=keep&page=4"
    assert navigation["pending_return_url"] == reverse("finance:pending-list") + "?q=keep&page=4"
