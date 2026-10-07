"""Keep a safe list origin through real draft editing and readable errors."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse
from django.utils.html import escape

from apps.assets.services import update_asset_draft
from apps.audit.models import AuditLog
from tests.test_asset_edit_revision import browser_payload
from tests.test_asset_list_return_navigation import Links, draft, ledger_context


pytestmark = pytest.mark.django_db


class Inputs(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.values = {}
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            data = dict(attrs)
            if "name" in data:
                self.values[data["name"]] = data.get("value", "")


def labeled_link(response, label):
    return next(href for href, text in Links(response).links if text.strip() == label)


def with_origin(path, origin):
    return path + "?" + urlencode({"return_to": origin})


def original_list(context):
    return reverse("assets:asset-list") + "?" + urlencode({
        "q": '编辑导航 & + / ? = % # "型号"',
        "category": str(context["category"].pk), "department": str(context["department"].pk),
        "employee": str(context["employee"].pk), "location": str(context["location"].pk),
        "asset_status": "draft", "record_status": "active", "view": "individual_durable",
        "page": "1", "page_size": "50",
    })


def test_list_detail_edit_cancel_returns_to_original_filtered_list(client, ledger_context):
    context = ledger_context
    asset = draft(context, asset_name='编辑导航 & + / ? = % # "型号"')
    draft(context, asset_name="其他分类设备")
    client.force_login(context["actor"])
    listing = client.get(original_list(context))
    assert listing.status_code == 200 and not listing.context["filter_errors"]
    assert [value.pk for value in listing.context["page"]] == [asset.pk]
    origin = listing.wsgi_request.get_full_path()
    detail_path = reverse("assets:asset-detail", args=[asset.pk])
    detail = client.get(Links(listing).hrefs_for_path(detail_path)[0])
    assert detail.status_code == 200 and labeled_link(detail, "返回总账") == origin
    edit_url = labeled_link(detail, "编辑草稿")
    assert parse_qs(urlsplit(edit_url).query) == {"return_to": [origin]}
    editing = client.get(edit_url)
    assert editing.status_code == 200 and Inputs(editing).values["return_to"] == origin
    cancel_url = labeled_link(editing, "取消")
    assert cancel_url == with_origin(detail_path, origin)
    assert labeled_link(editing, "返回") == cancel_url
    cancelled = client.get(cancel_url)
    returned = client.get(labeled_link(cancelled, "返回总账"))
    assert returned.status_code == 200
    assert returned.wsgi_request.GET == listing.wsgi_request.GET
    assert [value.pk for value in returned.context["page"]] == [asset.pk]


def test_successful_edit_post_persists_data_and_preserves_original_list_origin(client, ledger_context):
    context = ledger_context
    asset = draft(context, asset_name='编辑导航 & + / ? = % # "型号"')
    client.force_login(context["actor"])
    origin = original_list(context)
    url = reverse("assets:asset-edit", args=[asset.pk])
    page = client.get(with_origin(url, origin))
    assert page.status_code == 200
    payload = browser_payload(page)
    payload.update(notes="编辑后真实保存的备注", brand="编辑后真实保存的品牌", return_to=origin)
    response = client.post(url, payload)
    assert response.status_code == 302
    asset.refresh_from_db()
    assert asset.notes == payload["notes"] and asset.brand == payload["brand"]
    assert urlsplit(response.url).path == reverse("assets:asset-detail", args=[asset.pk])
    assert parse_qs(urlsplit(response.url).query) == {"return_to": [origin]}
    detail = client.get(response.url)
    assert detail.status_code == 200 and labeled_link(detail, "返回总账") == origin
    returned = client.get(labeled_link(detail, "返回总账"))
    assert returned.status_code == 200 and not returned.context["filter_errors"]
    assert returned.wsgi_request.get_full_path() == origin
    assert [value.pk for value in returned.context["page"]] == [asset.pk]


def test_invalid_business_field_keeps_origin_inputs_and_original_revision_without_writes(client, ledger_context):
    context = ledger_context
    asset = draft(context)
    client.force_login(context["actor"])
    origin = original_list(context)
    url = reverse("assets:asset-edit", args=[asset.pk])
    page = client.get(with_origin(url, origin))
    payload = browser_payload(page)
    payload.update(asset_name="", brand="尚未保存的输入", return_to=origin)
    before = (asset.asset_name, asset.brand, asset.updated_at, AuditLog.objects.count())
    response = client.post(url, payload)
    assert response.status_code == 200 and response.context["form"].errors["asset_name"]
    assert response.context["form"]["brand"].value() == payload["brand"]
    assert response.context["form"]["expected_revision"].value() == payload["expected_revision"]
    asset.refresh_from_db()
    assert (asset.asset_name, asset.brand, asset.updated_at, AuditLog.objects.count()) == before
    assert 'href="#id_asset_name"' in response.content.decode()
    assert 'data-unsaved-guard="true"' in response.content.decode()
    assert Inputs(response).values["return_to"] == origin
    assert labeled_link(response, "取消") == with_origin(reverse("assets:asset-detail", args=[asset.pk]), origin)


@pytest.mark.parametrize("revision_error", ["missing", "stale"])
def test_hidden_revision_error_is_plain_chinese_and_reopen_keeps_origin(client, ledger_context, revision_error):
    context = ledger_context
    asset = draft(context)
    client.force_login(context["actor"])
    origin = original_list(context)
    url = reverse("assets:asset-edit", args=[asset.pk])
    payload = browser_payload(client.get(with_origin(url, origin)))
    payload.update(brand="拒绝时仍保留的品牌输入", return_to=origin)
    if revision_error == "missing":
        payload.pop("expected_revision")
    else:
        update_asset_draft(actor=context["actor"], asset=asset, data={"notes": "同事已保存的新备注"})
    asset.refresh_from_db()
    before = (asset.notes, asset.brand, asset.updated_at, AuditLog.objects.count())
    response = client.post(url, payload)
    form = response.context["form"]
    assert response.status_code == 200 and form.errors["expected_revision"]
    assert form["brand"].value() == payload["brand"]
    assert (form["expected_revision"].value() or "") == payload.get("expected_revision", "")
    asset.refresh_from_db()
    assert (asset.notes, asset.brand, asset.updated_at, AuditLog.objects.count()) == before
    html = response.content.decode()
    assert 'href="#id_expected_revision"' not in html
    for error in form.errors["expected_revision"]:
        assert f"<li>{escape(error)}</li>" in html
    assert 'data-unsaved-guard="true"' in html
    assert Inputs(response).values["return_to"] == origin
    assert labeled_link(response, "重新打开最新编辑页面") == with_origin(url, origin)
    fresh = client.get(labeled_link(response, "重新打开最新编辑页面"))
    assert fresh.status_code == 200 and not fresh.context["form"].is_bound
    assert fresh.context["form"]["brand"].value() == before[1]


def test_unsafe_get_and_post_origins_fall_back_without_skipping_real_edit_validation(client, ledger_context):
    context = ledger_context
    asset = draft(context)
    client.force_login(context["actor"])
    edit_path = reverse("assets:asset-edit", args=[asset.pk])
    detail_path = reverse("assets:asset-detail", args=[asset.pk])
    for index, unsafe in enumerate(("https://example.com/assets/", "//example.com/assets/", reverse("logout"))):
        detail = client.get(detail_path, {"return_to": unsafe})
        assert detail.status_code == 200
        assert labeled_link(detail, "返回总账") == reverse("assets:asset-list")
        assert labeled_link(detail, "编辑草稿") == edit_path
        page = client.get(edit_path, {"return_to": unsafe})
        assert page.status_code == 200 and "return_to" not in Inputs(page).values
        assert labeled_link(page, "取消") == detail_path
        payload = browser_payload(page)
        payload.update(brand=f"安全回退仍正常保存{index}", return_to=unsafe)
        response = client.post(with_origin(edit_path, original_list(context)), payload)
        assert response.status_code == 302 and response.url == detail_path
        asset.refresh_from_db()
        assert asset.brand == payload["brand"]
