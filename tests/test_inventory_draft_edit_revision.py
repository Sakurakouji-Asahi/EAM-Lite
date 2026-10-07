"""Regression assertions: an old edit page must preserve a newer saved draft."""
import json
import threading
import time
from unittest.mock import patch

import pytest
from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connections
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.assets.models import Asset
from apps.inventory import views
from apps.inventory.draft_revision import INVENTORY_EDIT_REVISION_SALT, inventory_task_revision_snapshot
from apps.inventory.models import InventoryTask, InventoryTaskAsset
from apps.inventory.services import publish_inventory_task, update_inventory_task_draft
from tests.test_sprint3_support import make_user
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import add_active_asset, inventory_context

pytestmark = pytest.mark.django_db(transaction=True)


def page_payload(page):
    assert page.status_code == 200
    data = {}
    for field in page.context["form"]:
        value = field.value()
        if isinstance(field.field, forms.ModelMultipleChoiceField):
            data[field.html_name] = [str(item) for item in value or []]
        else:
            data[field.html_name] = "" if value is None else str(value)
    data["selected_asset_ids_ui_present"] = "1"
    data["selected_asset_ids_ui"] = list(page.context["selected_ids"])
    return data


def persisted(task):
    task = InventoryTask.objects.get(pk=task.pk)
    return {
        "scope_json": task.scope_definition_json,
        "assignee_user_ids": sorted(str(value) for value in task.assignees.values_list("user_id", flat=True)),
        "remark": task.remark,
        "status": task.status,
        "snapshot_at": task.snapshot_at.isoformat() if task.snapshot_at else None,
        "expected_asset_count": task.expected_asset_count,
        "task_asset_count": InventoryTaskAsset.objects.filter(inventory_task=task).count(),
        "updated_audit_count": AuditLog.objects.filter(
            object_type=task._meta.object_name, object_id=str(task.pk), action="inventory.task_updated").count(),
    }


@pytest.mark.parametrize("change", ["selected_assets", "assignees"])
def test_old_inventory_draft_page_preserves_newer_scope_and_assignees(change):
    context, first, _ = inventory_context("INVSTALE" + change)
    second, _ = add_active_asset(context, "INVSTALESECOND" + change)
    first_user = make_user("inventory-stale-first-" + change, "employee")
    second_user = make_user("inventory-stale-second-" + change, "employee")
    task = _draft(context, "INVSTALETASK" + change, inventory_type="special",
                  scope_type="selected_assets", scope_department=None,
                  selected_asset_ids=[str(first.pk)], assignees=[first_user], remark="原草稿备注")
    ca, cb = Client(), Client()
    ca.force_login(context["finance"])
    cb.force_login(context["equipment"])
    url = reverse("inventory:task-edit", args=[task.pk])
    pa, pb = page_payload(ca.get(url)), page_payload(cb.get(url))
    before = persisted(task)
    pa["remark"] = "A 已保存的新备注"
    if change == "selected_assets":
        pa["selected_asset_ids"] = str(second.pk)
        pa["selected_asset_ids_ui"] = [str(second.pk)]
    else:
        pa["assignees"] = [str(second_user.pk)]
    a_response = ca.post(url, pa)
    assert a_response.status_code == 302, str(a_response.context["form"].errors) if a_response.context else ""
    saved = persisted(task)
    if change == "selected_assets":
        assert saved["scope_json"]["selected_asset_ids"] == [str(second.pk)]
    else:
        assert saved["assignee_user_ids"] == [str(second_user.pk)]
    assert saved["updated_audit_count"] == before["updated_audit_count"] + 1
    pb["remark"] = "B 只修改备注的旧页面"
    b_response = cb.post(url, pb)
    actual = persisted(task)
    print("INVENTORY_STALE_PERSISTED " + json.dumps({
        "change": change, "a_http": a_response.status_code, "b_http": b_response.status_code,
        "before": before, "after_a": saved, "after_b": actual,
    }, ensure_ascii=False, sort_keys=True))
    # Keep the intended business assertion, so the unfixed baseline really fails.
    assert actual["scope_json"] == saved["scope_json"], "Old page replaced the newer selected asset scope"
    assert actual["assignee_user_ids"] == saved["assignee_user_ids"], "Old page replaced the newer assignee set"
    assert actual["remark"] == saved["remark"]
    assert actual["updated_audit_count"] == saved["updated_audit_count"]
    assert actual["status"] == "draft" and actual["snapshot_at"] is None
    assert actual["expected_asset_count"] is None and actual["task_asset_count"] == 0


@pytest.fixture
def edit_context():
    context, first, _ = inventory_context("INVREVISION")
    second, _ = add_active_asset(context, "INVREVISIONSECOND")
    first_user = make_user("inventory-revision-first", "employee")
    second_user = make_user("inventory-revision-second", "employee")
    task = _draft(context, "INVREVISIONTASK", inventory_type="special", scope_type="selected_assets",
                  scope_department=None, selected_asset_ids=[str(first.pk)], assignees=[first_user])
    ca, cb = Client(), Client()
    ca.force_login(context["finance"])
    cb.force_login(context["equipment"])
    return {"context": context, "first": first, "second": second, "first_user": first_user,
            "second_user": second_user, "task": task, "ca": ca, "cb": cb,
            "url": reverse("inventory:task-edit", args=[task.pk])}


def database_state(task):
    task = InventoryTask.objects.get(pk=task.pk)
    return {"revision": inventory_task_revision_snapshot(task), "audit_count": AuditLog.objects.count(),
            "snapshots": list(InventoryTaskAsset.objects.filter(inventory_task=task).order_by("pk").values()),
            "assets": list(Asset.objects.filter(company_id=task.company_id).order_by("pk").values())}


def trusted_data(task, *, remark):
    task = InventoryTask.objects.get(pk=task.pk)
    return {"name": task.name, "inventory_type": task.inventory_type, "scope_type": task.scope_type,
            "scope_department": task.scope_department, "scope_location": task.scope_location,
            "scope_category": task.scope_category, "selected_asset_ids": task.scope_definition_json.get("selected_asset_ids", []),
            "planned_start": task.planned_start, "planned_end": task.planned_end, "remark": remark}


@pytest.mark.parametrize("invalid", ["missing", "tampered", "actor", "company", "task", "expired"])
def test_invalid_inventory_edit_revision_preserves_all_persisted_data(edit_context, invalid):
    ctx = edit_context
    data = page_payload(ctx["ca"].get(ctx["url"]))
    if invalid == "missing":
        data.pop("expected_revision")
    elif invalid == "tampered":
        data["expected_revision"] += "tampered"
    elif invalid == "actor":
        data["expected_revision"] = page_payload(ctx["cb"].get(ctx["url"]))["expected_revision"]
    else:
        signed = signing.loads(data["expected_revision"], salt=INVENTORY_EDIT_REVISION_SALT)
        if invalid == "company":
            signed["company"] = "999999999"
        elif invalid == "task":
            signed["task"] = "00000000-0000-0000-0000-000000000001"
        if invalid == "expired":
            with patch("django.core.signing.time.time", return_value=time.time() - 25 * 60 * 60):
                data["expected_revision"] = signing.dumps(signed, salt=INVENTORY_EDIT_REVISION_SALT, compress=True)
        else:
            data["expected_revision"] = signing.dumps(signed, salt=INVENTORY_EDIT_REVISION_SALT, compress=True)
    data["remark"] = "无效版本的输入仍应保留"
    before = database_state(ctx["task"])
    response = ctx["ca"].post(ctx["url"], data)
    assert response.status_code == 200 and response.context["form"].errors["expected_revision"]
    assert response.context["form"]["expected_revision"].value() == data.get("expected_revision")
    assert response.context["form"]["remark"].value() == data["remark"]
    assert "重新打开最新草稿" in response.content.decode()
    assert 'data-unsaved-guard="true"' in response.content.decode()
    assert database_state(ctx["task"]) == before


def test_empty_inventory_edit_post_is_bound_and_cannot_save(edit_context):
    ctx = edit_context
    before = database_state(ctx["task"])
    response = ctx["ca"].post(ctx["url"], {})
    assert response.status_code == 200 and response.context["form"].is_bound
    assert response.context["form"].errors["expected_revision"]
    assert database_state(ctx["task"]) == before


def test_fresh_reopen_saves_latest_scope_assignees_after_rejecting_old_page(edit_context):
    ctx = edit_context
    old = page_payload(ctx["cb"].get(ctx["url"]))
    fresh_a = page_payload(ctx["ca"].get(ctx["url"]))
    fresh_a.update(selected_asset_ids=str(ctx["second"].pk), selected_asset_ids_ui=[str(ctx["second"].pk)],
                   assignees=[str(ctx["second_user"].pk)], remark="A 最新范围与执行人")
    assert ctx["ca"].post(ctx["url"], fresh_a).status_code == 302
    saved = database_state(ctx["task"])
    old["remark"] = "B 尚未保存的输入"
    stale = ctx["cb"].post(ctx["url"], old)
    assert stale.status_code == 200 and stale.context["form"].errors["expected_revision"]
    assert stale.context["form"]["expected_revision"].value() == old["expected_revision"]
    assert stale.context["form"]["remark"].value() == old["remark"]
    assert database_state(ctx["task"]) == saved
    reopened = page_payload(ctx["cb"].get(ctx["url"]))
    assert reopened["expected_revision"] != old["expected_revision"]
    reopened["remark"] = "B 已核对最新草稿"
    assert ctx["cb"].post(ctx["url"], reopened).status_code == 302
    actual = persisted(ctx["task"])
    assert actual["scope_json"]["selected_asset_ids"] == [str(ctx["second"].pk)]
    assert actual["assignee_user_ids"] == [str(ctx["second_user"].pk)]
    assert actual["remark"] == reopened["remark"] and actual["updated_audit_count"] == 2


def test_unchecking_all_selected_assets_with_valid_revision_retains_scope_and_input(edit_context):
    ctx = edit_context
    data = page_payload(ctx["ca"].get(ctx["url"]))
    before = database_state(ctx["task"])
    old_token = data["expected_revision"]
    data.update(selected_asset_ids_ui=[], remark="全取消尚未保存")
    response = ctx["ca"].post(ctx["url"], data)
    assert response.status_code == 200 and response.context["form"].errors["selected_asset_ids"]
    assert "expected_revision" not in response.context["form"].errors
    assert response.context["form"]["expected_revision"].value() == old_token
    assert response.context["form"]["remark"].value() == data["remark"]
    assert response.context["selected_ids"] == set()
    assert database_state(ctx["task"]) == before


def test_trusted_inventory_update_without_browser_revision_remains_supported(edit_context):
    ctx = edit_context
    before = persisted(ctx["task"])
    updated = update_inventory_task_draft(actor=ctx["context"]["finance"], task=ctx["task"],
        data=trusted_data(ctx["task"], remark="trusted服务保存"), assignee_users=[ctx["first_user"]])
    after = persisted(updated)
    assert after["remark"] == "trusted服务保存" and after["updated_audit_count"] == before["updated_audit_count"] + 1
    assert after["scope_json"] == before["scope_json"] and after["assignee_user_ids"] == before["assignee_user_ids"]
    assert after["task_asset_count"] == 0 and after["status"] == "draft"


@pytest.mark.parametrize("writer_change", ["scope", "assignees", "revoke_permission"])
def test_independent_inventory_writer_after_valid_form_is_checked_under_locks(edit_context, monkeypatch, writer_change):
    ctx = edit_context
    data = page_payload(ctx["ca"].get(ctx["url"]))
    data["remark"] = "旧页通过表单后提交"
    actual_update = views.update_inventory_task_draft
    committed, errors = {}, []

    def writer():
        try:
            if writer_change == "revoke_permission":
                get_user_model().objects.get(pk=ctx["context"]["finance"].pk).groups.clear()
            else:
                payload = trusted_data(ctx["task"], remark="独立writer提交")
                if writer_change == "scope":
                    payload["selected_asset_ids"] = [str(ctx["second"].pk)]
                update_inventory_task_draft(actor=get_user_model().objects.get(pk=ctx["context"]["equipment"].pk),
                    task=ctx["task"], data=payload,
                    assignee_users=[ctx["second_user"] if writer_change == "assignees" else ctx["first_user"]])
            committed.update(state=database_state(ctx["task"]))
        except BaseException as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    def after_form_before_lock(**kwargs):
        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive() and not errors, repr(errors)
        return actual_update(**kwargs)

    monkeypatch.setattr(views, "update_inventory_task_draft", after_form_before_lock)
    response = ctx["ca"].post(ctx["url"], data)
    assert committed
    assert response.status_code == (403 if writer_change == "revoke_permission" else 200)
    if writer_change != "revoke_permission":
        assert response.context["form"].errors["expected_revision"]
        assert response.context["form"]["expected_revision"].value() == data["expected_revision"]
    assert database_state(ctx["task"]) == committed["state"]


def test_published_task_still_rejects_old_edit_page_and_keeps_snapshot(edit_context):
    ctx = edit_context
    data = page_payload(ctx["ca"].get(ctx["url"]))
    published = publish_inventory_task(actor=ctx["context"]["finance"], task=ctx["task"])
    assert published.status == "in_progress" and published.expected_asset_count == 1
    before = database_state(published)
    assert ctx["ca"].get(ctx["url"]).status_code == 403
    assert ctx["ca"].post(ctx["url"], data).status_code == 403
    assert database_state(published) == before


def test_inventory_create_token_and_idempotent_retry_are_unchanged(edit_context):
    ctx = edit_context
    url = reverse("inventory:task-create")
    page = ctx["ca"].get(url)
    assert page.status_code == 200 and "expected_revision" not in page.context["form"].fields
    data = page_payload(page)
    data.update(name="创建令牌兼容", inventory_type="special", scope_type="selected_assets",
                scope_department="", selected_asset_ids=str(ctx["first"].pk),
                selected_asset_ids_ui=[str(ctx["first"].pk)], assignees=[str(ctx["first_user"].pk)],
                planned_start=ctx["task"].planned_start.isoformat(), planned_end=ctx["task"].planned_end.isoformat(),
                remark="创建幂等保持", _draft_token=page.context["draft_token"])
    first_response = ctx["ca"].post(url, data)
    assert first_response.status_code == 302
    before = AuditLog.objects.count(), InventoryTask.objects.count()
    retried = ctx["ca"].post(url, data)
    assert retried.status_code == 302 and retried["Location"] == first_response["Location"]
    assert (AuditLog.objects.count(), InventoryTask.objects.count()) == before
