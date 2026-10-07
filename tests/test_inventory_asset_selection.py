"""The draft selection controls keep the full submitted asset range intact."""

from html.parser import HTMLParser

import pytest
from django.urls import reverse

from tests.test_inventory_draft_edit_revision import page_payload
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import add_active_asset, inventory_context


pytestmark = pytest.mark.django_db


class _SelectionInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.checkboxes = {}
        self.search = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "input" and values.get("name") == "selected_asset_ids_ui":
            self.checkboxes[values["value"]] = values
        if tag == "input" and "data-inventory-asset-search" in values:
            self.search = values


def test_selection_page_renders_all_eligible_assets_and_existing_checked_range(client):
    context, first, _qr = inventory_context("INVSELECTUX")
    second, _ = add_active_asset(context, "INVSELECTUX-SECOND")
    second.equipment_number = "SELECTION-SEARCH-EQ"
    second.save(update_fields=["equipment_number"])
    task = _draft(context, "INVSELECTUX-T", inventory_type="special", scope_type="selected_assets",
                  scope_department=None, selected_asset_ids=[str(first.pk)])
    client.force_login(context["finance"])
    page = client.get(reverse("inventory:task-edit", args=[task.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    parsed = _SelectionInputs(html)
    assert set(parsed.checkboxes) == {str(first.pk), str(second.pk)}
    assert "checked" in parsed.checkboxes[str(first.pk)]
    assert "checked" not in parsed.checkboxes[str(second.pk)]
    assert all("disabled" not in values for values in parsed.checkboxes.values())
    assert parsed.checkboxes[str(first.pk)]["aria-label"] == f"选择资产 {first.asset_code} {first.asset_name}"
    assert parsed.search is not None and "name" not in parsed.search
    assert "SELECTION-SEARCH-EQ" in html
    assert "inventory-asset-selection.js" in html
    assert "选择当前显示" in html and "取消当前显示" in html
    assert "未显示的已选资产会保留" in html
    assert page.context["form"]["expected_revision"].value()
    task.refresh_from_db()
    assert task.scope_definition_json["selected_asset_ids"] == [str(first.pk)]


def test_selection_submission_and_error_response_preserve_every_checked_asset(client):
    context, first, _qr = inventory_context("INVSELECTSUBMIT")
    second, _ = add_active_asset(context, "INVSELECTSUBMIT-SECOND")
    task = _draft(context, "INVSELECTSUBMIT-T", inventory_type="special", scope_type="selected_assets",
                  scope_department=None, selected_asset_ids=[str(first.pk)])
    client.force_login(context["finance"])
    url = reverse("inventory:task-edit", args=[task.pk])
    data = page_payload(client.get(url))
    # The browser submits checked rows even when the local search hides them.
    data["selected_asset_ids_ui"] = [str(first.pk), str(second.pk)]
    data["name"] = ""
    failed = client.post(url, data)
    assert failed.status_code == 200 and "name" in failed.context["form"].errors
    assert failed.context["selected_ids"] == {str(first.pk), str(second.pk)}
    checkboxes = _SelectionInputs(failed.content.decode()).checkboxes
    assert all("checked" in checkboxes[str(asset.pk)] for asset in (first, second))
    task.refresh_from_db()
    assert task.scope_definition_json["selected_asset_ids"] == [str(first.pk)]
    data["name"] = task.name
    saved = client.post(url, data)
    assert saved.status_code == 302
    task.refresh_from_db()
    assert set(task.scope_definition_json["selected_asset_ids"]) == {str(first.pk), str(second.pk)}
    assert task.status == "draft" and task.snapshot_at is None
    assert task.task_assets.count() == 0
