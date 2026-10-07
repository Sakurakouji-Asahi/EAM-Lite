"""Scope changes submit only the chosen range, while strict backend rules remain."""
import pytest
from django.urls import reverse

from tests.test_inventory_draft_edit_revision import page_payload
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import inventory_context

pytestmark = pytest.mark.django_db


def test_empty_checkbox_range_has_visible_error_and_does_not_overwrite_draft(client):
    context, asset, _ = inventory_context("SCOPEEMPTY")
    task = _draft(context, "SCOPEEMPTY-T", inventory_type="special", scope_type="selected_assets",
        scope_department=None, selected_asset_ids=[str(asset.pk)])
    client.force_login(context["finance"])
    url = reverse("inventory:task-edit", args=[task.pk])
    opened = client.get(url)
    payload = page_payload(opened)
    payload["selected_asset_ids_ui"] = []
    payload["selected_asset_ids"] = ""
    failed = client.post(url,payload)
    assert failed.status_code == 200
    assert "selected_asset_ids" in failed.context["form"].errors
    html = failed.content.decode()
    message = str(failed.context["form"].errors["selected_asset_ids"][0])
    assert message in html[html.index("data-scope-assets"):]
    assert "inventory-scope-assistance.js" in html
    assert 'data-inventory-scope-assistance' in html
    assert failed.context["form"]["expected_revision"].value() == payload["expected_revision"]
    task.refresh_from_db()
    assert task.scope_definition_json["selected_asset_ids"] == [str(asset.pk)]
    assert task.status == "draft" and task.task_assets.count() == 0


def test_chosen_scope_only_payload_saves_but_unrelated_scope_payload_still_rejected(client):
    context, asset, _ = inventory_context("SCOPECHANGE")
    task = _draft(context, "SCOPECHANGE-T", inventory_type="special", scope_type="selected_assets",
        scope_department=None, selected_asset_ids=[str(asset.pk)])
    client.force_login(context["finance"])
    url = reverse("inventory:task-edit", args=[task.pk])
    payload = page_payload(client.get(url))
    payload.update(scope_type="department",scope_department=str(context["department"].pk),
        selected_asset_ids_ui=[],selected_asset_ids="",scope_category=str(context["category"].pk))
    rejected = client.post(url,payload)
    assert rejected.status_code == 200
    assert "scope_category" in rejected.context["form"].errors
    task.refresh_from_db()
    assert task.scope_type == "selected_assets"
    # Native formdata strips dormant selections without changing page values.
    payload.pop("scope_category")
    payload.pop("scope_location",None)
    saved = client.post(url,payload)
    assert saved.status_code == 302
    task.refresh_from_db()
    assert task.scope_type == "department" and task.scope_department_id == context["department"].pk
    assert not task.scope_definition_json.get("selected_asset_ids")
    assert task.status == "draft" and task.snapshot_at is None and task.task_assets.count() == 0
