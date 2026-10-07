from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse

from apps.supplies.models import SupplyDocument, SupplyStockLedger
from apps.supplies.services import (
    cancel_supply_document, create_supply_document, post_supply_document, reverse_supply_document,
)
from tests.test_asset_list_return_navigation import Links
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_supply_item, make_user
from tests.test_supply_draft_edit_revision import payload


pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(client):
    company, actor, _, _, warehouse, _, item, _ = supply_context()
    item.name = '查询设备 & + / ? = % # "规格"'
    item.save(update_fields=["name"])
    extra_item = make_supply_item(company, item.category, "WORKSPACE-EXTRA", name=item.name + " 配件")
    client.force_login(actor)
    return {"company": company, "actor": actor, "warehouse": warehouse, "item": item, "extra_item": extra_item}


def receipt(context, key, *, actor=None, two_lines=False):
    items = [context["item"], context["extra_item"]] if two_lines else [context["item"]]
    return create_supply_document(actor=actor or context["actor"], company=context["company"], document_type="receipt",
        data={"business_date": date(2026, 8, 26), "target_warehouse": context["warehouse"], "idempotency_key": key},
        lines=[{"item": item, "quantity": Decimal("5"), "entered_unit_cost": Decimal("2")} for item in items])


def test_my_document_counts_ignore_selected_status_and_count_duplicate_item_matches_once(client, workspace):
    context = workspace
    own = [receipt(context, f"workspace-{state}", two_lines=True) for state in ("draft", "posted", "cancelled", "reversed")]
    post_supply_document(actor=context["actor"], document=own[1])
    cancel_supply_document(actor=context["actor"], document=own[2], reason="尚未办理")
    post_supply_document(actor=context["actor"], document=own[3])
    reverse_supply_document(actor=context["actor"], document=own[3], idempotency_key="workspace-reverse", reason="录入错误")
    receipt(context, "workspace-other", actor=make_user("document-workspace-other", "warehouse"), two_lines=True)
    filters = {"q": context["item"].name, "document_type": "receipt", "status": "draft", "mine": "on", "page": 2}
    page = client.get(reverse("supplies:document-list"), filters)
    assert page.status_code == 200 and page.context["mine"]
    assert page.context["document_counts"] == {"total": 4, "draft": 1, "posted": 1, "cancelled": 1, "reversed": 1}
    assert [row.pk for row in page.context["page_obj"]] == [own[0].pk]
    for row in page.context["document_status_rows"]:
        query = parse_qs(urlsplit(row["url"]).query)
        assert query["q"] == [context["item"].name] and query["mine"] == ["on"]
        assert query["status"] == [row["value"]] and "page" not in query
    all_creators = client.get(reverse("supplies:document-list"), {"q": context["item"].name, "document_type": "receipt"})
    assert all_creators.context["document_counts"]["total"] == 5


def test_filter_removal_only_clears_one_condition_and_return_intent_survives_empty_recovery(client, workspace):
    context = workspace
    receipt(context, "workspace-chips")
    filters = {"q": context["item"].name, "document_type": "receipt", "status": "draft", "warehouse": str(context["warehouse"].pk),
        "item": context["item"].item_code, "date_from": "2026-08-26", "date_to": "2026-08-26", "mine": "on", "page": 2}
    page = client.get(reverse("supplies:document-list"), filters)
    assert len(page.context["active_filters"]) == 8
    for chip in page.context["active_filters"]:
        query = parse_qs(urlsplit(chip["url"]).query)
        assert chip["name"] not in query and "page" not in query
        for key, value in filters.items():
            if key not in {chip["name"], "page"}:
                assert query[key] == [str(value)]
    empty = client.get(reverse("supplies:document-list"), {"intent": "consumable_return", "q": "未匹配内容", "mine": "on"})
    assert empty.status_code == 200 and empty.context["return_intent"]
    assert empty.context["document_clear_url"] == reverse("supplies:document-list") + "?intent=consumable_return"
    assert {chip["name"] for chip in empty.context["active_filters"]} == {"q", "mine"}
    assert all(parse_qs(urlsplit(chip["url"]).query)["intent"] == ["consumable_return"] for chip in empty.context["active_filters"])


def test_list_detail_edit_errors_save_and_preview_post_keep_original_query_and_tokens(client, workspace):
    context = workspace
    document = receipt(context, "workspace-edit-post")
    list_url = reverse("supplies:document-list") + "?" + urlencode({
        "q": context["item"].name, "document_type": "receipt", "status": "draft", "mine": "on", "page": 2,
    })
    listing = client.get(list_url)
    detail_path = reverse("supplies:document-detail", args=[document.pk])
    edit_path = reverse("supplies:document-edit", args=[document.pk])
    post_path = reverse("supplies:document-post", args=[document.pk])
    for path in (edit_path, post_path, reverse("supplies:document-cancel", args=[document.pk])):
        assert parse_qs(urlsplit(Links(listing).hrefs_for_path(path)[0]).query)["return_to"] == [list_url]
    detail = client.get(Links(listing).hrefs_for_path(detail_path)[0])
    edit = client.get(Links(detail).hrefs_for_path(edit_path)[0])
    assert edit.context["document_return_to"] == list_url
    assert parse_qs(urlsplit(edit.context["document_form_back_url"]).query)["return_to"] == [list_url]
    data = {**payload(edit), "return_to": list_url, "next_action": "detail"}
    old_revision = data["expected_revision"]
    invalid = client.post(edit_path, {**data, "lines-0-quantity": "0"})
    assert invalid.status_code == 200 and invalid.context["formset"].errors
    assert invalid.context["document_return_to"] == list_url
    assert invalid.context["form"]["expected_revision"].value() == old_revision
    assert invalid.context["formset"].forms[0]["quantity"].value() == "0"
    saved = client.post(edit_path, {**data, "lines-0-quantity": "7"})
    assert saved.status_code == 302
    assert urlsplit(saved.url).path == detail_path and parse_qs(urlsplit(saved.url).query)["return_to"] == [list_url]
    saved_detail = client.get(saved.url)
    edit_again = client.get(Links(saved_detail).hrefs_for_path(edit_path)[0])
    proceed = client.post(edit_path, {**payload(edit_again), "return_to": list_url, "next_action": "post"})
    assert urlsplit(proceed.url).path == post_path
    assert parse_qs(urlsplit(proceed.url).query)["return_to"] == [list_url]
    confirmation = client.get(proceed.url)
    assert confirmation.context["posting_preview"]["total_amount"] == Decimal("14.00")
    assert confirmation.context["document_return_to"] == list_url
    posting = {"confirm": "on", "preview_token": confirmation.context["form"]["preview_token"].value(),
        "idempotency_key": confirmation.context["form"]["idempotency_key"].value(), "return_to": list_url}
    assert client.post(post_path, posting).url == list_url
    before = SupplyStockLedger.objects.count()
    assert client.post(post_path, posting).url == list_url
    assert SupplyStockLedger.objects.count() == before
    document.refresh_from_db()
    assert document.status == "posted" and document.lines.get().posted_amount == Decimal("14.00")


def test_new_draft_and_cancel_keep_posted_return_context_and_no_stock_changes(client, workspace):
    context = workspace
    list_url = reverse("supplies:document-list") + "?q=receipt&status=draft&page=2"
    create_path = reverse("supplies:document-create", args=["receipt"])
    page = client.get(create_path, {"return_to": list_url})
    assert page.context["document_form_back_url"] == list_url
    data = {**payload(page), "return_to": list_url, "business_date": "2026-08-26",
        "target_warehouse": str(context["warehouse"].pk), "lines-0-item": str(context["item"].pk),
        "lines-0-quantity": "2", "lines-0-entered_unit_cost": "3", "next_action": "detail"}
    created = client.post(create_path, data)
    assert created.status_code == 302
    assert parse_qs(urlsplit(created.url).query)["return_to"] == [list_url]
    document = SupplyDocument.objects.get(idempotency_key=data["idempotency_key"])
    cancel_path = reverse("supplies:document-cancel", args=[document.pk])
    cancel = client.get(Links(client.get(created.url)).hrefs_for_path(cancel_path)[0])
    assert cancel.context["document_return_to"] == list_url
    error = client.post(cancel_path, {"return_to": list_url, "reason": ""})
    assert error.status_code == 200 and error.context["form"].errors
    assert error.context["document_return_to"] == list_url
    assert 'data-unsaved-guard="true"' in error.content.decode()
    saved = client.post(cancel_path, {"return_to": list_url, "reason": "暂不入库"})
    assert parse_qs(urlsplit(saved.url).query)["return_to"] == [list_url]
    document.refresh_from_db()
    assert document.status == "cancelled" and not SupplyStockLedger.objects.exists()


def test_stale_draft_page_retains_original_version_and_query_on_reopen(client, workspace):
    document = receipt(workspace, "workspace-stale-return")
    edit_path = reverse("supplies:document-edit", args=[document.pk])
    list_url = reverse("supplies:document-list") + "?q=workspace&status=draft&mine=on&page=2"
    old = client.get(edit_path, {"return_to": list_url})
    other_window = client.get(edit_path, {"return_to": list_url})
    other_data = {**payload(other_window), "return_to": list_url, "remark": "另一窗口已保存",
                  "lines-0-quantity": "9"}
    assert client.post(edit_path, other_data).status_code == 302
    old_data = {**payload(old), "return_to": list_url, "remark": "旧窗口现场说明", "next_action": "post"}
    rejected = client.post(edit_path, old_data)
    assert rejected.status_code == 200
    assert rejected.context["form"].errors["expected_revision"]
    assert rejected.context["form"]["expected_revision"].value() == old_data["expected_revision"]
    assert rejected.context["form"]["remark"].value() == "旧窗口现场说明"
    assert rejected.context["document_return_to"] == list_url
    reopen = next(href for href, label in Links(rejected).links if label.strip() == "重新打开最新草稿")
    assert urlsplit(reopen).path == edit_path
    assert parse_qs(urlsplit(reopen).query)["return_to"] == [list_url]
    document.refresh_from_db()
    assert document.status == "draft" and document.remark == "另一窗口已保存"
    assert document.lines.get().quantity == Decimal("9.0000")
    assert not SupplyStockLedger.objects.exists()


@pytest.mark.parametrize("target", ["https://example.invalid/", "/accounts/logout/", "/supplies/documents/create/receipt/"])
def test_draft_navigation_rejects_external_or_action_return_targets(client, workspace, target):
    document = receipt(workspace, "workspace-safe-return")
    page = client.get(reverse("supplies:document-edit", args=[document.pk]), {"return_to": target})
    assert page.status_code == 200 and not page.context["document_return_to"]
    assert page.context["document_form_back_url"] == reverse("supplies:document-detail", args=[document.pk])
    assert 'name="return_to"' not in page.content.decode()
