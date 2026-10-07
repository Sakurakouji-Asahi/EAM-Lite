"""Read-only query summaries and local navigation for supply count tasks."""
from urllib.parse import urlencode, urlsplit

from django.db.models import Count, Q
from django.http import QueryDict
from django.urls import reverse

from .models import SupplyCountStatus


def count_list_return(request):
    return safe_count_list_url(request.POST.get("return_to", request.GET.get("return_to", "")))


def safe_count_list_url(value):
    if not value or len(value) > 3000 or "\\" in value or any(ord(c) < 32 for c in value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme or parts.netloc or not value.startswith("/") or value.startswith("//"):
            return ""
        if parts.path == reverse("supplies:count-task-list"):
            return value
    except ValueError:
        pass
    return ""


def count_navigation(request, task=None):
    raw = request.POST.get("return_query", request.GET.get("return_query"))
    values = QueryDict(raw) if raw is not None else request.GET
    list_return = count_list_return(request) or safe_count_list_url(values.get("return_to", ""))
    params = QueryDict(mutable=True)
    for name in ("q", "row_view", "page_size", "page"):
        if name in values:
            params[name] = values[name]
    result_query = params.urlencode()
    if list_return:
        params["return_to"] = list_return
    detail_query = params.urlencode()
    action_params = {"return_query": result_query} if result_query else {}
    if list_return:
        action_params["return_to"] = list_return
    detail_url = reverse("supplies:count-task-detail", args=[task.pk]) if task else ""
    if detail_url and detail_query:
        detail_url += "?" + detail_query
    return {
        "count_list_return": list_return,
        "count_list_url": list_return or reverse("supplies:count-task-list"),
        "count_detail_url": detail_url,
        "count_result_query": result_query,
        "count_action_query": urlencode(action_params),
        "count_create_query": urlencode({"return_to": list_return}) if list_return else "",
        "count_query_reset_url": request.path + ("?" + urlencode({"return_to": list_return}) if list_return else ""),
    }


def count_status_summary(queryset, request, selected_status):
    counts = queryset.aggregate(total=Count("pk", distinct=True), **{
        status: Count("pk", filter=Q(status=status), distinct=True) for status in SupplyCountStatus.values
    })
    counts["open"] = counts["draft"] + counts["in_progress"] + counts["reconciliation"]
    links = []
    for status, label in (("", "全部"), ("open", "未关闭"), *SupplyCountStatus.choices):
        params = request.GET.copy()
        params.pop("page", None)
        if status:
            params["status"] = status
        else:
            params.pop("status", None)
        links.append({"label": label, "count": counts[status or "total"],
                      "selected": selected_status == status, "url": "?" + params.urlencode()})
    return counts, links


def count_filter_chip(request, name, label, value):
    params = request.GET.copy()
    params.pop("page", None)
    params.pop(name, None)
    return {"label": label, "value": value, "url": "?" + params.urlencode()}
