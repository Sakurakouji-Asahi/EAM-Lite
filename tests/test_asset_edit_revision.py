"""Browser stale-edit protection and the fresh-lock revision boundary."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django import forms
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from apps.assets import services
from apps.assets.models import Asset, AssetRegistration
from apps.audit.models import AuditLog
from tests.test_asset_registration_finance_separation import context
from tests.test_asset_registration_http_reports import browser_data
from tests.test_sprint3_support import make_asset, make_category, make_custom_field, make_department, make_employee, make_user


pytestmark = pytest.mark.django_db(transaction=True)


def edit_revision(client, url):
    page = client.get(url)
    assert page.status_code == 200
    return page.context["form"]["expected_revision"].value()


def browser_payload(page):
    payload = {"asset_action": "draft"}
    for field in page.context["form"]:
        value = field.value()
        if isinstance(field.field.widget, forms.CheckboxInput):
            if value:
                payload[field.html_name] = "on"
        else:
            payload[field.html_name] = "" if value is None else str(value)
    for custom_form in page.context["custom_value_forms"]:
        field = custom_form["value"]
        value = field.value()
        payload[field.html_name] = "" if value is None else str(value)
    return payload


def draft(context, **overrides):
    return make_asset(
        actor=context["equipment"], company=context["company"], category=context["category"],
        department=context["department"], employee=context["employee"], location=context["location"],
        notes="原始备注", brand="原始品牌", **overrides,
    )


def logged_client(actor):
    client = Client()
    client.force_login(actor)
    return client


def assert_revision_rejection(response, url, submitted):
    assert response.status_code == 200
    assert response.context["form"].errors["expected_revision"]
    assert response.context["form"]["brand"].value() == submitted["brand"]
    assert (response.context["form"]["expected_revision"].value() or "") == submitted.get("expected_revision", "")
    html = response.content.decode()
    assert 'data-unsaved-guard="true"' in html
    assert "重新打开最新编辑页面" in html
    assert f'href="{url}" data-reload-asset-edit' in html


@pytest.mark.parametrize("changed_field", ["notes", "custom-text", "category", "scope"])
def test_two_clients_cannot_overwrite_newer_draft_values_and_can_reopen(context, changed_field):
    custom_field = None
    custom_values = None
    if changed_field == "custom-text":
        custom_field = make_custom_field(context["company"], context["category"], "EDIT_REVISION_TEXT", "text")
        custom_values = {custom_field.pk: "原始扩展资料"}
    asset = draft(context, custom_values=custom_values)
    url = reverse("assets:asset-edit", args=[asset.pk])
    client_a, client_b = logged_client(context["equipment"]), logged_client(context["equipment"])
    page_a, page_b = client_a.get(url), client_b.get(url)
    assert page_a.status_code == page_b.status_code == 200
    payload_a, payload_b = browser_payload(page_a), browser_payload(page_b)
    if changed_field == "notes":
        payload_a["notes"] = "同事已保存的新备注"
        expected_value = payload_a["notes"]
    elif changed_field == "custom-text":
        payload_a[f"custom_{custom_field.pk}-value"] = "同事已保存的新扩展资料"
        expected_value = payload_a[f"custom_{custom_field.pk}-value"]
    elif changed_field == "category":
        category = make_category(context["company"], "EDIT-OTHER-CAT")
        payload_a["category"] = str(category.pk)
        expected_value = category.pk
    else:
        department = make_department(context["company"], "EDIT-OTHER-DEPT")
        employee = make_employee(context["company"], department, "EDIT-OTHER-EMP")
        payload_a.update(department=str(department.pk), responsible_employee=str(employee.pk))
        expected_value = (department.pk, employee.pk)

    def stored_value():
        asset.refresh_from_db()
        if changed_field == "custom-text":
            return asset.custom_values.get(custom_field=custom_field).value_text
        if changed_field == "category":
            return asset.category_id
        if changed_field == "scope":
            return (asset.department_id, asset.responsible_employee_id)
        return asset.notes

    first = client_a.post(url, payload_a)
    assert first.status_code == 302
    assert stored_value() == expected_value
    assert asset.brand == "原始品牌"
    after_a_timestamp = asset.updated_at
    after_a_audit_count = AuditLog.objects.count()
    original_b_token = payload_b["expected_revision"]
    payload_b["brand"] = "B 想保存的新品牌"
    second = client_b.post(url, payload_b)
    assert_revision_rejection(second, url, payload_b)
    assert stored_value() == expected_value
    assert asset.brand == "原始品牌" and asset.updated_at == after_a_timestamp
    assert AuditLog.objects.count() == after_a_audit_count
    assert second.context["form"]["expected_revision"].value() == original_b_token
    # Re-rendering must not replace an old token while retaining the old inputs.
    assert_revision_rejection(client_b.post(url, browser_payload(second)), url, payload_b)
    assert stored_value() == expected_value and AuditLog.objects.count() == after_a_audit_count
    latest = client_b.get(url)
    fresh = browser_payload(latest)
    assert fresh["expected_revision"] != original_b_token
    fresh["brand"] = payload_b["brand"]
    assert client_b.post(url, fresh).status_code == 302
    assert stored_value() == expected_value and asset.brand == payload_b["brand"]
    assert asset.asset_status == "draft" and asset.current_issued_code_id is None
    assert not AssetRegistration.objects.filter(asset=asset).exists()


@pytest.mark.parametrize("bad_token", ["missing", "tampered", "other-asset", "other-actor"])
def test_edit_requires_an_authentic_version_for_this_asset_and_actor(context, bad_token):
    asset = draft(context)
    url = reverse("assets:asset-edit", args=[asset.pk])
    client = logged_client(context["equipment"])
    payload = browser_payload(client.get(url))
    if bad_token == "missing":
        payload.pop("expected_revision")
    elif bad_token == "tampered":
        token = payload["expected_revision"]
        payload["expected_revision"] = token[:-1] + ("a" if token[-1] != "a" else "b")
    elif bad_token == "other-asset":
        other = draft(context, asset_name="其他草稿")
        payload["expected_revision"] = edit_revision(client, reverse("assets:asset-edit", args=[other.pk]))
    else:
        other_actor = make_user("revision-other-equipment", "equipment")
        payload["expected_revision"] = edit_revision(logged_client(other_actor), url)
    payload["brand"] = "禁止被保存的品牌"
    before = AuditLog.objects.count()
    response = client.post(url, payload)
    assert_revision_rejection(response, url, payload)
    asset.refresh_from_db()
    assert asset.notes == "原始备注" and asset.brand == "原始品牌"
    assert AuditLog.objects.count() == before


def test_fresh_unchanged_edit_saves_and_repeating_its_old_page_does_not_write_again(context):
    asset = draft(context)
    url = reverse("assets:asset-edit", args=[asset.pk])
    client = logged_client(context["equipment"])
    payload = browser_payload(client.get(url))
    assert client.post(url, payload).status_code == 302
    asset.refresh_from_db()
    assert asset.notes == "原始备注" and asset.brand == "原始品牌"
    timestamp, audit_count = asset.updated_at, AuditLog.objects.count()
    repeated = client.post(url, payload)
    assert_revision_rejection(repeated, url, payload)
    asset.refresh_from_db()
    assert asset.updated_at == timestamp and AuditLog.objects.count() == audit_count


def test_saved_token_does_not_bypass_withdrawn_edit_permission(context):
    asset = draft(context)
    url = reverse("assets:asset-edit", args=[asset.pk])
    client = logged_client(context["equipment"])
    payload = browser_payload(client.get(url))
    # Retain the existing read-only role, so only editing is withdrawn.
    context["equipment"].groups.set([Group.objects.get(name="system_admin")])
    assert client.get(reverse("assets:asset-detail", args=[asset.pk])).status_code == 200
    payload["brand"] = "撤权后禁止保存"
    timestamp, audit_count = asset.updated_at, AuditLog.objects.count()
    assert client.post(url, payload).status_code == 403
    asset.refresh_from_db()
    assert asset.notes == "原始备注" and asset.brand == "原始品牌"
    assert asset.updated_at == timestamp and AuditLog.objects.count() == audit_count


def test_revision_is_compared_at_fresh_service_lock_after_form_validation(context, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Independent committed writer requires PostgreSQL.")
    asset = draft(context)
    url = reverse("assets:asset-edit", args=[asset.pk])
    client = logged_client(context["equipment"])
    payload = browser_payload(client.get(url))
    payload["brand"] = "旧页面准备提交的品牌"
    real_lock = services._lock_current_asset
    injected = False

    def committed_other_writer():
        close_old_connections()
        try:
            services.update_asset_draft(
                actor=type(context["equipment"]).objects.get(pk=context["equipment"].pk),
                asset=type(asset).objects.get(pk=asset.pk), data={"notes": "校验后另一事务提交的备注"},
            )
        finally:
            close_old_connections()

    def observe_before_actual_lock(value):
        nonlocal injected
        if not injected:
            injected = True
            # The HTTP form has validated, but its service has not locked the row yet.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(committed_other_writer).result(timeout=20)
        return real_lock(value)

    with monkeypatch.context() as patch:
        patch.setattr(services, "_lock_current_asset", observe_before_actual_lock)
        response = client.post(url, payload)
    assert injected
    assert_revision_rejection(response, url, payload)
    asset.refresh_from_db()
    assert asset.notes == "校验后另一事务提交的备注" and asset.brand == "原始品牌"
    assert AuditLog.objects.filter(action="asset_draft_update", object_id=str(asset.pk)).count() == 1


@pytest.mark.parametrize("action", ["draft", "register"])
def test_new_create_and_registration_do_not_require_edit_version(context, action):
    client = logged_client(context["equipment"])
    url = reverse("assets:asset-create")
    page = client.get(url)
    assert page.status_code == 200 and "expected_revision" not in page.context["form"].fields
    payload = browser_data(context, asset_action=action, expected_revision="unrelated-edit-value")
    assert client.post(url, payload).status_code == 302
    asset = Asset.objects.get(company=context["company"])
    assert asset.asset_status == ("draft" if action == "draft" else "pending_label")
    assert AssetRegistration.objects.filter(asset=asset).exists() == (action == "register")
