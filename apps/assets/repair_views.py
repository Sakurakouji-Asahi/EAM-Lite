"""A scoped workbench for assets that are currently under repair."""
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.assets.access import asset_company_for_request
from apps.assets.lifecycle_permissions import can_lifecycle_action
from apps.assets.models import AssetMovement
from apps.assets.repair_workspace import RepairFilter, can_open_repair_workspace, repair_context, repair_rows
from apps.core.pagination import paginate_query


@login_required
@never_cache
@require_GET
def repair_workbench(request):
    company = asset_company_for_request()
    if not can_open_repair_workspace(request.user):
        raise PermissionDenied("您没有查看维修实物资料的权限。")
    data = request.GET.copy()
    if not data.get("order"):
        data["order"] = "oldest"
    form = RepairFilter(data)
    rows = repair_rows(request.user, company)
    scope_count = rows.count()
    query, order = "", "oldest"
    if form.is_valid():
        query, order = form.cleaned_data["q"], form.cleaned_data["order"]
        if query:
            rows = rows.filter(
                Q(asset_code__icontains=query) | Q(equipment_number__icontains=query)
                | Q(asset_name__icontains=query) | Q(responsible_employee__name__icontains=query)
                | Q(responsible_employee__employee_no__icontains=query) | Q(repair_reason__icontains=query)
            )
    else:
        rows = rows.none()
    date_order = F("repair_started_at").desc(nulls_last=True) if order == "recent" else F("repair_started_at").asc(nulls_last=True)
    rows = rows.order_by(date_order, "asset_code", "pk")
    page, pagination_query = paginate_query(request, rows)
    starts = {movement.pk: movement for movement in AssetMovement.objects.filter(
        pk__in=[asset.repair_start_id for asset in page if asset.repair_start_id]
    )}
    for asset in page:
        asset.repair_context = repair_context(asset, start=starts.get(asset.repair_start_id))
        asset.can_complete_repair = can_lifecycle_action(request.user, asset, "repair_complete")
    return render(request, "assets/repair_workbench.html", {
        "form": form, "query": query, "order": order, "scope_count": scope_count,
        "page_obj": page, "pagination_query": pagination_query,
        "workbench_url": reverse("assets:repair-workbench"),
        "return_query": urlencode({"return_to": request.get_full_path()}),
    })
