"""Read-only policy lookup and navigation back to a fixed company policy list."""

from urllib.parse import parse_qsl, urlencode

from django.db.models import Count, Q
from django.urls import reverse

from apps.finance.models import DepreciationMethod, DepreciationPolicy


QUERY_FIELDS = ("q", "status", "method", "page_size", "page")


def _query_values(values):
    query = {}
    for key in QUERY_FIELDS:
        value = str(values.get(key, "") or "").strip()
        if not value:
            continue
        if key == "q":
            value = value[:200]
        elif key == "status" and value not in DepreciationPolicy.Status.values:
            continue
        elif key == "method" and value not in DepreciationMethod.values:
            continue
        elif key == "page_size" and value not in {"25", "50", "100", "200"}:
            continue
        elif key == "page" and (not value.isdecimal() or len(value) > 8 or int(value) < 1):
            continue
        query[key] = value
    return query


def policy_list_url(values):
    query = urlencode(_query_values(values))
    return reverse("finance:policy-list") + (f"?{query}" if query else "")


def policy_navigation(request):
    raw = request.POST.get("policy_query", request.GET.get("policy_query", ""))
    try:
        values = dict(parse_qsl(raw[:3000], max_num_fields=30))
    except ValueError:
        values = {}
    query = _query_values(values)
    return {"policy_query": urlencode(query), "policy_return_url": policy_list_url(query)}


def policy_followup_url(view, *, policy=None, policy_query=""):
    url = reverse(view, kwargs={"pk": policy.pk} if policy is not None else None)
    return url + ("?" + urlencode({"policy_query": policy_query}) if policy_query else "")


def filter_policies(queryset, data, *, ignore_status=False):
    if data.get("q"):
        queryset = queryset.filter(Q(policy_key__icontains=data["q"]) | Q(name__icontains=data["q"]))
    if data.get("method"):
        queryset = queryset.filter(method=data["method"])
    if not ignore_status and data.get("status"):
        queryset = queryset.filter(status=data["status"])
    return queryset


def policy_list_context(request, *, form, queryset, page):
    query = _query_values(form.cleaned_data if form.is_valid() else request.GET)
    query.pop("page", None)
    origin = {**query}
    if request.GET.get("page"):
        origin["page"] = str(page.number)
    encoded = urlencode(origin)
    context = {
        "policy_query": encoded,
        "pagination_query": urlencode(query),
        "policy_create_url": policy_followup_url("finance:policy-create", policy_query=encoded),
        "policy_status_cards": [],
        "policy_filter_summary": [],
        "has_policy_filters": any(request.GET.get(key) for key in ("q", "status", "method")),
    }
    if form.is_valid():
        counts = filter_policies(queryset, form.cleaned_data, ignore_status=True).aggregate(
            total=Count("pk"),
            **{status: Count("pk", filter=Q(status=status)) for status in DepreciationPolicy.Status.values},
        )
        for status, label in [("", "全部版本"), *DepreciationPolicy.Status.choices]:
            card_query = {key: value for key, value in query.items() if key != "status"}
            if status:
                card_query["status"] = status
            context["policy_status_cards"].append({
                "label": label, "count": counts[status or "total"], "url": policy_list_url(card_query),
                "active": (form.cleaned_data.get("status") or "") == status,
            })
        for key in ("q", "status", "method"):
            value = form.cleaned_data.get(key)
            if not value:
                continue
            label = dict(form.fields[key].choices).get(value, value) if key != "q" else value
            remaining = {name: item for name, item in query.items() if name != key}
            context["policy_filter_summary"].append({
                "label": f"{form.fields[key].label}：{label}", "remove_url": policy_list_url(remaining),
            })
    for policy in page.object_list:
        policy.detail_url = policy_followup_url("finance:policy-detail", policy=policy, policy_query=encoded)
    return context


def policy_detail_context(request, policy, *, can_manage, action_form):
    navigation = policy_navigation(request)
    return {
        "policy": policy, "can_manage": can_manage, "action_form": action_form, **navigation,
        "policy_edit_url": policy_followup_url(
            "finance:policy-edit", policy=policy, policy_query=navigation["policy_query"],
        ),
    }
