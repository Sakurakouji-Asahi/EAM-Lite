"""Plan finding and maintenance retain scope, dates and the originating query."""
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.maintenance.models import MaintenancePlan, MaintenanceRecord
from apps.maintenance.plan_workspace import plan_return_url
from apps.maintenance.services import create_maintenance_plan, set_maintenance_plan_status
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db


def make_plan(ctx, name, *, responsible=None, status="active"):
    plan = create_maintenance_plan(
        actor=ctx["equipment"], company=ctx["company"], asset=ctx["asset"],
        name=name, cycle_value=1, cycle_unit="month", advance_notice_days=3,
        responsible_employee=responsible or ctx["responsible"],
        standard_content="检查、清洁并记录合格标准", first_due_date=ctx["plan"].first_due_date,
    )
    if status != "active":
        plan = set_maintenance_plan_status(actor=ctx["equipment"], plan=plan, status=status,
                                          reason="隔离测试计划状态")
    return plan


def test_plan_summary_uses_access_and_search_scope_before_status(client):
    ctx = maintenance_context("PLANLIST")
    paused = make_plan(ctx, "PLANLIST 月度保养 · 暂停计划", status="suspended")
    ended = make_plan(ctx, "PLANLIST 月度保养 · 终止计划", status="ended")
    hidden = make_plan(ctx, "PLANLIST 月度保养 · 他人负责", responsible=ctx["equipment_employee"])
    make_plan(ctx, "关键词以外的计划")
    client.force_login(ctx["responsible_user"])
    url = reverse("maintenance:plan-list")
    response = client.get(url, {"q": "月度保养", "department": ctx["department"].pk,
                                "status": "suspended", "page": 8})
    assert response.status_code == 200
    assert [plan.pk for plan in response.context["plans"]] == [paused.pk]
    cards = response.context["plan_status_cards"]
    assert [(card["label"], card["count"]) for card in cards] == [
        ("全部", 3), ("启用", 1), ("暂停", 1), ("已终止", 1)]
    assert [card["label"] for card in cards if card["active"]] == ["暂停"]
    all_params = parse_qs(urlsplit(cards[0]["url"]).query)
    assert all_params == {"q": ["月度保养"], "department": [str(ctx["department"].pk)]}
    chips = response.context["plan_filter_chips"]
    assert len(chips) == 3
    assert "page" not in chips[0]["url"] and "status=suspended" in chips[0]["url"]
    assert hidden.name not in response.content.decode() and ended.name not in response.content.decode()
    # The owner dropdown is populated only from accessible plans as before.
    invalid = client.get(url, {"responsible_employee": ctx["equipment_employee"].pk})
    assert invalid.status_code == 400
    assert invalid.context["page_obj"].paginator.count == 0
    assert all(card["count"] == 0 for card in invalid.context["plan_status_cards"])
    empty = client.get(url, {"q": "PLANLIST 不存在"})
    assert empty.status_code == 200 and "查看全部计划" in empty.content.decode()
    client.force_login(ctx["equipment"])
    broader = client.get(url, {"q": "月度保养", "responsible_employee": ctx["equipment_employee"].pk})
    assert broader.context["page_obj"].paginator.count == 1
    assert [card["count"] for card in broader.context["plan_status_cards"]] == [1, 1, 0, 0]


def test_plan_preview_save_create_and_status_return_to_original_list(client):
    ctx = maintenance_context("PLANFLOW")
    plan = ctx["plan"]
    client.force_login(ctx["equipment"])
    return_to = reverse("maintenance:plan-list") + "?" + urlencode({
        "q": "PLANFLOW", "status": "active", "responsible_employee": ctx["responsible"].pk, "page": 2})
    detail_url = reverse("maintenance:plan-detail", args=[plan.pk])
    detail = client.get(detail_url, {"return_to": return_to})
    assert detail.context["plan_list_url"] == return_to
    assert 'name="return_to"' in detail.content.decode()  # history filtering retains the source query
    edit_url = reverse("maintenance:plan-edit", args=[plan.pk])
    page = client.get(edit_url, {"return_to": return_to})
    data = {field.html_name: "" if field.value() is None else str(field.value())
            for field in page.context["form"]}
    data.update(return_to=return_to, cycle_value="2", action="preview")
    original = MaintenancePlan._base_manager.filter(pk=plan.pk).values().get()
    audit_count = AuditLog.objects.count()
    preview = client.post(edit_url, data)
    assert preview.status_code == 200 and not preview.context["form"].errors
    assert preview.context["date_preview"] and preview.context["plan_return_to"] == return_to
    assert preview.context["form"]["expected_revision"].value() == data["expected_revision"]
    assert MaintenancePlan._base_manager.filter(pk=plan.pk).values().get() == original
    assert AuditLog.objects.count() == audit_count
    data.pop("action")
    saved = client.post(edit_url, data)
    assert saved.status_code == 302 and saved.url == return_to
    plan.refresh_from_db()
    assert plan.cycle_value == 2 and plan.next_maintenance_date == original["next_maintenance_date"]
    assert not MaintenanceRecord.objects.filter(maintenance_plan=plan).exists()
    status_url = reverse("maintenance:plan-status", args=[plan.pk])
    status_page = client.get(status_url, {"return_to": return_to})
    assert status_page.context["plan_return_to"] == return_to and plan.name in status_page.content.decode()
    paused = client.post(status_url, {"status": "suspended", "reason": "", "return_to": return_to})
    assert paused.status_code == 302 and paused.url == return_to
    data.pop("expected_revision")
    data["name"] = "PLANFLOW 新建计划"
    created = client.post(reverse("maintenance:plan-create"), data)
    assert created.status_code == 302 and created.url == return_to
    assert MaintenancePlan.objects.filter(name=data["name"]).exists()


def test_native_edit_post_omits_disabled_asset_and_ignores_replacement(client):
    ctx = maintenance_context("PLANNATIVE")
    plan = ctx["plan"]
    client.force_login(ctx["equipment"])
    url = reverse("maintenance:plan-edit", args=[plan.pk])

    def native_fields(page):
        return {field.html_name: "" if field.value() is None else str(field.value())
                for field in page.context["form"] if not field.field.disabled}

    page = client.get(url)
    original = MaintenancePlan._base_manager.filter(pk=plan.pk).values().get()
    audit_count = AuditLog.objects.count()
    data = native_fields(page)
    assert "asset" not in data
    data.update(cycle_value="2", standard_content="现场新增的检查项目必须保留", action="preview")
    preview = client.post(url, data)
    assert preview.status_code == 200 and not preview.context["form"].errors
    assert preview.context["form"].fields["asset"].disabled
    assert str(preview.context["form"]["asset"].value()) == str(plan.asset_id)
    assert preview.context["form"]["standard_content"].value() == data["standard_content"]
    assert preview.context["form"]["expected_revision"].value() == data["expected_revision"]
    assert MaintenancePlan._base_manager.filter(pk=plan.pk).values().get() == original
    assert AuditLog.objects.count() == audit_count
    data = native_fields(preview)
    assert "asset" not in data
    saved = client.post(url, data)
    assert saved.status_code == 302 and saved.url == reverse("maintenance:plan-detail", args=[plan.pk])
    plan.refresh_from_db()
    assert plan.asset_id == original["asset_id"] and plan.cycle_value == 2
    assert plan.standard_content == data["standard_content"]
    assert plan.next_maintenance_date == original["next_maintenance_date"]
    # A client can send the disabled field manually; the original binding wins.
    data = native_fields(client.get(url))
    data.update(asset=str(uuid4()), name="PLANNATIVE 原关联不能被提交值替换")
    tampered = client.post(url, data)
    assert tampered.status_code == 302
    plan.refresh_from_db()
    assert plan.name == data["name"] and plan.asset_id == original["asset_id"]
    assert not MaintenanceRecord.objects.filter(maintenance_plan=plan).exists()


@pytest.mark.parametrize("return_to", [
    "https://example.test/maintenance/?q=test", "//example.test/maintenance/",
    "/maintenance/plans/new/", "/maintenance/problems/", "/maintenance/\\evil",
    "/maintenance/?q=test\n", "/maintenance/#other", "/maintenance/?q=" + "x" * 3000,
])
def test_plan_return_rejects_external_or_unrelated_navigation(return_to):
    assert plan_return_url(RequestFactory().post("/maintenance/plans/new/", {"return_to": return_to})) == ""
