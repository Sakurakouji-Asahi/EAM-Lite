"""Display-only filters and navigation for the pending finance workspace."""

from urllib.parse import parse_qsl, urlencode

from django.db.models import Count, Q
from django.urls import reverse

from apps.finance.readiness import filter_pending_finance_assets


QUERY_FIELDS = ("q", "department", "data_status", "import_batch", "page_size", "page")


def _query_values(values):
    query = {}
    for key in QUERY_FIELDS:
        value = str(values.get(key, "") or "").strip()
        if not value:
            continue
        if key == "q":
            value = value[:200]
        elif key in {"department", "import_batch"}:
            if len(value) > 64:
                continue
        elif key == "data_status" and value not in {"not_entered", "saved"}:
            continue
        elif key == "page_size" and value not in {"25", "50", "100", "200"}:
            continue
        elif key == "page" and (not value.isdecimal() or len(value) > 8):
            continue
        query[key] = value
    return query


def pending_list_url(values):
    query = urlencode(_query_values(values))
    return reverse("finance:pending-list") + (f"?{query}" if query else "")


def pending_navigation(request):
    """Accept query fields only, never a user-supplied redirect destination."""
    source = request.POST if request.method == "POST" else request.GET
    raw = source.get("pending_query", "")
    try:
        values = dict(parse_qsl(raw[:3000], max_num_fields=30))
    except ValueError:
        values = {}
    query = urlencode(_query_values(values))
    return {"pending_query": query, "pending_return_url": pending_list_url(values)}


def pending_followup_url(view_name, asset, pending_query):
    url = reverse(view_name, kwargs={"pk": asset.pk})
    return url + ("?" + urlencode({"pending_query": pending_query}) if pending_query else "")


def pending_list_context(request, *, form, queryset, query, page):
    navigation_query = {**query}
    if request.GET.get("page"):
        navigation_query["page"] = page.number
    context = {
        "pending_query": urlencode(_query_values(navigation_query)),
        "pending_filter_summary": [],
        "pending_status_cards": [],
        "has_pending_filters": any(request.GET.get(key) for key in ("q", "department", "data_status", "import_batch")),
    }
    if not form.is_valid():
        return context

    data = form.cleaned_data
    without_status = {**data, "data_status": ""}
    counts = filter_pending_finance_assets(queryset, without_status).aggregate(
        total=Count("pk"),
        not_entered=Count("pk", filter=Q(finance__isnull=True)),
        saved=Count("pk", filter=Q(finance__isnull=False)),
    )
    for value, label, count_key in (("", "全部待确认", "total"), ("not_entered", "尚未填写财务资料", "not_entered"), ("saved", "已保存财务资料", "saved")):
        card_query = {key: val for key, val in query.items() if key not in {"data_status", "page"}}
        card_query["data_status"] = value
        context["pending_status_cards"].append({
            "label": label, "count": counts[count_key], "url": pending_list_url(card_query),
            "active": (data.get("data_status") or "") == value,
        })

    for key in ("q", "department", "data_status", "import_batch"):
        value = data.get(key)
        if not value:
            continue
        label = str(value)
        if key == "data_status":
            label = dict(form.fields[key].choices)[value]
        remaining = {name: val for name, val in query.items() if name not in {key, "page"}}
        context["pending_filter_summary"].append({
            "label": f"{form.fields[key].label}：{label}",
            "remove_url": pending_list_url(remaining),
        })
    return context
