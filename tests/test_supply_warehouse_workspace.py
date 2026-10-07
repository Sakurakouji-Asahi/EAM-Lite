from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.models import Company
from apps.supplies import warehouse_workspace
from apps.supplies.models import SupplyStockLedger
from apps.supplies.services import (cancel_supply_count_task, close_supply_count_task,
    publish_supply_count_task, record_supply_count, stop_supply_count_entry)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_supply_warehouse, make_user, seed_supply_stock
from tests.test_sprint17_services import make_count


pytestmark = pytest.mark.django_db


@pytest.fixture
def warehouse_context(client):
    company, actor, _, _, source, target, first, second = supply_context()
    clear = make_supply_warehouse(company, "CLEAR")
    for index, (warehouse, item) in enumerate(((source, first), (target, second), (clear, first))):
        seed_supply_stock(actor=actor, company=company, warehouse=warehouse, item=item,
            quantity="1.2345", unit_cost="10", key=f"warehouse-workspace-stock-{index}")
    active = make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=source, key="warehouse-workspace-active")
    publish_supply_count_task(actor=actor, task=active)
    recon = make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=target, key="warehouse-workspace-recon")
    publish_supply_count_task(actor=actor, task=recon)
    for line in recon.lines.all():
        record_supply_count(actor=actor, line=line, counted_quantity=line.expected_quantity, remark="")
    stop_supply_count_entry(actor=actor, task=recon)
    closed = make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=clear, key="warehouse-workspace-closed")
    publish_supply_count_task(actor=actor, task=closed)
    for line in closed.lines.all():
        record_supply_count(actor=actor, line=line, counted_quantity=line.expected_quantity, remark="")
    stop_supply_count_entry(actor=actor, task=closed)
    close_supply_count_task(actor=actor, task=closed)
    cancelled = make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=clear, key="warehouse-workspace-cancelled")
    cancel_supply_count_task(actor=actor, task=cancelled, reason="已取消安排")
    make_count(actor=actor, company=company, domain="warehouse_stock", warehouse=clear, key="warehouse-workspace-draft")
    client.force_login(actor)
    return company, actor, source, target, clear, active, recon


def test_freeze_badges_and_filter_match_live_service_states_and_keep_query_scope(client, warehouse_context):
    _, actor, source, target, clear, active, _ = warehouse_context
    url = reverse("supplies:warehouse-list")
    page = client.get(url)
    assert page.context["warehouse_summary"] == {"total": 3, "frozen": 2}
    rows = {row["warehouse"].pk: row for row in page.context["warehouse_rows"]}
    assert rows[source.pk]["warehouse"].count_frozen
    assert rows[target.pk]["warehouse"].count_frozen
    assert rows[target.pk]["freeze_task"].status == "reconciliation"
    assert not rows[clear.pk]["warehouse"].count_frozen and rows[clear.pk]["freeze_task"] is None
    frozen = client.get(url, {"freeze": "frozen"})
    assert {row.pk for row in frozen.context["page_obj"]} == {source.pk, target.pk}
    clear_only = client.get(url, {"freeze": "unfrozen"})
    assert {row.pk for row in clear_only.context["page_obj"]} == {clear.pk}
    searched = client.get(url, {"q": source.code, "freeze": "unfrozen"})
    assert searched.context["warehouse_summary"] == {"total": 1, "frozen": 1}
    assert not searched.context["warehouse_rows"]
    cancel_supply_count_task(actor=actor, task=active, reason="冻结提示实时刷新验证")
    refreshed = client.get(reverse("supplies:warehouse-detail", args=[source.pk]))
    assert not refreshed.context["warehouse"].count_frozen
    assert "当前无仓库盘点冻结" in refreshed.content.decode()


def test_business_links_select_exact_warehouse_and_active_task_without_writes(client, warehouse_context):
    _, _, source, _, _, active, _ = warehouse_context
    before_ledger = SupplyStockLedger.objects.count()
    before_audit = AuditLog.objects.count()
    page = client.get(reverse("supplies:warehouse-detail", args=[source.pk]))
    business = page.context["warehouse_business"]
    assert business["freeze_task"].pk == active.pk
    assert "常规库存业务暂不能过账" in page.content.decode()
    assert "库存金额" not in page.content.decode()
    for name, link in business["links"].items():
        params = parse_qs(urlsplit(link).query)
        assert params["warehouse"] == [str(source.pk)]
        linked = client.get(link)
        assert linked.status_code == 200
        assert {row.warehouse_id for row in linked.context["page_obj"]} == {source.pk}
        if name == "counts":
            assert params["count_domain"] == ["warehouse_stock"]
    task = client.get(business["freeze_task_url"])
    assert task.status_code == 200 and task.context["task"].pk == active.pk
    assert task.context["count_list_url"] == business["links"]["counts"]
    assert client.post(reverse("supplies:warehouse-detail", args=[source.pk]), {"name": "禁止写入"}).status_code == 405
    assert SupplyStockLedger.objects.count() == before_ledger and AuditLog.objects.count() == before_audit


def test_detail_uses_existing_view_roles_and_management_controls_remain_readonly(client, warehouse_context):
    _, _, source, _, _, _, _ = warehouse_context
    url = reverse("supplies:warehouse-detail", args=[source.pk])
    for role in ("system_admin", "finance", "warehouse", "equipment", "management"):
        client.force_login(make_user(f"warehouse-workspace-{role}", role))
        page = client.get(url)
        assert page.status_code == 200
        assert page.context["can_manage"] == (role in {"system_admin", "finance", "warehouse"})
        if role in {"equipment", "management"}:
            assert "编辑仓库" not in page.content.decode()
        assert page.context["warehouse_business"]["freeze_task"] is not None
    for role in ("hr", "employee", "department_manager"):
        client.force_login(make_user(f"warehouse-workspace-denied-{role}", role))
        assert client.get(url).status_code == 403


def test_hidden_task_details_do_not_leak_and_warehouse_scope_still_applies(client, warehouse_context, monkeypatch):
    _, _, source, target, _, active, _, = warehouse_context
    monkeypatch.setattr(warehouse_workspace, "scoped_supply_count_tasks", lambda actor, company, queryset: queryset.none())
    page = client.get(reverse("supplies:warehouse-detail", args=[source.pk]))
    assert page.context["warehouse"].count_frozen
    assert page.context["warehouse_business"]["freeze_task"] is None
    assert active.task_no not in page.content.decode() and active.name not in page.content.decode()
    assert "当前冻结任务不在可查看范围内" in page.content.decode()
    from apps.supplies import views
    original_scope = views.scoped_supply_warehouses
    monkeypatch.setattr(views, "scoped_supply_warehouses", lambda actor, company, queryset: original_scope(actor, company, queryset).filter(pk=source.pk))
    scoped = client.get(reverse("supplies:warehouse-list"))
    assert scoped.context["warehouse_summary"] == {"total": 1, "frozen": 1}
    assert client.get(reverse("supplies:warehouse-detail", args=[target.pk])).status_code == 404


def test_list_to_detail_preserves_filters_and_actual_page_two_and_foreign_company_is_hidden(client, warehouse_context):
    company = warehouse_context[0]
    for index in range(26):
        make_supply_warehouse(company, f"PAGING-{index:02}")
    page = client.get(reverse("supplies:warehouse-list"), {"q": "PAGING", "status": "all", "freeze": "unfrozen", "page": 2})
    assert page.context["page_obj"].number == 2 and len(page.context["warehouse_rows"]) == 1
    link = page.context["warehouse_rows"][0]["detail_url"]
    original = parse_qs(urlsplit(link).query)["return_to"][0]
    assert parse_qs(urlsplit(original).query) == {"q": ["PAGING"], "status": ["all"], "freeze": ["unfrozen"], "page": ["2"]}
    detail = client.get(link)
    assert detail.status_code == 200 and detail.context["warehouse_list_url"] == original
    foreign_company = Company.objects.create(code="FOREIGN-WAREHOUSE", name="另一公司", short_name="FW", is_active=False)
    foreign = make_supply_warehouse(foreign_company, "FOREIGN")
    assert client.get(reverse("supplies:warehouse-detail", args=[foreign.pk])).status_code == 404


def test_return_navigation_rejects_external_and_action_targets(client, warehouse_context):
    source = warehouse_context[2]
    for value in ("https://example.org/supplies/warehouses/", "//example.org/supplies/warehouses/",
        "/supplies/items/", "/supplies/warehouses/new/", "/supplies/warehouses/\\unsafe"):
        page = client.get(reverse("supplies:warehouse-detail", args=[source.pk]), {"return_to": value})
        assert page.context["warehouse_list_url"] == reverse("supplies:warehouse-list")
