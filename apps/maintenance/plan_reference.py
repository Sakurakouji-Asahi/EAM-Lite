"""Explicit current-standard references for a separate, unsaved new plan."""
from urllib.parse import urlencode
from uuid import UUID

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse

from .plan_workspace import plan_navigation_context, plan_return_url


REFERENCE_FIELDS = ("name", "cycle_value", "cycle_unit", "advance_notice_days", "standard_content")


def reference_source(request, plans):
    value = request.POST.get("reference_plan", request.GET.get("reference_plan", ""))
    if not value:
        return None
    try:
        source_id = UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise Http404("参考保养计划不存在。")
    return get_object_or_404(plans, pk=source_id)


def reference_initial(source):
    return {name: getattr(source, name) for name in REFERENCE_FIELDS} if source else {}


def reference_create_url(request, plan):
    params = {"reference_plan": str(plan.pk)}
    return_to = plan_return_url(request)
    if return_to:
        params["return_to"] = return_to
    return reverse("maintenance:plan-create") + "?" + urlencode(params)


def reference_context(request, source):
    if source is None:
        return {}
    return {
        "reference_source": source,
        "reference_source_url": plan_navigation_context(request, source)["plan_detail_url"],
    }
