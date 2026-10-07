"""Read-only item facts and activity within the existing supply scopes."""
from decimal import Decimal
from urllib.parse import urlencode, urlsplit

from django.db.models import Count, Sum
from django.urls import reverse

from .domain import quantize_quantity
from .models import SupplyItemType
from .permissions import (
    can_view_supply_custodies, can_view_supply_documents, can_view_supply_stock,
    scoped_supply_categories, scoped_supply_custodies, scoped_supply_stock_balances,
    scoped_supply_warehouses,
)


def item_list_return(request):
    fallback = reverse("supplies:item-list")
    value = request.POST.get("return_to", request.GET.get("return_to", ""))
    if not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return fallback
    try:
        parts = urlsplit(value)
        if not parts.scheme and not parts.netloc and value.startswith("/") and not value.startswith("//") and parts.path == fallback:
            return value
    except ValueError:
        pass
    return fallback


def item_activity(actor, item):
    company = item.company
    item_query = {"item": item.item_code}
    stock_visible = can_view_supply_stock(actor)
    custody_visible = item.item_type == SupplyItemType.DURABLE_QUANTITY and can_view_supply_custodies(actor)
    links = {}
    stock_rows = []
    stock_summary = None
    custody_rows = []
    custody_summary = None
    if stock_visible:
        balances = scoped_supply_stock_balances(actor, company).filter(item=item).select_related("warehouse")
        stock_summary = balances.aggregate(quantity=Sum("quantity_on_hand", default=Decimal("0.0000")),
                                           warehouse_count=Count("warehouse_id", distinct=True))
        stock_summary["quantity"] = quantize_quantity(stock_summary["quantity"])
        for balance in balances.order_by("warehouse__normalized_code", "pk")[:10]:
            stock_rows.append({"balance": balance, "ledger_url": reverse("supplies:stock-ledger-list") + "?" + urlencode({
                **item_query, "warehouse": str(balance.warehouse_id),
            })})
        links["stock"] = reverse("supplies:stock-balance-list") + "?" + urlencode(item_query)
        links["ledger"] = reverse("supplies:stock-ledger-list") + "?" + urlencode(item_query)
    if custody_visible:
        custodies = scoped_supply_custodies(actor, company).filter(item=item, status="open").select_related("department", "employee")
        custody_summary = custodies.aggregate(quantity=Sum("current_quantity", default=Decimal("0.0000")), count=Count("pk"))
        custody_summary["quantity"] = quantize_quantity(custody_summary["quantity"])
        custody_rows = list(custodies.order_by("-started_on", "-created_at", "pk")[:10])
        links["custodies"] = reverse("supplies:custody-list") + "?" + urlencode({**item_query, "status": "open"})
    if can_view_supply_documents(actor):
        links["documents"] = reverse("supplies:document-list") + "?" + urlencode(item_query)
        links["issues"] = reverse("supplies:document-list") + "?" + urlencode({**item_query, "document_type": "issue", "status": "posted"})
    return {
        "item_links": links, "stock_rows": stock_rows, "stock_summary": stock_summary,
        "custody_rows": custody_rows, "custody_summary": custody_summary,
        "item_category": scoped_supply_categories(actor, company).filter(pk=item.category_id).first(),
        "item_default_warehouse": scoped_supply_warehouses(actor, company).filter(pk=item.default_warehouse_id).first() if item.default_warehouse_id else None,
    }
