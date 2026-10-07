"""Real GET draft creation retries, request identity, permissions and locks.

Only the first case is selected on the unmodified before carrier.
All outcomes in this prepared module remain unobserved until independent execution.
"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from apps.assets import services
from apps.assets.forms import AssetDraftForm
from apps.assets.models import Asset, AssetCustomValue, AssetRegistration
from apps.assets.registration import register_asset
from apps.audit.models import AuditLog
from apps.audit.undo import preview_undo, undo_operation
from apps.masterdata.models import IssuedCode
from apps.masterdata.services import revoke_department_scope
from tests.test_asset_registration_finance_separation import context, physical_data
from tests.test_asset_registration_http_reports import browser_data
from tests.test_unified_asset_identity import context as standard_context
from tests.test_sprint3_support import (
    grant_scope, make_category, make_company, make_custom_field, make_department, make_employee, make_user,
)

pytestmark = pytest.mark.django_db(transaction=True)


def get_draft_page(ctx, *, actor=None, extra=None):
    client = Client()
    client.force_login(actor or ctx["equipment"])
    url = reverse("assets:asset-create")
    page = client.get(url)
    assert page.status_code == 200
    key = str(page.context["form"]["idempotency_key"].value())
    assert key and f'value="{key}"' in page.content.decode()
    submitted = browser_data(ctx, asset_action="draft", idempotency_key=key,
                             asset_name="同一页面暂存的资产草稿", **(extra or {}))
    return client, url, page, submitted


def facts(ctx):
    assets = Asset.objects.filter(company=ctx["company"]).order_by("pk")
    return {
        "assets": assets.count(),
        "asset_ids": [str(pk) for pk in assets.values_list("pk", flat=True)],
        "draft_create_audits": AuditLog.objects.filter(
            company=ctx["company"], action="asset_draft_create").count(),
        "keyed_create_audits": AuditLog.objects.filter(
            company=ctx["company"], action="asset_draft_create",
            new_data_json__has_key="idempotency_key").count(),
        "custom_values": AssetCustomValue.objects.filter(company=ctx["company"]).count(),
        "registrations": AssetRegistration.objects.filter(company=ctx["company"]).count(),
        "issued_codes": IssuedCode.objects.filter(company=ctx["company"]).count(),
    }


def service_values(ctx, submitted, *, actor=None):
    actor = actor or ctx["equipment"]
    form = AssetDraftForm(submitted, actor=actor, company=ctx["company"])
    assert form.is_valid(), str(form.errors)
    data = {key: value for key, value in form.cleaned_data.items()
            if key not in {"idempotency_key", "expected_revision"}}
    return {"actor": actor, "company": ctx["company"], "data": data,
            "custom_values": {}, "idempotency_key": submitted["idempotency_key"]}


def bounded_backend():
    # Per-connection limits do not alter the database or other worker sessions.
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '10s'")
        cursor.execute("SET statement_timeout = '20s'")
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def test_same_get_draft_save_replay_returns_one_asset_and_one_create_audit(context):
    client, url, page, submitted = get_draft_page(context)
    first = client.post(url, submitted)
    assert first.status_code == 302
    saved = facts(context)
    assert saved["assets"] == saved["draft_create_audits"] == 1
    replay = client.post(url, submitted)
    actual = facts(context)
    print("ASSET_DRAFT_CREATE_IDEMPOTENCY " + json.dumps({
        "scenario": "same_get_same_key_same_draft_multipart", "first_http": first.status_code,
        "replay_http": replay.status_code, "first_redirect": first.url,
        "replay_redirect": getattr(replay, "url", None), "first": saved, "after": actual,
        "idempotency_key": submitted["idempotency_key"],
        "actor_id": str(context["equipment"].pk), "company_id": str(context["company"].pk),
        "request_sha256": hashlib.sha256(json.dumps(submitted, sort_keys=True).encode()).hexdigest(),
    }, sort_keys=True))
    assert actual["assets"] == actual["draft_create_audits"] == 1, "Successful same-page draft retry created a second asset and creation audit"
    assert replay.status_code == 302 and replay.url == first.url
    assert actual == saved
    assert actual["registrations"] == actual["issued_codes"] == 0
    asset = Asset.objects.get(company=context["company"])
    assert asset.asset_status == "draft" and asset.asset_code is None
    audit = AuditLog.objects.get(company=context["company"], action="asset_draft_create")
    assert audit.object_id == str(asset.pk)
    assert audit.new_data_json["idempotency_key"] == submitted["idempotency_key"]
    assert len(audit.new_data_json["request_hash"]) == 64


def test_same_draft_key_rejects_changed_physical_and_custom_payload(context):
    field = make_custom_field(context["company"], context["category"], "RETRY_TEXT", "text")
    custom_name = f"custom_{field.pk}-value"
    client, url, _, submitted = get_draft_page(context, extra={custom_name: "原始扩展资料"})
    first = client.post(url, submitted)
    assert first.status_code == 302
    saved = facts(context)
    for changes in ({"asset_name": "同一key的不同资产"}, {custom_name: "修改后的扩展资料"}):
        response = client.post(url, {**submitted, **changes})
        assert response.status_code == 200
        assert "不同资料" in response.content.decode()
        assert response.context["form"]["idempotency_key"].value() == submitted["idempotency_key"]
        assert facts(context) == saved
    assert AssetCustomValue.objects.get(custom_field=field).value_text == "原始扩展资料"
    values = service_values(context, submitted)
    values["custom_values"] = {str(field.pk): "原始扩展资料"}
    same = services.create_asset_draft(**values)
    assert str(same.pk) == saved["asset_ids"][0]
    with pytest.raises(ValidationError, match="不同资料"):
        services.create_asset_draft(**values, initialization_source="excel_import")
    assert facts(context) == saved


def test_new_get_key_can_create_distinct_draft_with_identical_contents(context):
    client, url, _, first_data = get_draft_page(context)
    first = client.post(url, first_data)
    assert first.status_code == 302
    _, _, _, second_data = get_draft_page(context)
    assert second_data["idempotency_key"] != first_data["idempotency_key"]
    second = client.post(url, second_data)
    assert second.status_code == 302 and second.url != first.url
    actual = facts(context)
    assert actual["assets"] == actual["draft_create_audits"] == actual["keyed_create_audits"] == 2
    assert actual["registrations"] == actual["issued_codes"] == 0


def test_original_create_retry_preserves_later_edit_and_registration(context):
    client, url, _, submitted = get_draft_page(context)
    first = client.post(url, submitted)
    assert first.status_code == 302
    asset = Asset.objects.get(company=context["company"])
    services.update_asset_draft(actor=context["equipment"], asset=asset,
                                data={"notes": "另一办理人已补全的资料"})
    asset.refresh_from_db()
    saved_time = asset.updated_at
    audit_count = AuditLog.objects.count()
    replay = client.post(url, submitted)
    assert replay.status_code == 302 and replay.url == first.url
    asset.refresh_from_db()
    assert asset.notes == "另一办理人已补全的资料" and asset.updated_at == saved_time
    assert AuditLog.objects.count() == audit_count
    assert facts(context)["assets"] == 1
    registered = register_asset(actor=context["finance"], asset=asset,
                                idempotency_key="draft-replay-later-registration")
    assert registered.pk == asset.pk and registered.asset_code
    saved_code, saved_status = registered.asset_code, registered.asset_status
    saved = facts(context)
    audit_count = AuditLog.objects.count()
    retry_after_registration = client.post(url, submitted)
    assert retry_after_registration.status_code == 302 and retry_after_registration.url == first.url
    asset.refresh_from_db()
    assert asset.asset_code == saved_code and asset.asset_status == saved_status
    assert asset.notes == "另一办理人已补全的资料"
    assert AuditLog.objects.count() == audit_count and facts(context) == saved
    log = AuditLog.objects.get(company=context["company"], action="asset_register",
                               object_id=str(asset.pk))
    preview = preview_undo(actor=context["finance"], log=log)
    undo_operation(actor=context["finance"], log=log, reason="独立办理人撤销尚未使用的建档",
                   confirmation=preview["confirmation"])
    asset.refresh_from_db()
    assert asset.asset_status == "draft" and asset.asset_code is None
    assert not asset.qr_identities.exists()
    saved_after_undo = facts(context)
    audit_count = AuditLog.objects.count()
    retry_after_undo = client.post(url, submitted)
    assert retry_after_undo.status_code == 302 and retry_after_undo.url == first.url
    asset.refresh_from_db()
    assert asset.asset_status == "draft" and asset.asset_code is None
    assert not asset.qr_identities.exists()
    assert asset.notes == "另一办理人已补全的资料"
    assert facts(context) == saved_after_undo and AuditLog.objects.count() == audit_count


def test_first_draft_audit_failure_rolls_back_then_same_page_can_retry(context, monkeypatch):
    field = make_custom_field(context["company"], context["category"], "ROLLBACK_TEXT", "text")
    custom_name = f"custom_{field.pk}-value"
    client, url, _, submitted = get_draft_page(context, extra={custom_name: "事务内的扩展资料"})
    baseline = facts(context)
    real_audit = services._audit

    def fail_create_audit(**kwargs):
        if kwargs["action"] == "asset_draft_create":
            raise ValidationError("独立注入的创建审计失败")
        return real_audit(**kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(services, "_audit", fail_create_audit)
        rejected = client.post(url, submitted)
    assert rejected.status_code == 200
    assert "独立注入的创建审计失败" in rejected.content.decode()
    assert facts(context) == baseline
    assert rejected.context["form"]["idempotency_key"].value() == submitted["idempotency_key"]
    assert rejected.context["form"]["asset_name"].value() == submitted["asset_name"]
    success = client.post(url, submitted)
    assert success.status_code == 302
    actual = facts(context)
    assert actual["assets"] == actual["draft_create_audits"] == actual["keyed_create_audits"] == actual["custom_values"] == 1


def test_missing_draft_page_key_is_visible_and_cannot_create(context):
    client, url, _, submitted = get_draft_page(context)
    baseline = facts(context)
    for key_value in (None, ""):
        invalid = dict(submitted)
        invalid.pop("idempotency_key")
        if key_value is not None:
            invalid["idempotency_key"] = key_value
        response = client.post(url, invalid)
        assert response.status_code == 200
        assert response.context["form"].errors["idempotency_key"]
        assert "页面校验信息" in response.content.decode()
        assert response.context["form"]["asset_name"].value() == submitted["asset_name"]
        assert facts(context) == baseline


def test_replay_checks_fresh_department_permission_after_form_validation(context):
    manager = make_user("draft-idempotency-manager", "department_manager")
    scope = grant_scope(manager, context["company"], context["department"])
    client, url, _, submitted = get_draft_page(context, actor=manager)
    first = client.post(url, submitted)
    assert first.status_code == 302
    values = service_values(context, submitted, actor=manager)
    asset = Asset.objects.get(company=context["company"])
    outside = make_department(context["company"], "DRAFTRETRYOUTSIDE")
    owner = make_employee(context["company"], outside, "DRAFTRETRY-OUTSIDE")
    services.update_asset_draft(actor=context["equipment"], asset=asset,
                                data={"department": outside, "responsible_employee": owner})
    saved_outside = facts(context)
    audit_count = AuditLog.objects.count()
    # The original request department still belongs to this manager; the target no longer does.
    with pytest.raises(PermissionDenied, match="查看"):
        services.create_asset_draft(**values)
    assert facts(context) == saved_outside and AuditLog.objects.count() == audit_count
    admin = make_user("draft-idempotency-admin", "system_admin")
    retained = make_department(context["company"], "DRAFTRETRYRETAINED")
    grant_scope(manager, context["company"], retained, descendants=False)
    revoke_department_scope(actor=admin, scope=scope, reason="独立办理人撤销创建数据范围")
    saved = facts(context)
    with pytest.raises(PermissionDenied, match="范围|权限"):
        services.create_asset_draft(**values)
    assert facts(context) == saved
    assert client.post(url, submitted).status_code in {200, 403}
    assert facts(context) == saved


def test_keyed_draft_call_rejects_noncurrent_company_without_returning_local_result(context):
    client, url, _, submitted = get_draft_page(context)
    assert client.post(url, submitted).status_code == 302
    values = service_values(context, submitted)
    foreign = make_company("DRAFTRETRYFOREIGN", active=False)
    saved = facts(context)
    with pytest.raises(PermissionDenied, match="当前公司"):
        services.create_asset_draft(**{**values, "company": foreign})
    assert facts(context) == saved
    assert not Asset.objects.filter(company=foreign).exists()
    assert not AuditLog.objects.filter(company=foreign, action="asset_draft_create").exists()


def test_trusted_calls_without_key_keep_independent_creation_and_old_audit_shape(context):
    form = AssetDraftForm(browser_data(context, asset_action="draft", idempotency_key=""),
                          actor=context["equipment"], company=context["company"])
    assert not form.fields["idempotency_key"].required
    assert form.is_valid(), str(form.errors)
    values = {"actor": context["equipment"], "company": context["company"],
              "data": physical_data(context)}
    first = services.create_asset_draft(**values)
    second = services.create_asset_draft(**values)
    assert first.pk != second.pk
    actual = facts(context)
    assert actual["assets"] == actual["draft_create_audits"] == 2
    assert actual["keyed_create_audits"] == 0
    for log in AuditLog.objects.filter(company=context["company"], action="asset_draft_create"):
        assert "idempotency_key" not in log.new_data_json and "request_hash" not in log.new_data_json


def test_old_create_key_cannot_resurrect_a_deleted_draft(context):
    client, url, _, submitted = get_draft_page(context)
    assert client.post(url, submitted).status_code == 302
    asset = Asset.objects.get(company=context["company"])
    services.delete_asset_draft(actor=context["equipment"], asset=asset, reason="独立办理人删除无引用草稿")
    saved = facts(context)
    response = client.post(url, submitted)
    assert response.status_code == 200
    assert "结果记录已不存在" in response.content.decode()
    assert response.context["form"]["idempotency_key"].value() == submitted["idempotency_key"]
    assert facts(context) == saved and saved["assets"] == 0


@pytest.mark.parametrize("same_key", [True, False], ids=["same-page-key", "distinct-page-keys"])
def test_company_lock_serializes_keyed_draft_creations(context, same_key):
    if connection.vendor != "postgresql":
        pytest.skip("Keyed draft creation concurrency requires PostgreSQL row locks")
    _, _, _, submitted = get_draft_page(context)
    _, _, _, other_page = get_draft_page(context)
    values = service_values(context, submitted)
    barrier = Barrier(2)

    def create(index):
        close_old_connections()
        try:
            pid = bounded_backend()
            actor = type(context["equipment"]).objects.get(pk=context["equipment"].pk)
            company = type(context["company"]).objects.get(pk=context["company"].pk)
            key = submitted["idempotency_key"] if same_key or index == 0 else other_page["idempotency_key"]
            barrier.wait(timeout=10)
            result = services.create_asset_draft(**{**values, "actor": actor, "company": company,
                                                    "idempotency_key": key})
            return str(result.pk), pid
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(create, index) for index in range(2)]
        results = [future.result(timeout=30) for future in pending]
    expected = 1 if same_key else 2
    assert len({pid for _, pid in results}) == 2
    assert len({pk for pk, _ in results}) == expected
    actual = facts(context)
    print("ASSET_DRAFT_CREATE_CONCURRENCY " + json.dumps({
        "scenario": "same-key" if same_key else "distinct-keys", "workers": results,
        "after": actual, "lock_timeout_seconds": 10, "statement_timeout_seconds": 20,
    }, sort_keys=True))
    assert actual["assets"] == actual["draft_create_audits"] == actual["keyed_create_audits"] == expected
    assert actual["registrations"] == actual["issued_codes"] == 0


def test_keyed_create_replay_and_controlled_edit_can_complete_concurrently(context):
    if connection.vendor != "postgresql":
        pytest.skip("Mixed keyed replay/edit locking requires PostgreSQL row locks")
    client, url, _, submitted = get_draft_page(context)
    assert client.post(url, submitted).status_code == 302
    asset = Asset.objects.get(company=context["company"])
    values = service_values(context, submitted)
    barrier = Barrier(2)

    def operation(kind):
        close_old_connections()
        try:
            pid = bounded_backend()
            actor = type(context["equipment"]).objects.get(pk=context["equipment"].pk)
            company = type(context["company"]).objects.get(pk=context["company"].pk)
            current = Asset.objects.get(pk=asset.pk)
            # Only unlocked reads precede the barrier; no held Company/Asset lock.
            barrier.wait(timeout=10)
            if kind == "replay":
                result = services.create_asset_draft(**{**values, "actor": actor, "company": company})
            else:
                result = services.update_asset_draft(actor=actor, asset=current,
                    data={"notes": "并发独立修改后的资料"})
            return str(result.pk), pid
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(operation, kind) for kind in ("replay", "edit")]
        results = [future.result(timeout=30) for future in pending]
    assert [pk for pk, _ in results] == [str(asset.pk), str(asset.pk)]
    assert len({pid for _, pid in results}) == 2
    asset.refresh_from_db()
    assert asset.notes == "并发独立修改后的资料"
    actual = facts(context)
    print("ASSET_DRAFT_CREATE_CONCURRENCY " + json.dumps({
        "scenario": "replay-and-controlled-edit", "workers": results,
        "after": actual, "lock_timeout_seconds": 10, "statement_timeout_seconds": 20,
    }, sort_keys=True))
    assert actual["assets"] == actual["draft_create_audits"] == actual["keyed_create_audits"] == 1
    assert AuditLog.objects.filter(company=context["company"], action="asset_draft_update",
                                  object_id=str(asset.pk)).count() == 1


def test_keyed_create_replay_and_controlled_delete_never_resurrect_asset(context):
    if connection.vendor != "postgresql":
        pytest.skip("Mixed keyed replay/delete locking requires PostgreSQL row locks")
    client, url, _, submitted = get_draft_page(context)
    assert client.post(url, submitted).status_code == 302
    asset = Asset.objects.get(company=context["company"])
    values = service_values(context, submitted)
    barrier = Barrier(2)

    def operation(kind):
        close_old_connections()
        try:
            pid = bounded_backend()
            actor = type(context["equipment"]).objects.get(pk=context["equipment"].pk)
            company = type(context["company"]).objects.get(pk=context["company"].pk)
            current = Asset.objects.get(pk=asset.pk)
            # Both workers reach here without any Company/Asset row lock held.
            barrier.wait(timeout=10)
            if kind == "delete":
                services.delete_asset_draft(actor=actor, asset=current, reason="并发独立删除草稿")
                return {"kind": kind, "outcome": "deleted", "pid": pid, "asset_id": str(asset.pk)}
            try:
                result = services.create_asset_draft(**{**values, "actor": actor, "company": company})
            except ValidationError as exc:
                assert "原草稿请求的结果记录已不存在" in str(exc)
                return {"kind": kind, "outcome": "missing-result", "pid": pid, "asset_id": None}
            except PermissionDenied as exc:
                # A delete between target read and fresh scope query removes view access.
                assert str(exc) == "您没有查看此资产的权限。"
                return {"kind": kind, "outcome": "removed-during-scope-read", "pid": pid, "asset_id": None}
            return {"kind": kind, "outcome": "returned-original", "pid": pid, "asset_id": str(result.pk)}
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(operation, kind) for kind in ("replay", "delete")]
        results = [future.result(timeout=30) for future in pending]
    assert len({result["pid"] for result in results}) == 2
    assert results[1]["outcome"] == "deleted"
    assert results[0]["outcome"] in {"returned-original", "missing-result", "removed-during-scope-read"}
    if results[0]["outcome"] == "returned-original":
        assert results[0]["asset_id"] == str(asset.pk)
    actual = facts(context)
    print("ASSET_DRAFT_CREATE_CONCURRENCY " + json.dumps({
        "scenario": "replay-and-controlled-delete", "workers": results,
        "after": actual, "lock_timeout_seconds": 10, "statement_timeout_seconds": 20,
    }, sort_keys=True))
    assert actual["assets"] == actual["custom_values"] == 0
    assert actual["draft_create_audits"] == actual["keyed_create_audits"] == 1
    assert AuditLog.objects.filter(company=context["company"], action="asset_draft_delete",
                                  object_id=str(asset.pk)).count() == 1
    assert actual["registrations"] == actual["issued_codes"] == 0


def test_real_get_key_allows_incomplete_standard_identity_draft_but_registration_is_strict(standard_context):
    from apps.assets.models import AssetIdentity, AssetQrIdentity

    ctx = standard_context
    electronic = make_category(ctx["company"], "04")
    variants = [
        ("management_attribute", {"management_attribute": "", "acquisition_date": "", "coding_year": "", "serial_number": ""}),
        ("coding_year_note", {"management_attribute": "FA", "acquisition_date": "", "coding_year": "2019", "coding_year_note": "", "serial_number": "KNOWN-2019"}),
        ("serial_number", {"management_attribute": "FA", "acquisition_date": "2020-08-01", "coding_year": "", "serial_number": ""}),
    ]
    for expected_error, changes in variants:
        extra = {"category": str(electronic.pk), **changes}
        client, url, page, submitted = get_draft_page(ctx, extra=extra)
        assert page.context["form"].fields["idempotency_key"].required
        before = facts(ctx)
        saved = client.post(url, submitted)
        assert saved.status_code == 302, f"Incomplete standard identity could not be saved as a draft: {expected_error}"
        after = facts(ctx)
        assert after["assets"] == before["assets"] + 1
        assert after["draft_create_audits"] == before["draft_create_audits"] + 1
        assert after["keyed_create_audits"] == before["keyed_create_audits"] + 1
        asset = Asset.objects.get(company=ctx["company"], pk=saved.url.rstrip("/").split("/")[-1])
        assert asset.asset_status == "draft" and asset.asset_code is None and asset.current_issued_code_id is None
        assert asset.category_id == electronic.pk
        assert asset.management_attribute == changes["management_attribute"]
        assert asset.coding_year == (int(changes["coding_year"]) if changes["coding_year"] else None)
        assert asset.coding_year_note == "" and asset.serial_number == changes["serial_number"]
        assert after["registrations"] == after["issued_codes"] == 0
        assert not AssetIdentity.objects.filter(company=ctx["company"]).exists()
        assert not AssetQrIdentity.objects.filter(company=ctx["company"]).exists()
        # Registration uses a fresh GET key: this tests action validation, not
        # the separate cross-action key policy or registration of this draft.
        strict_client, strict_url, strict_page, registration = get_draft_page(ctx, extra=extra)
        registration["asset_action"] = "register"
        assert registration["idempotency_key"] != submitted["idempotency_key"]
        rejected = strict_client.post(strict_url, registration)
        assert rejected.status_code == 200
        assert rejected.context["form"].registration_requested is True
        assert rejected.context["form"].errors.get(expected_error)
        assert rejected.context["form"]["idempotency_key"].value() == registration["idempotency_key"]
        assert rejected.context["form"]["asset_name"].value() == registration["asset_name"]
        assert facts(ctx) == after
        print("ASSET_DRAFT_CREATE_COMPATIBILITY " + json.dumps({
            "scenario": "incomplete_standard_identity", "missing_field": expected_error,
            "draft_http": saved.status_code, "registration_http": rejected.status_code,
            "before": before, "after_draft": after, "after_rejection": facts(ctx),
            "draft_key": submitted["idempotency_key"], "registration_key": registration["idempotency_key"],
        }, sort_keys=True))


def test_real_get_key_allows_draft_without_coding_scheme_but_registration_is_strict():
    from apps.assets.models import AssetIdentity, AssetQrIdentity
    from apps.masterdata.models import AssetCodingScheme
    from tests.test_sprint4_acceptance import _base_context

    # Existing acceptance fixture deliberately keeps initialization complete
    # with coding=none, so only coding availability differs from a normal page.
    ctx = _base_context("DRAFTRETRYNOAVAILABLECODE", coding="none", include_policy=False)
    assert ctx["company"].initialization_setting.initialization_completed
    assert not AssetCodingScheme.objects.filter(company=ctx["company"], status="active").exists()
    client, url, page, submitted = get_draft_page(ctx)
    assert page.context["form"].fields["idempotency_key"].required
    before = facts(ctx)
    saved = client.post(url, submitted)
    assert saved.status_code == 302, "Missing coding scheme incorrectly blocked saving a draft"
    after = facts(ctx)
    assert after["assets"] == after["draft_create_audits"] == after["keyed_create_audits"] == 1
    asset = Asset.objects.get(company=ctx["company"])
    assert asset.asset_status == "draft" and asset.asset_code is None and asset.current_issued_code_id is None
    strict_client, strict_url, strict_page, registration = get_draft_page(ctx)
    registration["asset_action"] = "register"
    assert registration["idempotency_key"] != submitted["idempotency_key"]
    rejected = strict_client.post(strict_url, registration)
    assert rejected.status_code == 200
    assert rejected.context["form"].registration_requested is True
    assert "公司必须且只能解析出一个生效的默认编码方案。" in rejected.content.decode()
    assert rejected.context["form"]["idempotency_key"].value() == registration["idempotency_key"]
    assert rejected.context["form"]["asset_name"].value() == registration["asset_name"]
    assert facts(ctx) == after
    assert after["registrations"] == after["issued_codes"] == 0
    assert not AssetIdentity.objects.filter(company=ctx["company"]).exists()
    assert not AssetQrIdentity.objects.filter(company=ctx["company"]).exists()
    print("ASSET_DRAFT_CREATE_COMPATIBILITY " + json.dumps({
        "scenario": "no_available_coding_scheme", "draft_http": saved.status_code,
        "registration_http": rejected.status_code, "before": before,
        "after_draft": after, "after_rejection": facts(ctx),
        "draft_key": submitted["idempotency_key"], "registration_key": registration["idempotency_key"],
    }, sort_keys=True))
