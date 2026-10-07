"""Read-only warnings using the existing default-warehouse stock rule."""
from django.db.models import F, Q
from django.urls import reverse

from .domain import quantize_quantity


def low_stock_balance_q():
    return Q(item__is_active=True, item__minimum_stock_quantity__gt=0,
        item__default_warehouse_id=F("warehouse_id"),
        quantity_on_hand__lt=F("item__minimum_stock_quantity"))


def attach_stock_low_warnings(balances):
    for balance in balances:
        balance.is_low_stock = bool(balance.item.is_active and balance.item.minimum_stock_quantity > 0
            and balance.item.default_warehouse_id == balance.warehouse_id
            and balance.quantity_on_hand < balance.item.minimum_stock_quantity)
        balance.low_stock_shortage = (quantize_quantity(balance.item.minimum_stock_quantity - balance.quantity_on_hand)
            if balance.is_low_stock else None)


def stock_low_navigation(request):
    params = request.GET.copy()
    params.pop("page", None)
    params["quantity_state"] = "low"
    base = reverse("supplies:stock-balance-list")
    low_url = base + "?" + params.urlencode()
    params.pop("quantity_state", None)
    all_url = base + ("?" + params.urlencode() if params else "")
    return {"stock_low_url": low_url, "stock_low_remove_url": all_url,
        "stock_full_low_report_url": reverse("reports:supply-report-detail", args=["supply_low_stock"]) + "?low_stock_scope=formal"}
