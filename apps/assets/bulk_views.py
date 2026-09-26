from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.assets.bulk_registration import registration_candidates, preview_bulk_registration, confirm_bulk_registration
from apps.assets.bulk_forms import BulkDraftAssignmentForm
from apps.assets.draft_assignment import preview_draft_assignment, confirm_draft_assignment
from apps.assets.permissions import can_create_asset_draft
from apps.assets.access import asset_company_for_request
from apps.masterdata.permissions import role_names_for
from apps.finance.permissions import can_manage_finance


@login_required
@require_http_methods(["GET", "POST"])
def bulk_registration(request):
    company = asset_company_for_request()
    if not (can_create_asset_draft(request.user, company) or "department_manager" in role_names_for(request.user)):
        raise PermissionDenied("您没有办理资产建档的权限。")
    assignment_action = request.method == "POST" and request.POST.get("action") == "assignment_preview"
    assignment_form = BulkDraftAssignmentForm(request.POST if assignment_action else None,
        actor=request.user, company=company)
    context = {"preview": None, "result": None, "error": "", "assignment_preview": None,
        "assignment_result": None, "assignment_form": assignment_form, "cleared_asset_ids": []}
    if request.method == "POST":
        try:
            if request.POST.get("action") == "confirm":
                context["result"] = confirm_bulk_registration(actor=request.user, company=company,
                    token=request.POST.get("token", ""), request=request)
                context["cleared_asset_ids"] = [str(row["asset"].pk) for row in context["result"]["rows"]
                    if row["asset"] is not None and not row["error"]]
            elif request.POST.get("action") == "preview":
                context["preview"] = preview_bulk_registration(actor=request.user, company=company,
                    asset_ids=request.POST.getlist("assets"))
            elif assignment_action:
                if assignment_form.is_valid():
                    data = assignment_form.cleaned_data
                    context["assignment_preview"] = preview_draft_assignment(actor=request.user, company=company,
                        asset_ids=request.POST.getlist("assets"), reason=data["reason"],
                        data={name: data[name] for name in ("department", "responsible_employee", "location")})
            elif request.POST.get("action") == "assignment_confirm":
                context["assignment_result"] = confirm_draft_assignment(actor=request.user, company=company,
                    token=request.POST.get("token", ""), request=request)
            else:
                raise ValidationError("请选择核对或确认建档操作。")
        except ValidationError as exc:
            context["error"] = "；".join(exc.messages)
    qs = registration_candidates(request.user, company)
    query = request.GET.get("q", "").strip()[:200]
    if query:
        qs = qs.filter(Q(asset_name__icontains=query) | Q(model__icontains=query) | Q(responsible_employee__name__icontains=query))
    batch_id = request.GET.get("import_batch", "")
    if batch_id:
        from apps.masterdata.models import ImportBatch
        from apps.imports.services import require_import_permission
        try:
            batch = ImportBatch.objects.get(pk=batch_id, company=company, import_type="asset_initialization", status="confirmed")
        except (ImportBatch.DoesNotExist, ValueError, ValidationError) as exc:
            raise PermissionDenied("该导入批次不可用。") from exc
        require_import_permission(request.user, "asset_initialization", company=company)
        qs = qs.filter(pk__in=[row.created_object_id for row in batch.rows.filter(created_object_type="Asset") if row.created_object_id])
    page = Paginator(qs.order_by("created_at", "pk"), 100).get_page(request.GET.get("page"))
    selected = set(request.POST.getlist("assets"))
    if context["assignment_result"]:
        selected.update(context["assignment_result"]["asset_ids"])
    for asset in page:
        asset.bulk_selected = str(asset.pk) in selected
    context.update({"page": page, "query": query, "import_batch": batch_id,"can_manage_finance":can_manage_finance(request.user),
        "selection_key": f"eam-bulk:{company.pk}:{request.user.pk}:{batch_id}"})
    response = render(request, "assets/bulk_registration.html", context)
    response["Cache-Control"] = "private, no-store"
    return response
