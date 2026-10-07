from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.supplies import item_workspace
from apps.supplies.models import SupplyCustody, SupplyStockLedger
from apps.supplies.services import post_supply_document, return_custody_to_warehouse, transfer_custody
from tests.test_asset_list_return_navigation import Links
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_company, make_department, make_supply_category, make_supply_item,
    make_supply_warehouse, make_user, make_issue_document, seed_supply_stock,
)


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("role", ["system_admin", "finance", "warehouse", "equipment", "management"])
def test_item_detail_uses_existing_master_view_roles_and_never_writes(client, role):
    company, _, _, _, _, _, consumable, _ = supply_context()
    actor = make_user(f"item-detail-{role}", role)
    client.force_login(actor)
    before = AuditLog.objects.count()
    url = reverse("supplies:item-detail", args=[consumable.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert response.context["item"].company_id == company.pk
    assert response.context["can_manage_item"] == (role in {"system_admin", "finance", "warehouse"})
    assert response.context["stock_summary"]["quantity"] == Decimal("0.0000")
    assert response.context["custody_summary"] is None
    html = response.content.decode()
    assert "物品相关查询" in html
    assert "当前范围暂无该物品的库存余额" in html
    if role in {"equipment", "management"}:
        assert "编辑档案" not in html and reverse("supplies:item-deactivate", args=[consumable.pk]) not in html
    assert client.post(url, {"name": "不应写入"}).status_code == 405
    assert AuditLog.objects.count() == before
    assert not SupplyStockLedger.objects.exists()


@pytest.mark.parametrize("role", ["hr", "employee", "department_manager", None])
def test_relation_and_unapproved_roles_do_not_gain_company_item_archive_access(client, role):
    _, _, _, _, _, _, item, _ = supply_context()
    actor = make_user(f"item-detail-denied-{role}", *([role] if role else []))
    client.force_login(actor)
    response = client.get(reverse("supplies:item-detail", args=[item.pk]))
    assert response.status_code == 403


def test_durable_detail_quantities_are_separate_and_links_match_only_this_item(client):
    company, actor, department, employee, source, target, paper, durable = supply_context()
    for warehouse, quantity, key in ((source, "8.7654", "item-detail-stock-one"), (target, "2.0123", "item-detail-stock-two")):
        seed_supply_stock(actor=actor, company=company, warehouse=warehouse, item=durable,
            quantity=quantity, unit_cost="7", key=key)
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=paper,
        quantity="999", unit_cost="1", key="item-detail-other-unit")
    issued = make_issue_document(actor=actor, company=company, warehouse=source, item=durable,
        department=department, employee=employee, quantity="1.2345", key="item-detail-issue")
    post_supply_document(actor=actor, document=issued)
    closed_issue = make_issue_document(actor=actor, company=company, warehouse=source, item=durable,
        department=department, quantity="0.5", key="item-detail-closed-issue")
    post_supply_document(actor=actor, document=closed_issue)
    closed = SupplyCustody.objects.get(origin_issue_line=closed_issue.lines.get())
    returned = return_custody_to_warehouse(actor=actor, custody=closed, target_warehouse=source,
        quantity=Decimal("0.5"), business_date=closed.started_on, reason="全部归还",
        idempotency_key="item-detail-return")
    post_supply_document(actor=actor, document=returned)
    client.force_login(make_user("item-detail-readonly-summary", "management"))
    before = SupplyStockLedger.objects.count()
    page = client.get(reverse("supplies:item-detail", args=[durable.pk]))
    assert page.status_code == 200 and not page.context["can_manage_item"]
    assert page.context["stock_summary"] == {"quantity": Decimal("9.5432"), "warehouse_count": 2}
    assert page.context["custody_summary"] == {"quantity": Decimal("1.2345"), "count": 1}
    assert {row["balance"].warehouse_id for row in page.context["stock_rows"]} == {source.pk, target.pk}
    assert [row.pk for row in page.context["custody_rows"]] == [issued.lines.get().supply_custody.pk]
    for name, link in page.context["item_links"].items():
        query = parse_qs(urlsplit(link).query)
        assert query["item"] == [durable.item_code]
        linked = client.get(link)
        assert linked.status_code == 200, name
        if name == "issues":
            assert query["document_type"] == ["issue"] and query["status"] == ["posted"]
    for row in page.context["stock_rows"]:
        query = parse_qs(urlsplit(row["ledger_url"]).query)
        assert query == {"item": [durable.item_code], "warehouse": [str(row["balance"].warehouse_id)]}
    assert "999.0000" not in page.content.decode()
    assert "移动平均成本" not in page.content.decode()
    assert SupplyStockLedger.objects.count() == before


def test_activity_summaries_and_default_warehouse_follow_scoped_querysets(client, monkeypatch):
    company, actor, department, _, source, target, _, durable = supply_context()
    durable.default_warehouse = target
    durable.save(update_fields=["default_warehouse"])
    for index, warehouse in enumerate((source, target)):
        seed_supply_stock(actor=actor, company=company, warehouse=warehouse, item=durable,
            quantity="3", unit_cost="1", key=f"item-detail-scoped-stock-{index}")
    issue = make_issue_document(actor=actor, company=company, warehouse=source, item=durable,
        department=department, quantity="2", key="item-detail-scope-issue")
    post_supply_document(actor=actor, document=issue)
    root_custody = issue.lines.get().supply_custody
    other_department = make_department(company, "ITEMDETAIL-OUTSIDE")
    transfer_custody(actor=actor, custody=root_custody, target_department=other_department,
        target_employee=None, quantity=Decimal("0.5"), business_date=root_custody.started_on,
        reason="转交", idempotency_key="item-detail-scope-transfer")
    original_stock = item_workspace.scoped_supply_stock_balances
    original_custodies = item_workspace.scoped_supply_custodies
    original_warehouses = item_workspace.scoped_supply_warehouses
    monkeypatch.setattr(item_workspace, "scoped_supply_stock_balances",
        lambda user, selected_company: original_stock(user, selected_company).filter(warehouse=source))
    monkeypatch.setattr(item_workspace, "scoped_supply_custodies",
        lambda user, selected_company: original_custodies(user, selected_company).filter(department=department))
    monkeypatch.setattr(item_workspace, "scoped_supply_warehouses",
        lambda user, selected_company: original_warehouses(user, selected_company).filter(pk=source.pk))
    client.force_login(actor)
    page = client.get(reverse("supplies:item-detail", args=[durable.pk]))
    assert page.context["stock_summary"] == {"quantity": Decimal("1.0000"), "warehouse_count": 1}
    assert page.context["custody_summary"] == {"quantity": Decimal("1.5000"), "count": 1}
    assert page.context["item_default_warehouse"] is None
    html = page.content.decode()
    assert target.name not in html and other_department.name not in html


def test_list_detail_roundtrip_preserves_page_filters_and_inactive_archive(client):
    company, _, _, _, warehouse, _, first, _ = supply_context()
    query = '只读档案 & + / ? = % # "规格"'
    for index in range(26):
        make_supply_item(company, first.category, f"ARCHIVE-{index:02d}", name=query + str(index),
            item_type="durable_quantity", is_active=False, default_warehouse=warehouse,
            brand="演示品牌", model="MODEL-X", specification="25 × 30", remark="历史档案备注")
    client.force_login(make_user("item-detail-navigation", "management"))
    listing = client.get(reverse("supplies:item-list"), {"q": query, "category": str(first.category_id),
        "item_type": "durable_quantity", "status": "inactive", "page": 2})
    assert listing.status_code == 200 and listing.context["page_obj"].number == 2
    selected = listing.context["page_obj"].object_list[0]
    hrefs = Links(listing).hrefs_for_path(reverse("supplies:item-detail", args=[selected.pk]))
    assert len(hrefs) == 3
    target = listing.wsgi_request.get_full_path()
    for href in hrefs:
        assert parse_qs(urlsplit(href).query)["return_to"] == [target]
    detail = client.get(hrefs[0])
    assert detail.context["item_list_url"] == target
    html = detail.content.decode()
    for fact in ("演示品牌", "MODEL-X", "25 × 30", "历史档案备注", "档案已停用"):
        assert fact in html
    assert "编辑档案" not in html
    returned = client.get(detail.context["item_list_url"])
    assert returned.context["page_obj"].number == 2
    assert [row.pk for row in returned.context["page_obj"]] == [selected.pk]
    other = make_company("ITEM-OTHER", active=False)
    foreign = make_supply_item(other, make_supply_category(other), "FOREIGN-ITEM")
    assert client.get(reverse("supplies:item-detail", args=[foreign.pk])).status_code == 404


@pytest.mark.parametrize("target", ["https://example.invalid/supplies/items/", "//example.invalid/", "/accounts/logout/", "/supplies/items/new/", "/supplies/items/?q=bad\n"])
def test_item_return_only_allows_local_item_list(target):
    request = RequestFactory().get("/", {"return_to": target})
    assert item_workspace.item_list_return(request) == reverse("supplies:item-list")
