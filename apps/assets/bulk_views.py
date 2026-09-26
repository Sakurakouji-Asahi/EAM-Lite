import hashlib
import re
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.assets.access import asset_company_for_request
from apps.assets.bulk_forms import BulkDraftAssignmentForm, BulkRegistrationFilterForm
from apps.assets.bulk_registration import (
    confirm_bulk_registration, preview_bulk_registration, registration_candidates,
)
from apps.assets.bulk_support import MAX_BULK_ASSETS
from apps.assets.draft_assignment import confirm_draft_assignment, preview_draft_assignment
from apps.assets.permissions import can_create_asset_draft
from apps.finance.permissions import can_manage_finance
from apps.imports.services import require_import_permission
from apps.masterdata.hierarchy import descendant_ids
from apps.masterdata.location_tree import LocationTree
from apps.masterdata.models import Department, ImportBatch
from apps.masterdata.permissions import role_names_for


def _filtered_candidates(actor, company, data):
    # Resolve the entire filter before any POST action can write business data.
    queryset = registration_candidates(actor, company)
    query = data.get("q", "")
    if query:
        search = (
            Q(asset_name__icontains=query) | Q(model__icontains=query)
            | Q(equipment_number__icontains=query)
            | Q(responsible_employee__name__icontains=query)
        )
        draft = re.fullmatch(r"D-([0-9A-Fa-f]{1,8})", query)
        if draft:
            queryset = queryset.annotate(_draft_uuid=Cast("id", output_field=CharField()))
            search |= Q(_draft_uuid__istartswith=draft.group(1))
        queryset = queryset.filter(search)
    department = data.get("department")
    if department:
        queryset = queryset.filter(department_id__in=descendant_ids(
            Department, company=company, identifier=department.pk,
        ))
    if data.get("import_batch"):
        batch = ImportBatch.objects.filter(
            pk=data["import_batch"], company=company,
            import_type="asset_initialization", status="confirmed",
        ).first()
        if batch is None:
            raise PermissionDenied("该导入批次不可用。")
        require_import_permission(actor, "asset_initialization", company=company)
        queryset = queryset.filter(pk__in=[
            row.created_object_id for row in batch.rows.filter(created_object_type="Asset")
            if row.created_object_id
        ])
    return queryset.order_by("created_at", "pk")


@login_required
@require_http_methods(["GET", "POST"])
def bulk_registration(request):
    company = asset_company_for_request()
    if not (can_create_asset_draft(request.user, company)
            or "department_manager" in role_names_for(request.user)):
        raise PermissionDenied("您没有办理资产建档的权限。")

    filter_data = request.GET.copy()
    filter_data.setdefault("page_size", "100")
    filter_form = BulkRegistrationFilterForm(filter_data, actor=request.user, company=company)
    filters_valid = filter_form.is_valid()
    filters = filter_form.cleaned_data if filters_valid else {}
    queryset = (
        _filtered_candidates(request.user, company, filters)
        if filters_valid else registration_candidates(request.user, company).none()
    )
    department = filters.get("department")
    values = {
        "q": filters.get("q", ""),
        "department": str(department.pk) if department else "",
        "import_batch": str(filters.get("import_batch") or ""),
        "page_size": filters.get("page_size") or 100,
    }
    pagination_query = urlencode({key: value for key, value in values.items() if value})
    selection_material = urlencode({key: values[key] for key in ("q", "department", "import_batch")})
    selection_digest = hashlib.sha256(selection_material.encode()).hexdigest()

    assignment_action = request.method == "POST" and request.POST.get("action") == "assignment_preview"
    assignment_form = BulkDraftAssignmentForm(
        request.POST if assignment_action else None, actor=request.user, company=company,
    )
    context = {
        "preview": None, "result": None, "error": "", "assignment_preview": None,
        "assignment_result": None, "assignment_form": assignment_form, "cleared_asset_ids": [],
    }
    if request.method == "POST":
        try:
            if not filters_valid:
                raise ValidationError("筛选条件无效，请修正后重新操作。")
            action = request.POST.get("action")
            if action == "confirm":
                context["result"] = confirm_bulk_registration(
                    actor=request.user, company=company, token=request.POST.get("token", ""), request=request,
                )
                context["cleared_asset_ids"] = [
                    str(row["asset"].pk) for row in context["result"]["rows"]
                    if row["asset"] is not None and not row["error"]
                ]
            elif action == "preview":
                context["preview"] = preview_bulk_registration(
                    actor=request.user, company=company, asset_ids=request.POST.getlist("assets"),
                )
            elif assignment_action:
                if assignment_form.is_valid():
                    data = assignment_form.cleaned_data
                    context["assignment_preview"] = preview_draft_assignment(
                        actor=request.user, company=company,
                        asset_ids=request.POST.getlist("assets"), reason=data["reason"],
                        data={name: data[name] for name in ("department", "responsible_employee", "location")},
                    )
            elif action == "assignment_confirm":
                context["assignment_result"] = confirm_draft_assignment(
                    actor=request.user, company=company, token=request.POST.get("token", ""), request=request,
                )
                # Reviewing saved drafts never issues a code; confirmation is still explicit.
                try:
                    context["preview"] = preview_bulk_registration(
                        actor=request.user, company=company,
                        asset_ids=context["assignment_result"]["asset_ids"],
                    )
                except (PermissionDenied, ValidationError) as exc:
                    detail = "；".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                    context["error"] = "资料已保存，建档核对未完成：" + detail
            else:
                raise ValidationError("请选择核对或确认建档操作。")
        except ValidationError as exc:
            context["error"] = "；".join(exc.messages)

    page = Paginator(queryset, values["page_size"]).get_page(request.GET.get("page"))
    selected = set(request.POST.getlist("assets"))
    if context["assignment_result"]:
        selected.update(context["assignment_result"]["asset_ids"])
    locations = LocationTree(company)
    for asset in page:
        asset.bulk_selected = str(asset.pk) in selected
        asset.bulk_location_path = locations.path(asset.location_id)
    if context["preview"]:
        for row in context["preview"]["rows"]:
            row["asset"].bulk_location_path = locations.path(row["asset"].location_id)
    all_ids = [str(pk) for pk in queryset.values_list("pk", flat=True)[:MAX_BULK_ASSETS + 1]]
    selection_query = pagination_query + "&" + urlencode({"page": page.number})
    selection_url = reverse("assets:bulk-registration") + "?" + selection_query
    clear_url = reverse("assets:bulk-registration")
    if values["import_batch"]:
        clear_url += "?" + urlencode({"import_batch": values["import_batch"]})
    context.update({
        "page": page, "page_obj": page, "filter_form": filter_form,
        "query": values["q"], "import_batch": values["import_batch"],
        "pagination_query": pagination_query, "selection_url": selection_url, "clear_url": clear_url,
        "can_manage_finance": can_manage_finance(request.user),
        "selection_key": f"eam-bulk:v2:{company.pk}:{request.user.pk}:{selection_digest if filters_valid else 'invalid'}",
        "all_filtered_count": page.paginator.count,
        "all_filtered_ids": all_ids if len(all_ids) <= MAX_BULK_ASSETS else [],
        "can_select_filtered": 0 < len(all_ids) <= MAX_BULK_ASSETS,
    })
    response = render(request, "assets/bulk_registration.html", context)
    response["Cache-Control"] = "private, no-store"
    return response
