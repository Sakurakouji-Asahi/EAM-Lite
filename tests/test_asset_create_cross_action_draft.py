"""Conceptual real-GET cross-action cases; unexecuted and not source-frozen.

Future before is the verified draft-idempotency product, not this working tree.
Only a future reviewed candidate may satisfy the proposed refusal messages.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from apps.assets import services
from apps.assets.forms import AssetDraftForm
from apps.assets.models import Asset, AssetCustomValue, AssetIdentity, AssetQrIdentity, AssetRegistration
from apps.assets.registration import create_registered_asset
from apps.audit.models import AuditLog, OperationUndo
from apps.audit.undo import preview_undo, registration_key_was_undone, undo_operation
from apps.finance.models import AssetFinance, DepreciationEntry
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_asset_registration_finance_separation import context
from tests.test_asset_registration_http_reports import browser_data

pytestmark = pytest.mark.django_db(transaction=True)


def open_create_page(ctx, client):
    url = reverse("assets:asset-create")
    page = client.get(url)
    assert page.status_code == 200
    key = str(page.context["form"]["idempotency_key"].value())
    assert key and f'value="{key}"' in page.content.decode()
    return url, key


def saved_facts(ctx):
    company = ctx["company"]
    assets = Asset.objects.filter(company=company).order_by("pk")
    return {
        "asset_count": assets.count(),
        "assets": list(assets.values()),
        "custom_values": list(AssetCustomValue.objects.filter(company=company).order_by("pk").values()),
        "business_audits": list(AuditLog.objects.filter(company=company).order_by("pk").values()),
        "registrations": list(AssetRegistration.objects.filter(company=company).order_by("pk").values()),
        "issued_codes": list(IssuedCode.objects.filter(company=company).order_by("pk").values()),
        "qr_identities": list(AssetQrIdentity.objects.filter(company=company).order_by("pk").values()),
        "identities": list(AssetIdentity.objects.filter(company=company).order_by("pk").values()),
        "sequence_counters": list(SequenceCounter.objects.filter(company=company).order_by("pk").values()),
        "finances": AssetFinance.objects.filter(asset__company=company).count(),
        "depreciation_entries": DepreciationEntry.objects.filter(asset__company=company).count(),
    }


def assert_cross_action_refused_then_new_page_allowed(ctx, *, first_action, other_action, message):
    client = Client()
    client.force_login(ctx["equipment"])
    url, key = open_create_page(ctx, client)
    submitted = browser_data(ctx, idempotency_key=key, asset_action=first_action,
                             asset_name="同一新建页面切换动作的设备")
    first = client.post(url, submitted)
    assert first.status_code == 302
    original = Asset.objects.get(company=ctx["company"])
    original_id = original.pk
    original_row = Asset.objects.filter(pk=original_id).values().get()
    saved = saved_facts(ctx)
    assert saved["asset_count"] == 1
    switched = {**submitted, "asset_action": other_action}
    second = client.post(url, switched)
    after = saved_facts(ctx)
    print("ASSET_CREATE_CROSS_ACTION " + json.dumps({
        "scenario": first_action + "_then_" + other_action,
        "first_http": first.status_code, "second_http": second.status_code,
        "first_redirect": first.url, "second_redirect": getattr(second, "url", None),
        "idempotency_key": key, "company_id": str(ctx["company"].pk),
        "original_asset_id": str(original_id), "first": saved, "after": after,
    }, ensure_ascii=False, sort_keys=True, default=str))
    assert after == saved, "Same create-page key used for another action created a second asset"
    assert second.status_code == 200
    assert message in second.content.decode()
    assert second.context["form"]["idempotency_key"].value() == key
    assert second.context["form"]["asset_name"].value() == switched["asset_name"]
    assert second.context["form"]["department"].value() == switched["department"]
    assert Asset.objects.filter(pk=original_id).values().get() == original_row
    # Existing successful same-action replay retains its own result contract.
    replay = client.post(url, submitted)
    assert replay.status_code == 302 and replay.url == first.url
    assert saved_facts(ctx) == saved
    # Distinct GET keys represent legitimate independent physical objects.
    fresh_url, fresh_key = open_create_page(ctx, client)
    assert fresh_key != key
    allowed = client.post(fresh_url, {**switched, "idempotency_key": fresh_key})
    assert allowed.status_code == 302 and allowed.url != first.url
    final = saved_facts(ctx)
    assert final["asset_count"] == 2
    assert Asset.objects.filter(pk=original_id).values().get() == original_row
    assert Asset.objects.filter(company=ctx["company"], asset_status="draft").count() == 1
    assert Asset.objects.filter(company=ctx["company"], asset_status="pending_label").count() == 1
    assert len(final["registrations"]) == len(final["issued_codes"]) == len(final["qr_identities"]) == 1
    assert final["finances"] == final["depreciation_entries"] == 0


def test_successful_draft_page_key_cannot_create_registered_second_asset(context):
    assert_cross_action_refused_then_new_page_allowed(context, first_action="draft", other_action="register",
        message="此新建页面已用于暂存草稿，请从原资产详情办理建立资产。当前输入已保留。")


def test_successful_register_page_key_cannot_create_draft_second_asset(context):
    assert_cross_action_refused_then_new_page_allowed(context, first_action="register", other_action="draft",
        message="此新建页面已用于建立资产，请从原资产详情查看或补充资料。当前输入已保留。")


def test_deleted_original_draft_key_cannot_create_registered_replacement(context):
    client = Client()
    client.force_login(context["equipment"])
    url, key = open_create_page(context, client)
    submitted = browser_data(context, idempotency_key=key, asset_action="draft")
    assert client.post(url, submitted).status_code == 302
    original = Asset.objects.get(company=context["company"])
    original_id = original.pk
    services.delete_asset_draft(actor=context["equipment"], asset=original,
                               reason="独立受控删除空草稿后核对旧新建页面")
    assert not Asset.objects.filter(pk=original_id).exists()
    marker = AuditLog.objects.get(company=context["company"], action="asset_draft_create",
        object_type="Asset", new_data_json__idempotency_key=key)
    assert marker.object_id == str(original_id)
    saved = saved_facts(context)
    rejected = client.post(url, {**submitted, "asset_action": "register"})
    assert rejected.status_code == 200
    assert "草稿结果记录已不存在" in rejected.content.decode()
    assert rejected.context["form"]["idempotency_key"].value() == key
    assert rejected.context["form"]["asset_name"].value() == submitted["asset_name"]
    assert saved_facts(context) == saved and saved["asset_count"] == 0
    fresh_url, fresh_key = open_create_page(context, client)
    assert fresh_key != key
    allowed = client.post(fresh_url, {**submitted, "asset_action": "register", "idempotency_key": fresh_key})
    assert allowed.status_code == 302
    replacement = Asset.objects.get(company=context["company"])
    assert replacement.pk != original_id and replacement.asset_code
    assert marker.new_data_json["idempotency_key"] == key
    assert AssetRegistration.objects.filter(company=context["company"], idempotency_key=fresh_key).count() == 1
    assert not AssetRegistration.objects.filter(company=context["company"], idempotency_key=key).exists()


def test_undone_registration_key_cannot_create_another_draft(context):
    client = Client()
    client.force_login(context["finance"])
    url, key = open_create_page(context, client)
    submitted = browser_data(context, idempotency_key=key, asset_action="register")
    assert client.post(url, submitted).status_code == 302
    original = Asset.objects.get(company=context["company"])
    original_id = original.pk
    log = AuditLog.objects.get(company=context["company"], action="asset_register", object_id=str(original_id))
    preview = preview_undo(actor=context["finance"], log=log)
    undone = undo_operation(actor=context["finance"], log=log, reason="独立撤销尚未使用的登记后核对旧页面",
                           confirmation=preview["confirmation"])
    assert OperationUndo.objects.filter(pk=undone.pk, original_log=log).exists()
    assert key in undone.plan_json["registration_keys"]
    assert registration_key_was_undone(context["company"], key)
    original.refresh_from_db()
    assert original.asset_status == "draft" and original.asset_code is None and original.current_issued_code_id is None
    assert not AssetRegistration.objects.filter(company=context["company"], idempotency_key=key).exists()
    client.force_login(context["equipment"])
    saved = saved_facts(context)
    rejected = client.post(url, {**submitted, "asset_action": "draft"})
    assert rejected.status_code == 200
    assert "该建档请求已撤销" in rejected.content.decode()
    assert rejected.context["form"]["idempotency_key"].value() == key
    assert rejected.context["form"]["asset_name"].value() == submitted["asset_name"]
    assert saved_facts(context) == saved and saved["asset_count"] == 1
    # Original registration same-action reversal contract must also remain.
    retry_registration = client.post(url, submitted)
    assert retry_registration.status_code == 200
    assert "该建档请求已撤销" in retry_registration.content.decode()
    assert saved_facts(context) == saved
    fresh_url, fresh_key = open_create_page(context, client)
    assert fresh_key != key
    allowed = client.post(fresh_url, {**submitted, "asset_action": "draft", "idempotency_key": fresh_key})
    assert allowed.status_code == 302
    assert Asset.objects.filter(company=context["company"], asset_status="draft").count() == 2
    assert Asset.objects.filter(pk=original_id).values().get() == saved["assets"][0]
    assert not AssetRegistration.objects.filter(company=context["company"]).exists()
    assert not IssuedCode.objects.filter(company=context["company"]).exists()
    assert not AssetQrIdentity.objects.filter(company=context["company"]).exists()


def test_postgresql_same_get_key_draft_register_competition_has_one_winner(context):
    if connection.vendor != "postgresql":
        pytest.skip("Cross-action competition requires independent PostgreSQL row locks")
    client = Client()
    client.force_login(context["equipment"])
    url, key = open_create_page(context, client)
    submitted = browser_data(context, idempotency_key=key, asset_action="register")
    form = AssetDraftForm(submitted, actor=context["equipment"], company=context["company"],
                          registration_requested=True, require_create_key=True)
    assert form.is_valid(), str(form.errors)
    data = {name: value for name, value in form.cleaned_data.items() if name not in {"idempotency_key", "expected_revision"}}
    barrier = Barrier(2)
    refusal_messages = {
        "draft": "此新建页面已用于建立资产，请从原资产详情查看或补充资料。当前输入已保留。",
        "register": "此新建页面已用于暂存草稿，请从原资产详情办理建立资产。当前输入已保留。",
    }

    def worker(action):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
                cursor.execute("SET statement_timeout = '20s'")
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            actor = type(context["equipment"]).objects.get(pk=context["equipment"].pk)
            company = type(context["company"]).objects.get(pk=context["company"].pk)
            # No Company/Asset row lock or surrounding service atomic is held.
            barrier.wait(timeout=10)
            operation = services.create_asset_draft if action == "draft" else create_registered_asset
            try:
                asset = operation(actor=actor, company=company, data=data, custom_values={}, idempotency_key=key)
            except ValidationError as exc:
                # A deadlock/timeout/permission/fixture error is never success.
                assert exc.messages == [refusal_messages[action]]
                return {"action": action, "pid": pid, "outcome": "rejected", "asset_id": None}
            return {"action": action, "pid": pid, "outcome": "saved", "asset_id": str(asset.pk)}
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, action) for action in ("draft", "register")]
        workers = [future.result(timeout=30) for future in futures]
    assert len({worker["pid"] for worker in workers}) == 2
    assert sorted(worker["outcome"] for worker in workers) == ["rejected", "saved"]
    winner = next(worker for worker in workers if worker["outcome"] == "saved")
    final = saved_facts(context)
    print("ASSET_CREATE_CROSS_ACTION_CONCURRENCY " + json.dumps({
        "scenario": "same_get_key_draft_register_competition", "workers": workers,
        "after": final, "idempotency_key": key, "lock_timeout_seconds": 10, "statement_timeout_seconds": 20,
    }, ensure_ascii=False, sort_keys=True, default=str))
    assert final["asset_count"] == 1
    asset = Asset.objects.get(company=context["company"])
    assert str(asset.pk) == winner["asset_id"]
    assert AuditLog.objects.filter(company=context["company"], action="asset_draft_create", object_type="Asset").count() == 1
    if winner["action"] == "draft":
        assert asset.asset_status == "draft" and asset.asset_code is None
        assert len(final["registrations"]) == len(final["issued_codes"]) == len(final["qr_identities"]) == 0
        assert AuditLog.objects.filter(company=context["company"], action="asset_draft_create",
            object_type="Asset", new_data_json__idempotency_key=key).count() == 1
    else:
        assert asset.asset_status == "pending_label" and asset.asset_code
        assert len(final["registrations"]) == len(final["issued_codes"]) == len(final["qr_identities"]) == 1
        assert AssetRegistration.objects.get(asset=asset).idempotency_key == key
        assert not AuditLog.objects.filter(company=context["company"], action="asset_draft_create",
            object_type="Asset", new_data_json__idempotency_key=key).exists()
    assert final["finances"] == final["depreciation_entries"] == 0
