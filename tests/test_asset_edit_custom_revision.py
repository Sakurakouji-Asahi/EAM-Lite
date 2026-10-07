"""Custom-only edits must invalidate old pages even at one clock instant."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core import signing
from django.db import close_old_connections
from django.urls import reverse
from django.utils import timezone

from apps.assets import services, views
from apps.assets.bulk_support import asset_revision_snapshot
from apps.assets.forms import ASSET_EDIT_REVISION_SALT
from apps.audit.models import AuditLog
from tests.test_asset_edit_revision import assert_revision_rejection, browser_payload, draft, logged_client
from tests.test_asset_registration_finance_separation import context
from tests.test_sprint3_support import make_custom_field


pytestmark = pytest.mark.django_db(transaction=True)


def test_custom_only_change_rejects_old_page_when_persistent_clock_is_identical(context, monkeypatch):
    custom = make_custom_field(context["company"], context["category"], "CLOCK_CUSTOM", "text")
    instant = timezone.now().replace(microsecond=123100)
    # Control only the clock used by the real create/update services. The
    # same instant is stronger than the encoder's same-millisecond collision.
    with monkeypatch.context() as clock:
        clock.setattr(services.timezone, "now", lambda: instant)
        asset = draft(context, custom_values={custom.pk: "原始扩展资料"})
    asset.refresh_from_db()
    assert asset.updated_at == instant
    concrete_before = asset_revision_snapshot(asset)
    url = reverse("assets:asset-edit", args=[asset.pk])
    client_a, client_b = logged_client(context["equipment"]), logged_client(context["equipment"])
    page_a, page_b = client_a.get(url), client_b.get(url)
    assert page_a.status_code == page_b.status_code == 200
    payload_a, payload_b = browser_payload(page_a), browser_payload(page_b)
    revision_before = signing.loads(payload_b["expected_revision"], salt=ASSET_EDIT_REVISION_SALT)["revision"]
    payload_a[f"custom_{custom.pk}-value"] = "A 已保存的新扩展资料"
    with monkeypatch.context() as clock:
        clock.setattr(services.timezone, "now", lambda: instant)
        assert client_a.post(url, payload_a).status_code == 302
    asset.refresh_from_db()
    assert asset.custom_values.get(custom_field=custom).value_text == "A 已保存的新扩展资料"
    assert asset.updated_at == instant
    # Ensure the intended boundary is real: all concrete values are unchanged.
    concrete_after_a = asset_revision_snapshot(asset)
    assert concrete_after_a == concrete_before
    fresh = browser_payload(client_a.get(url))
    revision_after_a = signing.loads(fresh["expected_revision"], salt=ASSET_EDIT_REVISION_SALT)["revision"]
    before_b_audit = AuditLog.objects.count()
    payload_b["brand"] = "B 旧页面的新品牌"
    response = client_b.post(url, payload_b)
    asset.refresh_from_db()
    actual_custom = asset.custom_values.get(custom_field=custom).value_text
    print("CUSTOM_REVISION_CLOCK_BOUNDARY=" + json.dumps({
        "a_persisted_custom": "A 已保存的新扩展资料",
        "concrete_revision_unchanged_after_a": concrete_after_a == concrete_before,
        "a_edit_revision_unchanged": revision_after_a == revision_before,
        "b_http_status": response.status_code, "after_b_custom": actual_custom,
        "after_b_brand": asset.brand,
    }, ensure_ascii=False, sort_keys=True))
    assert actual_custom == "A 已保存的新扩展资料"
    assert asset.brand == "原始品牌"
    assert asset.updated_at == instant and AuditLog.objects.count() == before_b_audit
    assert response.status_code == 200 and response.context["form"].errors["expected_revision"]
    assert response.context["form"]["brand"].value() == payload_b["brand"]
    assert response.context["form"]["expected_revision"].value() == payload_b["expected_revision"]
    assert revision_after_a != revision_before


def test_get_signs_and_displays_the_same_custom_rows_when_another_request_commits(context, monkeypatch):
    custom = make_custom_field(context["company"], context["category"], "GET_CLOCK_CUSTOM", "text")
    instant = timezone.now().replace(microsecond=123100)
    with monkeypatch.context() as clock:
        clock.setattr(services.timezone, "now", lambda: instant)
        asset = draft(context, custom_values={custom.pk: "GET 读取的原始资料"})
    url = reverse("assets:asset-edit", args=[asset.pk])
    client_a, client_b = logged_client(context["equipment"]), logged_client(context["equipment"])
    payload_a = browser_payload(client_a.get(url))
    revision_before = signing.loads(payload_a["expected_revision"], salt=ASSET_EDIT_REVISION_SALT)["revision"]
    payload_a[f"custom_{custom.pk}-value"] = "GET 期间另一请求保存的新资料"
    real_token = views.asset_edit_revision_token
    injected = False

    def actual_a_request():
        close_old_connections()
        try:
            with monkeypatch.context() as clock:
                clock.setattr(services.timezone, "now", lambda: instant)
                return client_a.post(url, payload_a).status_code
        finally:
            close_old_connections()

    def commit_after_read_before_signing(**kwargs):
        nonlocal injected
        injected = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(actual_a_request).result(timeout=20) == 302
        return real_token(**kwargs)

    with monkeypatch.context() as hook:
        hook.setattr(views, "asset_edit_revision_token", commit_after_read_before_signing)
        page_b = client_b.get(url)
    assert injected and page_b.status_code == 200
    payload_b = browser_payload(page_b)
    assert payload_b[f"custom_{custom.pk}-value"] == "GET 读取的原始资料"
    assert signing.loads(payload_b["expected_revision"], salt=ASSET_EDIT_REVISION_SALT)["revision"] == revision_before
    asset.refresh_from_db()
    assert asset.custom_values.get(custom_field=custom).value_text == "GET 期间另一请求保存的新资料"
    assert asset.updated_at == instant
    audit_count = AuditLog.objects.count()
    payload_b["brand"] = "GET 旧页面准备提交的品牌"
    response = client_b.post(url, payload_b)
    assert_revision_rejection(response, url, payload_b)
    asset.refresh_from_db()
    assert asset.custom_values.get(custom_field=custom).value_text == "GET 期间另一请求保存的新资料"
    assert asset.brand == "原始品牌" and asset.updated_at == instant
    assert AuditLog.objects.count() == audit_count
