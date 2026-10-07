"""Read-only plan-list summaries and navigation back to a scoped query."""
from urllib.parse import urlencode, urlsplit

from django.db.models import Count, Q
from django.urls import reverse

from .models import MaintenancePlan


def plan_return_url(request):
    value = request.POST.get("return_to", request.GET.get("return_to", ""))
    if not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme or parts.netloc or parts.fragment or not value.startswith("/") or value.startswith("//"):
            return ""
        if parts.path == reverse("maintenance:plan-list"):
            return value
    except ValueError:
        pass
    return ""


def plan_navigation_context(request, plan=None):
    return_to = plan_return_url(request)
    query = urlencode({"return_to": return_to}) if return_to else ""
    context = {
        "plan_return_to": return_to,
        "plan_return_query": query,
        "plan_list_url": return_to or reverse("maintenance:plan-list"),
    }
    if plan is not None:
        detail = reverse("maintenance:plan-detail", args=[plan.pk])
        context["plan_detail_url"] = detail + ("?" + query if query else "")
    return context


def plan_list_presentation(request, form, plans):
    """The input already has access, keyword, department and owner restrictions."""
    statuses = (("", "全部", "secondary"), ("active", "启用", "success"),
                ("suspended", "暂停", "warning"), ("ended", "已终止", "secondary"))
    counts = plans.aggregate(total=Count("pk", distinct=True), **{
        value: Count("pk", filter=Q(status=value), distinct=True)
        for value, _label in MaintenancePlan.Status.choices
    })
    params = request.GET.copy()
    params.pop("page", None)
    selected_status = form.cleaned_data.get("status", "") if form.is_valid() else ""
    cards = []
    for value, label, tone in statuses:
        card_params = params.copy()
        if value:
            card_params["status"] = value
        else:
            card_params.pop("status", None)
        cards.append({"label": label, "count": counts[value or "total"], "tone": tone,
                      "active": value == selected_status,
                      "url": "?" + card_params.urlencode() if card_params else request.path})
    chips = []
    if form.is_valid():
        for name in ("q", "department", "responsible_employee", "status"):
            value = form.cleaned_data.get(name)
            if not value:
                continue
            display = str(value)
            if name == "status":
                display = dict(MaintenancePlan.Status.choices)[value]
            chip_params = params.copy()
            chip_params.pop(name, None)
            chips.append({"label": form.fields[name].label, "value": display,
                          "url": "?" + chip_params.urlencode() if chip_params else request.path})
    return {"plan_status_cards": cards, "plan_filter_chips": chips,
            "plan_return_query": urlencode({"return_to": request.get_full_path()})}
