"""Repair follow-up uses the current asset scope and the native completion form."""
from urllib.parse import urlencode

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import complete_asset_repair, send_asset_for_repair
from apps.assets.models import Asset
from apps.assets.repair_workspace import repair_context
from tests.test_repair_complete_note import _Controls
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import active_asset_context, add_department_manager, add_target_assignment
from tests.test_sprint8_support import add_active_asset

pytestmark = pytest.mark.django_db


def send(context, asset, key, reason):
    return send_asset_for_repair(actor=context["equipment"], asset=asset,
        effective_at=timezone.now(), expected_status=asset.asset_status, reason=reason,
        remark="送修补充说明\n第二行", idempotency_key=key)


def test_repair_list_search_order_scope_and_read_only_access(client):
    context, first, _qr = active_asset_context("REPAIR-LIST")
    send(context, first, "repair-list-first", "接线端子松动")
    second, _qr = add_active_asset(context, "REPAIR-SECOND", status="idle")
    send(context, second, "repair-list-second", "传感器校验")
    department, employee, location = add_target_assignment(context, "REPAIR-PRIVATE")
    private, _qr = add_active_asset(context, "REPAIR-PRIVATE", department=department, employee=employee, location=location)
    send(context, private, "repair-list-private", "隐秘送修原因")
    before = list(Asset._base_manager.values())
    url = reverse("assets:repair-workbench")
    client.force_login(context["equipment"])
    page = client.get(url)
    assert page.status_code == 200 and page.context["scope_count"] == 3
    assert [asset.pk for asset in page.context["page_obj"]] == [first.pk, second.pk, private.pk]
    assert page.context["page_obj"][1].repair_context["restore_status"] == "闲置"
    recent = client.get(url, {"order": "recent"})
    assert [asset.pk for asset in recent.context["page_obj"]] == [private.pk, second.pk, first.pk]
    searched = client.get(url, {"q": "传感器"})
    assert searched.context["page_obj"].paginator.count == 1 and searched.context["scope_count"] == 3
    assert searched.context["page_obj"][0].pk == second.pk
    empty = client.get(url, {"q": "不存在的送修"})
    assert "查看全部维修中资产" in empty.content.decode()
    assert client.get(url, {"order": "invalid"}).context["page_obj"].paginator.count == 0
    manager = add_department_manager(context, "REPAIR-LIST", context["department"])
    client.force_login(manager)
    scoped = client.get(url)
    assert scoped.context["scope_count"] == 2 and private.asset_name not in scoped.content.decode()
    assert "隐秘送修原因" not in scoped.content.decode()
    client.force_login(make_user("repair-work-reader", "management"))
    read = client.get(url)
    assert read.context["scope_count"] == 3 and ">办理维修完成</a>" not in read.content.decode()
    assert client.get(reverse("assets:lifecycle-repair-complete", args=[first.pk])).status_code == 403
    client.force_login(make_user("repair-work-hr", "hr"))
    assert client.get(url).status_code == 403
    assert "维修中资产</a>" not in client.get(reverse("assets:asset-list")).content.decode()
    assert list(Asset._base_manager.values()) == before


def test_native_completion_latest_context_return_and_replay_preserve_notes_and_identity():
    context, asset, qr = active_asset_context("REPAIR-WORK-SAVE", status="idle")
    old_start = send(context, asset, "repair-old-start", "旧送修原因")
    complete_asset_repair(actor=context["equipment"], asset=asset, effective_at=timezone.now(),
        result="旧维修结束", idempotency_key="repair-old-complete")
    asset.refresh_from_db()
    start = send(context, asset, "repair-new-start", "本次需要校验")
    original = (asset.asset_code, asset.current_issued_code_id, qr.public_token)
    asset.refresh_from_db()
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["equipment"])
    return_to = reverse("assets:repair-workbench") + "?q=" + "REPAIR-WORK-SAVE&order=recent"
    url = reverse("assets:lifecycle-repair-complete", args=[asset.pk]) + "?" + urlencode({"return_to": return_to})
    page = client.get(url)
    assert page.context["cancel_url"] == return_to
    assert page.context["repair_context"]["start"].pk == start.pk
    assert "旧送修原因" not in page.content.decode() and "本次需要校验" in page.content.decode()
    assert page.context["form"].fields["reason"].label == "维修结果"
    assert page.context["repair_context"]["restore_status"] == "闲置"
    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert detail.context["repair_context"]["start"].pk == start.pk
    form = [form for form in _Controls(page).forms if {"reason", "remark"} <= form["names"]][0]
    data = dict(form["values"])
    data.update(reason="", remark="换件并校验\n复测正常")
    invalid = client.post(url, data, HTTP_ORIGIN="http://testserver")
    assert invalid.status_code == 200 and invalid.context["form"].errors["reason"]
    assert invalid.context["form"]["remark"].value() == data["remark"]
    assert invalid.context["repair_context"]["start"].pk == start.pk
    data["reason"] = "本次试运行正常"
    saved = client.post(url, data, HTTP_ORIGIN="http://testserver")
    assert saved.status_code == 302 and saved.url == return_to
    assert client.post(url, data, HTTP_ORIGIN="http://testserver").url == return_to
    asset.refresh_from_db()
    qr.refresh_from_db()
    assert asset.asset_status == "idle"
    assert (asset.asset_code, asset.current_issued_code_id, qr.public_token) == original
    completed = asset.movements.filter(movement_type="repair_complete").exclude(reason="旧维修结束").get()
    assert completed.remark == f"对应送修变动：{start.pk}\n" + data["remark"]
    assert str(old_start.pk) not in completed.remark
    assert client.get(return_to).context["page_obj"].paginator.count == 0
    assert client.get(reverse("assets:asset-detail", args=[asset.pk])).context["repair_context"] is None
    external = client.get(reverse("assets:lifecycle-repair-complete", args=[asset.pk]), {"return_to": "https://outside.invalid/"})
    assert external.context["cancel_url"] == reverse("assets:asset-detail", args=[asset.pk])


def test_repair_context_handles_missing_history_without_inventing_restore_state():
    asset = Asset(asset_status="under_repair")
    assert repair_context(asset, start=None) == {"start": None}
    asset.asset_status = "in_use"
    assert repair_context(asset) is None
