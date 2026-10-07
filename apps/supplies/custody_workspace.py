"""Personal custody lookup and safe query continuity, without business writes."""
from urllib.parse import urlencode, urlsplit

from django.db.models import Count, Q, Sum
from django.urls import reverse

from .domain import quantize_quantity


def custody_query_return(request):
    fallback = reverse("supplies:custody-list")
    value = request.GET.get("return_to", "")
    if not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return fallback
    try:
        parts = urlsplit(value)
        if not parts.scheme and not parts.netloc and value.startswith("/") and not value.startswith("//") and parts.path in {fallback, reverse("supplies:my-custodies")}:
            return value
    except ValueError:
        pass
    return fallback


def custody_query_context(request):
    target = custody_query_return(request)
    return {"custody_list_url": target, "custody_navigation_query": urlencode({"return_to": target}),
        "custody_return_label": "返回我的保管" if urlsplit(target).path == reverse("supplies:my-custodies") else "返回保管清单"}


def attach_custody_query_links(records, return_to):
    query = urlencode({"return_to": return_to})
    for custody in records:
        custody.ui_detail_url = reverse("supplies:custody-detail", args=[custody.pk]) + "?" + query


def personal_custody_lookup(queryset, request):
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(Q(item__item_code__icontains=query) | Q(item__name__icontains=query)
            | Q(department__code__icontains=query) | Q(department__name__icontains=query)
            | Q(origin_issue_line__document__document_no__icontains=query))
    counts = queryset.aggregate(total=Count("pk"), open=Count("pk", filter=Q(status="open")), closed=Count("pk", filter=Q(status="closed")))
    quantities = list(queryset.filter(status="open").values("item_id", "item__item_code", "item__name", "item__unit")
        .annotate(quantity=Sum("current_quantity"), count=Count("pk")).order_by("item__normalized_item_code")[:10])
    for row in quantities:
        row["quantity"] = quantize_quantity(row["quantity"])
    status = request.GET.get("status", "open").strip()
    if status not in {"open", "closed", "all"}:
        status = "open"
    status_links = []
    for value, label, count in (("open", "在管", counts["open"]), ("closed", "已结清", counts["closed"]), ("all", "全部记录", counts["total"])):
        params = request.GET.copy()
        params.pop("page", None)
        params["status"] = value
        status_links.append({"value": value, "label": label, "count": count, "url": "?" + params.urlencode()})
    if status != "all":
        queryset = queryset.filter(status=status)
    return queryset, {"query": query, "selected_status": status, "custody_status_links": status_links,
        "custody_personal_counts": counts, "custody_open_quantities": quantities}
