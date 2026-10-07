"""Read-only batch lookup, filter summaries and fixed-route navigation."""

import uuid
from calendar import monthrange
from urllib.parse import parse_qsl, urlencode

from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse

from apps.finance.models import DepreciationBatch, DepreciationBatchItem


LIST_FIELDS = ("q", "period", "status", "batch_type", "page_size", "page")
ITEM_FIELDS = ("q", "status", "page_size", "page")


def _values(values, *, items=False):
    query = {}
    for key in ITEM_FIELDS if items else LIST_FIELDS:
        value = str(values.get(key, "") or "").strip()
        if not value:
            continue
        if key == "q":
            value = value[:200]
        elif key == "period" and len(value) != 7:
            continue
        elif key == "status":
            allowed = {"ready", "error", "skipped"} if items else DepreciationBatch.Status.values
            if value not in allowed:
                continue
        elif key == "batch_type" and value not in DepreciationBatch.BatchType.values:
            continue
        elif key == "page_size" and value not in {"25", "50", "100", "200"}:
            continue
        elif key == "page" and (not value.isdecimal() or len(value) > 8):
            continue
        query[key] = value
    return query


def _url(name, query, *, pk=None):
    url = reverse(name, kwargs={"pk": pk} if pk is not None else None)
    encoded = urlencode(query)
    return url + (f"?{encoded}" if encoded else "")


def batch_list_navigation(request):
    raw = request.POST.get("batch_query", request.GET.get("batch_query", ""))
    try:
        values = dict(parse_qsl(raw[:3000], max_num_fields=30))
    except ValueError:
        values = {}
    query = _values(values)
    return {"batch_query": urlencode(query), "batch_return_url": _url("finance:batch-list", query)}


def batch_detail_url(batch, batch_query="", *, item_query=None, view="finance:batch-detail"):
    query = _values(item_query or {}, items=True)
    if batch_query:
        query["batch_query"] = batch_query
    return _url(view, query, pk=batch.pk)


def filter_batches(queryset, data, *, ignore_status=False):
    period = data.get("period")
    if period:
        month_end = period.replace(day=monthrange(period.year, period.month)[1])
        queryset = queryset.filter(period_start__lte=month_end, period_end__gt=period)
    if data.get("batch_type"):
        queryset = queryset.filter(batch_type=data["batch_type"])
    if not ignore_status and data.get("status"):
        queryset = queryset.filter(status=data["status"])
    if data.get("q"):
        search = data["q"]
        matching_items = DepreciationBatchItem.objects.filter(batch_id=OuterRef("pk")).filter(
            Q(asset__asset_code__icontains=search) | Q(asset__equipment_number__icontains=search)
            | Q(asset__asset_name__icontains=search)
        )
        queryset = queryset.alias(matches_asset=Exists(matching_items))
        matches = Q(matches_asset=True)
        try:
            matches |= Q(pk=uuid.UUID(search))
        except (ValueError, AttributeError):
            pass
        queryset = queryset.filter(matches)
    return queryset


def _summary(form, query, url_for, fields):
    summary = []
    for key in fields:
        value = form.cleaned_data.get(key)
        if not value:
            continue
        if key == "period":
            label = value.strftime("%Y-%m")
        elif key in {"status", "batch_type"}:
            label = dict(form.fields[key].choices)[value]
        else:
            label = str(value)
        remaining = {name: val for name, val in query.items() if name not in {key, "page"}}
        summary.append({"label": f"{form.fields[key].label}：{label}", "remove_url": url_for(remaining)})
    return summary


def batch_list_context(request, *, form, queryset, page):
    query = _values(request.GET)
    query.pop("page", None)
    navigation_query = {**query}
    if request.GET.get("page"):
        navigation_query["page"] = str(page.number)
    encoded = urlencode(navigation_query)
    context = {
        "batch_query": encoded, "batch_filter_summary": [], "batch_status_cards": [],
        "batch_generate_url": _url("finance:batch-generate", {"batch_query": encoded} if encoded else {}),
        "has_batch_filters": any(request.GET.get(key) for key in ("q", "period", "status", "batch_type")),
    }
    if form.is_valid():
        context["batch_filter_summary"] = _summary(form, query, lambda values: _url("finance:batch-list", values),
                                                  ("q", "period", "status", "batch_type"))
        counts = filter_batches(queryset, form.cleaned_data, ignore_status=True).aggregate(
            total=Count("pk"), **{status: Count("pk", filter=Q(status=status)) for status in DepreciationBatch.Status.values}
        )
        for status, label in [("", "全部批次"), *DepreciationBatch.Status.choices]:
            card_query = {key: value for key, value in query.items() if key != "status"}
            if status:
                card_query["status"] = status
            context["batch_status_cards"].append({"label": label, "count": counts[status or "total"],
                "url": _url("finance:batch-list", card_query), "active": (form.cleaned_data.get("status") or "") == status})
    for batch in page.object_list:
        batch.detail_url = batch_detail_url(batch, encoded)
        batch.errors_url = batch_detail_url(batch, encoded, item_query={"status": "error"})
    return context


def batch_detail_context(request, batch, *, form, counts):
    navigation = batch_list_navigation(request)
    query = _values(request.GET, items=True)
    query.pop("page", None)
    detail_query = _values(request.GET, items=True)
    url_for = lambda values: batch_detail_url(batch, navigation["batch_query"], item_query=values)
    context = {**navigation, "batch_filter_summary": [], "batch_item_cards": [],
        "batch_clear_items_url": url_for({}),
        "batch_confirm_url": batch_detail_url(batch, navigation["batch_query"], item_query=detail_query, view="finance:batch-confirm"),
        "batch_reverse_url": batch_detail_url(batch, navigation["batch_query"], view="finance:batch-reverse"),
    }
    if form.is_valid():
        context["batch_filter_summary"] = _summary(form, query, url_for, ("q", "status"))
        for status, label, count_key in (("", "全部明细", "count"), ("ready", "通过试算", "ready"),
                                       ("error", "错误", "errors"), ("skipped", "跳过", "skipped")):
            card_query = {key: value for key, value in query.items() if key != "status"}
            if status:
                card_query["status"] = status
            context["batch_item_cards"].append({"label": label, "count": counts[count_key], "url": url_for(card_query),
                "active": (form.cleaned_data.get("status") or "") == status})
    for field in ("reverses_batch", "supersedes_batch"):
        source = getattr(batch, field)
        if source is not None and source.company_id == batch.company_id:
            context[field + "_url"] = batch_detail_url(source, navigation["batch_query"])
    return context
