from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.assets.registration import create_registered_asset
from apps.finance.batch_workspace import batch_list_navigation
from apps.finance.models import DepreciationBatch, DepreciationEntry
from apps.finance.services import confirm_asset_finance, create_fixed_asset_category, generate_depreciation_batch
from tests.test_sprint3_support import make_company, make_user
from tests.test_unified_asset_identity import context, physical_data


pytestmark = pytest.mark.django_db(transaction=True)


def query_of(url):
    return {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}


@pytest.fixture
def assets(context):
    category = create_fixed_asset_category(actor=context["finance"], company=context["company"],
        data={"code": "LOOKUP", "name": "批次查找设备", "useful_life_months_default": 60})
    result = []
    for index, name in enumerate(("查找目标", "另一设备"), 1):
        asset = create_registered_asset(actor=context["equipment"], company=context["company"],
            data=physical_data(context, asset_name=name, equipment_number=f"BATCH-LOOKUP-{index}",
                commissioning_date=date(2026, 9, 1)), idempotency_key=f"lookup-asset-{index}")
        asset = confirm_asset_finance(actor=context["finance"], asset=asset,
            finance_data={"accounting_treatment": "fixed_asset", "original_cost": Decimal("12000.00"),
                "fixed_asset_category": category, "capitalization_date": date(2026, 9, 1)},
            profile_data={"depreciation_policy": context["policy"], "method": "manual",
                "start_rule": "specified_date", "specified_start": date(2026, 9, 1),
                "actual_continuation_date": date(2026, 9, 1)},
            idempotency_key=f"lookup-finance-{index}", reason="批次核对测试")
        result.append(asset)
    return result


def generate(context, assets, key, *, complete=False):
    inputs = {str(assets[0].pk): {"amount": "100.00", "reason": "核对第1项"}}
    if complete:
        inputs[str(assets[1].pk)] = {"amount": "200.00", "reason": "核对第2项"}
    return generate_depreciation_batch(actor=context["finance"], company=context["company"],
        period_start=date(2026, 9, 1), period_end=date(2026, 10, 1), idempotency_key=key, manual_inputs=inputs)


def test_asset_lookup_preserves_whole_batch_counts_and_status_context(context, assets, client):
    first = generate(context, assets, "first-lookup")
    second = generate(context, assets, "replacement-lookup", complete=True)
    client.force_login(context["finance"])
    response = client.get(reverse("finance:batch-list"), {"q": assets[0].equipment_number,
        "period": "2026-09", "batch_type": "regular", "status": "draft", "page": "4"})
    assert response.status_code == 200
    batches = list(response.context["batches"])
    assert {batch.pk for batch in batches} == {first.pk, second.pk}
    first_row = next(batch for batch in batches if batch.pk == first.pk)
    assert (first_row.item_count, first_row.ready_count, first_row.error_count) == (2, 1, 1)
    cards = response.context["batch_status_cards"]
    assert [card["count"] for card in cards] == [2, 2, 0, 0, 0]
    for card in cards:
        query = query_of(card["url"])
        assert query["q"] == assets[0].equipment_number and query["period"] == "2026-09"
        assert query["batch_type"] == "regular" and "page" not in query
    status_chip = next(chip for chip in response.context["batch_filter_summary"] if "批次状态" in chip["label"])
    assert "status" not in query_of(status_chip["remove_url"])
    errors = client.get(first_row.errors_url)
    assert errors.context["items"][0].asset_id == assets[1].pk
    assert query_of(errors.context["batch_return_url"])["q"] == assets[0].equipment_number
    exact = client.get(reverse("finance:batch-list"), {"q": str(first.pk)})
    assert [batch.pk for batch in exact.context["batches"]] == [first.pk]


def test_detail_status_shortcuts_keep_search_and_list_navigation(context, assets, client):
    batch = generate(context, assets, "detail-shortcuts")
    client.force_login(context["finance"])
    batch_query = "period=2026-09&status=draft&page=2"
    page = client.get(reverse("finance:batch-detail", args=[batch.pk]), {
        "q": assets[0].equipment_number, "status": "error", "page": "8", "batch_query": batch_query})
    assert page.status_code == 200 and page.context["page_obj"].paginator.count == 0
    assert page.context["batch_totals"]["errors"] == 1
    assert page.context["batch_totals"]["amount"] == Decimal("100.00")
    assert [card["count"] for card in page.context["batch_item_cards"]] == [1, 1, 0, 0]
    for card in page.context["batch_item_cards"]:
        query = query_of(card["url"])
        assert query["q"] == assets[0].equipment_number and query["batch_query"] == batch_query
        assert "page" not in query
    reset = query_of(page.context["batch_clear_items_url"])
    assert reset == {"batch_query": batch_query}
    invalid = client.post(page.context["batch_confirm_url"], {"batch_query": batch_query})
    assert invalid.status_code == 400 and invalid.context["filter_form"]["q"].value() == assets[0].equipment_number
    assert invalid.context["batch_return_url"] == reverse("finance:batch-list") + "?" + batch_query
    assert not DepreciationEntry.objects.exists()


def test_generation_confirmation_and_reversal_keep_navigation_and_entire_batch_scope(context, assets, client):
    client.force_login(context["finance"])
    batch_query = "period=2026-09&status=draft&page=2"
    generated = client.post(reverse("finance:batch-generate"), {
        "period_start": "2026-09-01", "period_end": "2026-09-30", "idempotency_key": "navigation-generation",
        "batch_query": batch_query, f"manual-{assets[0].pk}-amount": "100.00", f"manual-{assets[0].pk}-reason": "第1项",
        f"manual-{assets[1].pk}-amount": "200.00", f"manual-{assets[1].pk}-reason": "第2项"})
    assert generated.status_code == 302 and query_of(generated.url)["batch_query"] == batch_query
    batch = DepreciationBatch.objects.get(company=context["company"])
    detail = client.get(generated.url + "&" + urlencode({"q": assets[0].equipment_number, "status": "ready"}))
    assert len(detail.context["items"]) == 1
    confirmed = client.post(detail.context["batch_confirm_url"], {"confirm": "on", "reason": "核对整批", "batch_query": batch_query})
    assert confirmed.status_code == 302
    assert query_of(confirmed.url)["batch_query"] == batch_query and query_of(confirmed.url)["q"] == assets[0].equipment_number
    assert DepreciationEntry.objects.filter(batch_item__batch=batch).count() == 2
    assert sum(DepreciationEntry.objects.filter(batch_item__batch=batch).values_list("amount", flat=True)) == Decimal("300.00")
    confirmed_detail = client.get(confirmed.url)
    reverse_url = confirmed_detail.context["batch_reverse_url"]
    reverse_page = client.get(reverse_url)
    assert query_of(reverse_page.context["cancel_url"])["batch_query"] == batch_query
    assert 'name="batch_query"' in reverse_page.content.decode()
    reversed_response = client.post(reverse_url, {"confirm": "on", "reason": "冲销核对测试",
        "idempotency_key": reverse_page.context["form"].initial["idempotency_key"]})
    assert reversed_response.status_code == 302 and query_of(reversed_response.url)["batch_query"] == batch_query
    reversed_page = client.get(reversed_response.url)
    assert reversed_page.context["batch_totals"]["effect_amount"] == Decimal("-300.00")
    assert query_of(reversed_page.context["reverses_batch_url"])["batch_query"] == batch_query
    assert sum(batch.items.values_list("planned_amount", flat=True)) == Decimal("300.00")


def test_invalid_filters_and_foreign_batches_preserve_existing_read_scope(context, assets, client):
    batch = generate(context, assets, "read-scope")
    other_company = make_company("OTHER-LOOKUP", active=False)
    foreign = DepreciationBatch.objects.create(company=other_company, period_start=date(2026, 9, 1),
        period_end=date(2026, 10, 1), batch_type="regular", idempotency_key="foreign",
        request_hash="foreign", generated_by=context["finance"], generated_at=timezone.now())
    client.force_login(context["finance"])
    invalid = client.get(reverse("finance:batch-list"), {"batch_type": "invented"})
    assert invalid.status_code == 400 and not invalid.context["batches"]
    assert not invalid.context["batch_status_cards"]
    assert not client.get(reverse("finance:batch-list"), {"q": foreign.pk}).context["batches"]
    assert client.get(reverse("finance:batch-detail", args=[foreign.pk])).status_code == 404
    management = make_user("batch-reader", "management")
    client.force_login(management)
    page = client.get(reverse("finance:batch-list"), {"q": assets[0].equipment_number})
    assert page.status_code == 200 and not page.context["can_manage"]
    detail = client.get(reverse("finance:batch-detail", args=[batch.pk]))
    assert detail.status_code == 200 and "确认批次并过账" not in detail.content.decode()
    assert client.post(reverse("finance:batch-confirm", args=[batch.pk]), {"reason": "不应确认", "confirm": "on"}).status_code == 403
    client.force_login(context["equipment"])
    assert client.get(reverse("finance:batch-list"), {"q": assets[0].equipment_number}).status_code == 403


def test_return_metadata_keeps_only_batch_list_fields():
    request = RequestFactory().get("/finance/batch/", {"batch_query":
        "next=https%3A%2F%2Fexample.com&period=2026-09&status=draft&batch_type=regular&page=2&token=hidden"})
    navigation = batch_list_navigation(request)
    assert navigation["batch_query"] == "period=2026-09&status=draft&batch_type=regular&page=2"
    assert navigation["batch_return_url"].startswith(reverse("finance:batch-list") + "?")
