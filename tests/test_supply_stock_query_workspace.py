from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.supplies.models import SupplyStockBalance, SupplyStockLedger
from apps.supplies.services import create_supply_document, post_supply_document
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_issue_document,
    make_supply_item,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def stock_query_context(client):
    company, actor, department, employee, source, target, paper, chair = supply_context()
    for index, (warehouse, item, quantity, cost) in enumerate(
        ((source, paper, "10", "10"), (target, paper, "2", "10"), (source, chair, "3", "7"))
    ):
        seed_supply_stock(
            actor=actor, company=company, warehouse=warehouse, item=item,
            quantity=quantity, unit_cost=cost, key=f"stock-workspace-{index}",
        )
    issue = make_issue_document(
        actor=actor, company=company, warehouse=target, item=paper,
        department=department, quantity="2", key="stock-workspace-zero",
    )
    post_supply_document(document=issue, actor=actor)
    client.force_login(actor)
    return company, actor, source, target, paper, chair


def test_stock_summary_and_quantity_filters_cover_all_matching_rows(client, stock_query_context):
    response = client.get(reverse("supplies:stock-balance-list"))
    assert response.status_code == 200
    assert response.context["stock_summary"] == {
        "balance_count": 3, "item_count": 2, "warehouse_count": 2,
        "zero_count": 1, "low_count": 0, "amount": Decimal("121.00"),
    }
    available = client.get(reverse("supplies:stock-balance-list"), {"quantity_state": "available"})
    assert available.context["page_obj"].paginator.count == 2
    assert available.context["stock_summary"]["zero_count"] == 0
    zero = client.get(reverse("supplies:stock-balance-list"), {"quantity_state": "zero"})
    assert zero.context["stock_summary"]["balance_count"] == 1
    assert zero.context["stock_summary"]["amount"] == Decimal("0")
    assert zero.context["page_obj"][0].quantity_on_hand == Decimal("0")


def test_stock_summary_applies_item_mode_warehouse_and_normalized_code(client, stock_query_context):
    _, _, source, _, paper, chair = stock_query_context
    response = client.get(reverse("supplies:stock-balance-list"), {
        "warehouse": str(source.pk), "item_type": "durable_quantity",
        "item": f"  {chair.item_code.lower()}  ",
    })
    assert [row.item_id for row in response.context["page_obj"]] == [chair.pk]
    assert response.context["stock_summary"]["amount"] == Decimal("21")
    empty = client.get(reverse("supplies:stock-balance-list"), {
        "warehouse": str(source.pk), "item_type": "durable_quantity", "item": paper.item_code,
    })
    assert empty.context["stock_summary"]["balance_count"] == 0
    assert empty.context["stock_summary"]["amount"] == Decimal("0")


def test_each_stock_balance_link_opens_only_that_warehouse_and_item(client, stock_query_context):
    response = client.get(reverse("supplies:stock-balance-list"))
    for balance in response.context["page_obj"]:
        link = urlsplit(balance.ledger_url)
        assert parse_qs(link.query) == {
            "warehouse": [str(balance.warehouse_id)], "item": [balance.item.item_code],
            "return_to": [reverse("supplies:stock-balance-list")],
        }
        ledger = client.get(balance.ledger_url)
        assert ledger.status_code == 200
        assert ledger.context["page_obj"].paginator.count > 0
        assert all(row.warehouse_id == balance.warehouse_id and row.item_id == balance.item_id
                   for row in ledger.context["page_obj"])


def test_stock_and_ledger_navigation_preserve_shared_filters_and_encode_search(client, stock_query_context):
    _, _, source, _, _, _ = stock_query_context
    filters = {"q": "仓库 + & <", "warehouse": str(source.pk), "item_type": "consumable"}
    response = client.get(reverse("supplies:stock-balance-list"), {**filters, "page": "2", "quantity_state": "zero"})
    assert parse_qs(urlsplit(response.context["ledger_url"]).query) == {
        **{key: [value] for key, value in filters.items()},
        "return_to": [response.wsgi_request.get_full_path()],
    }
    ledger = client.get(response.context["ledger_url"])
    assert parse_qs(urlsplit(ledger.context["balance_url"]).query) == {
        key: [value] for key, value in filters.items()
    }


def test_ledger_direction_mode_and_warehouse_search_are_combined(client, stock_query_context):
    _, _, source, target, _, chair = stock_query_context
    incoming = client.get(reverse("supplies:stock-ledger-list"), {"direction": "incoming"})
    assert incoming.context["page_obj"].paginator.count == 3
    assert all(row.quantity_delta > 0 for row in incoming.context["page_obj"])
    outgoing = client.get(reverse("supplies:stock-ledger-list"), {"direction": "outgoing", "q": target.code})
    assert outgoing.context["page_obj"].paginator.count == 1
    assert outgoing.context["page_obj"][0].quantity_delta == Decimal("-2")
    durable = client.get(reverse("supplies:stock-ledger-list"), {
        "direction": "incoming", "item_type": "durable_quantity", "q": source.name,
    })
    assert [row.item_id for row in durable.context["page_obj"]] == [chair.pk]
    assert "业务日期 / 过账时间" in durable.content.decode()
    assert "2026-08-26" in durable.content.decode()
    invalid_dates = client.get(reverse("supplies:stock-ledger-list"), {
        "direction": "outgoing", "date_from": "invalid",
    })
    assert invalid_dates.status_code == 400
    assert invalid_dates.context["page_obj"].paginator.count == 0


def test_invalid_stock_filters_fail_closed_and_new_choices_normalize(client, stock_query_context):
    for route in ("supplies:stock-balance-list", "supplies:stock-ledger-list"):
        response = client.get(reverse(route), {"warehouse": "invalid"})
        assert response.context["page_obj"].paginator.count == 0
    response = client.get(reverse("supplies:stock-balance-list"), {
        "item_type": "invalid", "quantity_state": "invalid",
    })
    assert response.context["selected_item_type"] == ""
    assert response.context["selected_quantity_state"] == ""
    assert response.context["page_obj"].paginator.count == 3


def test_stock_summary_covers_more_than_one_page(client, stock_query_context):
    company, actor, source, _, paper, _ = stock_query_context
    items = [make_supply_item(company, paper.category, f"PAGE-{index:02d}") for index in range(26)]
    document = create_supply_document(
        actor=actor, company=company, document_type="opening",
        data={"business_date": date(2026, 8, 26), "target_warehouse": source, "idempotency_key": "stock-pages"},
        lines=[{"item": item, "quantity": Decimal("1"), "entered_unit_cost": Decimal("2")} for item in items],
    )
    post_supply_document(actor=actor, document=document)
    response = client.get(reverse("supplies:stock-balance-list"), {"q": "PAGE-", "page": "2"})
    assert len(response.context["page_obj"]) == 1
    assert response.context["stock_summary"]["balance_count"] == 26
    assert response.context["stock_summary"]["item_count"] == 26
    assert response.context["stock_summary"]["amount"] == Decimal("52")


def test_stock_query_workbench_is_read_only_and_requires_existing_permission(client, stock_query_context):
    before_balances = list(SupplyStockBalance.objects.order_by("pk").values())
    before_ledgers = list(SupplyStockLedger.objects.order_by("pk").values())
    for route in ("supplies:stock-balance-list", "supplies:stock-ledger-list"):
        assert client.get(reverse(route)).status_code == 200
    assert list(SupplyStockBalance.objects.order_by("pk").values()) == before_balances
    assert list(SupplyStockLedger.objects.order_by("pk").values()) == before_ledgers
    client.force_login(make_user("stock-workspace-employee", "employee"))
    for route in ("supplies:stock-balance-list", "supplies:stock-ledger-list"):
        assert client.get(reverse(route)).status_code == 403
