"""Find personal inventory work without widening task access or stale counts."""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.inventory.models import InventoryResolution
from apps.inventory.services import (
    create_inventory_surplus, publish_inventory_task, resolve_inventory_difference,
    stop_inventory_scanning,
)
from tests.test_sprint3_support import make_user
from tests.test_sprint8_services import _draft, _published, _scan
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db


def _counts(page):
    return {card["value"]:card["count"] for card in page.context["status_cards"]}


def _ids(page):
    return {task.pk for task in page.context["tasks"]}


def test_personal_filters_and_status_cards_preserve_search_and_permission_scope(client):
    context, _asset, _qr = inventory_context("LISTPERSONAL")
    colleague = make_user("list-personal-colleague", "employee")
    own_draft = _draft(context, "LISTPERSONAL-OWN", actor=context["equipment"], assignees=[context["equipment"], colleague])
    assigned = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "LISTPERSONAL-ASSIGNED", assignees=[context["equipment"]]),
    )
    created = _draft(context, "LISTPERSONAL-CREATED", actor=context["equipment"], assignees=[colleague])
    other = _draft(context, "LISTPERSONAL-OTHER", assignees=[colleague])
    _draft(context, "DIFFERENT-SEARCH", assignees=[context["equipment"]])
    url = reverse("inventory:task-list")
    client.force_login(context["equipment"])
    page = client.get(url, {"q":"LISTPERSONAL", "relation":"assigned", "status":"draft", "page":"2"})
    assert page.status_code == 200 and _ids(page) == {own_draft.pk}
    counts = _counts(page)
    assert counts[""] == 2 and counts["draft"] == 1 and counts["in_progress"] == 1
    card = next(card for card in page.context["status_cards"] if card["value"] == "in_progress")
    assert parse_qs(urlsplit(card["url"]).query) == {"q":["LISTPERSONAL"], "relation":["assigned"], "status":["in_progress"]}
    running = client.get(url + card["url"])
    assert _ids(running) == {assigned.pk}
    assert reverse("inventory:task-scan", args=[assigned.pk]) in running.content.decode()
    relation_chip = next(row for row in page.context["active_filters"] if row["label"] == "任务关系")
    assert _ids(client.get(url + relation_chip["url"])) == {own_draft.pk, created.pk, other.pk}
    own = client.get(url, {"q":"LISTPERSONAL", "relation":"created"})
    assert _ids(own) == {own_draft.pk, created.pk}
    assert _counts(own)[""] == 2

    client.force_login(make_user("list-personal-management", "management"))
    read_only = client.get(url, {"q":"LISTPERSONAL", "status":"in_progress"})
    assert _ids(read_only) == {assigned.pk} and _counts(read_only)[""] == 4
    assert reverse("inventory:task-scan", args=[assigned.pk]) not in read_only.content.decode()
    client.force_login(colleague)
    scoped = client.get(url, {"q":"LISTPERSONAL"})
    assert _ids(scoped) == {own_draft.pk, created.pk, other.pk}
    assert _counts(scoped)[""] == 3 and _counts(scoped)["in_progress"] == 0
    client.force_login(make_user("list-personal-outsider", "employee"))
    empty = client.get(url, {"q":"LISTPERSONAL"})
    assert not _ids(empty) and not any(_counts(empty).values())
    assert "LISTPERSONAL-ASSIGNED" not in empty.content.decode()


def test_unresolved_filter_excludes_effective_conclusions_but_keeps_pending_surplus(client):
    context, asset, qr = inventory_context("LISTWORK")
    resolved = _published(context, "LISTWORK-RESOLVED")
    resolved = stop_inventory_scanning(actor=context["finance"], task=resolved, reason="现场完成", idempotency_key="list-resolved-stop")
    row = resolved.task_assets.get(asset=asset)
    resolve_inventory_difference(
        actor=context["finance"], task_asset=row, resolution_type="loss_confirmed",
        conclusion="已确认盘亏，保留资产另行办理", idempotency_key="list-resolved-conclusion",
    )
    pending = _published(context, "LISTWORK-PENDING")
    pending = stop_inventory_scanning(actor=context["finance"], task=pending, reason="现场结束", idempotency_key="list-pending-stop")
    surplus_task = _published(context, "LISTWORK-SURPLUS")
    _scan(context, surplus_task, qr, "list-surplus-normal")
    create_inventory_surplus(
        actor=context["equipment"], task=surplus_task, temporary_name="未建账工装",
        temporary_category_text="工装", temporary_location_text="车间角落", idempotency_key="list-pending-surplus",
    )
    surplus_task = stop_inventory_scanning(actor=context["finance"], task=surplus_task, reason="现场完成", idempotency_key="list-surplus-stop")
    running = _published(context, "LISTWORK-RUNNING")
    client.force_login(context["equipment"])
    url = reverse("inventory:task-list")
    page = client.get(url, {"q":"LISTWORK", "work":"unresolved"})
    assert page.status_code == 200
    assert _ids(page) == {pending.pk, surplus_task.pk}
    assert _counts(page)[""] == _counts(page)["reconciliation"] == 2
    assert "LISTWORK-RESOLVED" not in page.content.decode()
    assert "row_view=unresolved&amp;surplus_view=pending" in page.content.decode()
    resolved.refresh_from_db()
    row.refresh_from_db()
    assert resolved.status == "reconciliation" and row.inventory_status == "resolved"
    assert InventoryResolution.objects.filter(inventory_task_asset=row, status="active").exists()
    # Work counts still honor this pending-work query when switching status.
    blank = client.get(url, {"q":"LISTWORK", "work":"unresolved", "status":"draft"})
    assert not _ids(blank) and _counts(blank)["reconciliation"] == 2
    unscanned = client.get(url, {"q":"LISTWORK", "work":"unscanned"})
    assert _ids(unscanned) == {running.pk}
    assert _counts(unscanned)[""] == _counts(unscanned)["in_progress"] == 1


def test_invalid_filters_and_empty_results_offer_recovery_without_unscoped_fallback(client):
    context, _asset, _qr = inventory_context("LISTRECOVER")
    task = _draft(context, "LISTRECOVER-T")
    client.force_login(context["equipment"])
    url = reverse("inventory:task-list")
    for query in ({"status":"invented"}, {"relation":"anybody"}, {"work":"invented"}, {"q":"x" * 201}):
        page = client.get(url, query)
        assert page.status_code == 400 and page.context["filter_form"].errors
        assert not _ids(page) and not any(_counts(page).values())
        assert "重置筛选" in page.content.decode()
    page = client.get(url, {"q":"找不到的任务", "relation":"assigned"})
    assert page.status_code == 200 and not _ids(page)
    q_chip = next(row for row in page.context["active_filters"] if row["label"] == "任务编号或名称")
    recovered = client.get(url + q_chip["url"])
    assert _ids(recovered) == {task.pk}
    assert recovered.context["relation"] == "assigned"
