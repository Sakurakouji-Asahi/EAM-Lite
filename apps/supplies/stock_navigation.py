"""Read-only stock query context across related supply pages."""
from urllib.parse import urlencode, urlsplit

from django.urls import reverse

from .permissions import can_view_supply_master_data, can_view_supply_stock


def stock_query_return(request):
    value = request.POST.get("return_to", request.GET.get("return_to", ""))
    if not can_view_supply_stock(request.user) or not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return ""
    try:
        parts = urlsplit(value)
        paths = {reverse("supplies:stock-balance-list"), reverse("supplies:stock-ledger-list")}
        if not parts.scheme and not parts.netloc and value.startswith("/") and not value.startswith("//") and parts.path in paths:
            return value
    except ValueError:
        pass
    return ""


def stock_return_context(request):
    target = stock_query_return(request)
    label = "返回出入库查询" if target and urlsplit(target).path == reverse("supplies:stock-ledger-list") else "返回库存查询"
    return {"stock_return_url": target, "stock_return_label": label}


def stock_related_url(route, pk, return_to):
    return reverse(route, args=[pk]) + "?" + urlencode({"return_to": return_to})


def attach_stock_archive_links(actor, rows, return_to):
    archive_visible = can_view_supply_master_data(actor)
    for row in rows:
        row.item_detail_url = stock_related_url("supplies:item-detail", row.item_id, return_to) if archive_visible else ""
        row.warehouse_detail_url = stock_related_url("supplies:warehouse-detail", row.warehouse_id, return_to) if archive_visible else ""
        if getattr(row, "document_id", None):
            row.document_detail_url = stock_related_url("supplies:document-detail", row.document_id, return_to)
