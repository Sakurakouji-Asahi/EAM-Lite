from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.supplies.models import SupplyStockLedger
from apps.supplies.services import create_supply_document, post_supply_document
from apps.supplies.stock_navigation import stock_query_return
from tests.test_supply_stock_query_workspace import stock_query_context
from tests.test_sprint15_support import make_issue_document, make_user, make_employee, make_department, make_supply_item


pytestmark = pytest.mark.django_db


def test_stock_and_ledger_archive_links_preserve_the_actual_second_page_and_encoded_query(client, stock_query_context):
    company, actor, source, _, paper, _ = stock_query_context
    items = [make_supply_item(company, paper.category, f"NAV-{index:02d}", name=f"NAV+ & 资料 {index}") for index in range(26)]
    opening = create_supply_document(actor=actor, company=company, document_type="opening", data={
        "business_date": date(2026, 8, 26), "target_warehouse": source, "idempotency_key": "stock-navigation-page2"},
        lines=[{"item": item, "quantity": Decimal("1.2345"), "entered_unit_cost": Decimal("2")} for item in items])
    post_supply_document(actor=actor, document=opening)
    for route, label, filters in (("supplies:stock-balance-list", "返回库存查询", {"quantity_state": "available"}),
                                 ("supplies:stock-ledger-list", "返回出入库查询", {"direction": "incoming", "date_from": "2026-08-26"})):
        listing = client.get(reverse(route), {"q": "NAV+ &", "warehouse": str(source.pk), "page": 2, **filters})
        assert listing.context["page_obj"].number == 2 and len(listing.context["page_obj"]) == 1
        origin = listing.wsgi_request.get_full_path()
        row = listing.context["page_obj"][0]
        for related, path in (("item_detail_url", reverse("supplies:item-detail", args=[row.item_id])),
                              ("warehouse_detail_url", reverse("supplies:warehouse-detail", args=[row.warehouse_id]))):
            url = getattr(row, related)
            assert urlsplit(url).path == path and parse_qs(urlsplit(url).query)["return_to"] == [origin]
            detail = client.get(url)
            assert detail.status_code == 200 and detail.context["stock_return_url"] == origin
            assert label in detail.content.decode()
        if route == "supplies:stock-ledger-list":
            detail = client.get(row.document_detail_url)
            assert detail.status_code == 200 and detail.context["workflow_return_url"] == origin
            assert detail.context["stock_return_label"] == label


def test_balance_to_exact_ledger_to_document_keeps_original_query_across_filtering(client, stock_query_context):
    _, _, _, target, paper, _ = stock_query_context
    balance = client.get(reverse("supplies:stock-balance-list"), {"warehouse": str(target.pk), "item": paper.item_code,
        "quantity_state": "zero", "page": 2})
    origin = balance.wsgi_request.get_full_path()
    line_url = balance.context["page_obj"][0].ledger_url
    query = parse_qs(urlsplit(line_url).query)
    assert query["warehouse"] == [str(target.pk)] and query["item"] == [paper.item_code]
    assert query["return_to"] == [origin]
    filtered = client.get(reverse("supplies:stock-ledger-list"), {**{key:values[0] for key,values in query.items()}, "direction":"outgoing"})
    assert filtered.context["stock_return_url"] == origin
    assert 'name="return_to"' in filtered.content.decode()
    assert filtered.context["page_obj"].paginator.count == 1
    ledger_origin = filtered.wsgi_request.get_full_path()
    document = client.get(filtered.context["page_obj"][0].document_detail_url)
    assert document.context["workflow_return_url"] == ledger_origin and not document.context["return_is_document_list"]
    assert "返回出入库查询" in document.content.decode()
    returned = client.get(document.context["workflow_return_url"])
    assert returned.context["selected_direction"] == "outgoing" and returned.context["stock_return_url"] == origin


def test_stock_return_only_accepts_the_two_existing_local_read_only_routes(stock_query_context):
    actor = stock_query_context[1]
    factory = RequestFactory()
    for value in ("https://example.org/supplies/stock/", "//example.org/supplies/stock/", "/supplies/items/new/",
                  "/supplies/stock/\\example.org", "/supplies/stock/\n", "/supplies/stock/?q=" + "x"*3000):
        request = factory.get("/supplies/stock/", {"return_to": value})
        request.user = actor
        assert stock_query_return(request) == ""
    for route in ("supplies:stock-balance-list", "supplies:stock-ledger-list"):
        target = reverse(route) + "?q=Chair%2B%26&page=2"
        request = factory.get("/supplies/stock/", {"return_to": target})
        request.user = actor
        assert stock_query_return(request) == target


def test_stock_navigation_keeps_employee_access_and_existing_records_unchanged(client, stock_query_context):
    company, actor, source, _, paper, _ = stock_query_context
    user = make_user("stock-navigation-employee", "employee")
    department = make_department(company, "NAV-DEPT")
    employee = make_employee(company, department, "NAV-EMP", user=user)
    own = make_issue_document(actor=actor, company=company, warehouse=source, item=paper,
        department=department, employee=employee, quantity="1", key="stock-navigation-own")
    post_supply_document(actor=actor, document=own)
    before = list(SupplyStockLedger.objects.order_by("pk").values())
    client.force_login(user)
    target = reverse("supplies:stock-ledger-list") + "?item=" + paper.item_code
    detail = client.get(reverse("supplies:document-detail", args=[own.pk]), {"return_to": target})
    assert detail.status_code == 200 and not detail.context["show_cost"]
    assert not detail.context["stock_return_url"] and not detail.context["workflow_return_url"]
    for route in ("supplies:stock-balance-list", "supplies:stock-ledger-list"):
        assert client.get(reverse(route)).status_code == 403
    assert client.get(reverse("supplies:item-detail", args=[paper.pk]), {"return_to": target}).status_code == 403
    assert client.get(reverse("supplies:warehouse-detail", args=[source.pk]), {"return_to": target}).status_code == 403
    assert list(SupplyStockLedger.objects.order_by("pk").values()) == before
