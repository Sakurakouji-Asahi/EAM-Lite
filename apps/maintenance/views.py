"""Server-rendered preventive-maintenance UI."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from apps.assets.models import Asset
from apps.assets.models import AttachmentLink
from apps.maintenance.forms import (
    MaintenanceCompletionForm,
    MaintenanceAttachmentUploadForm,
    MaintenancePlanForm,
    MaintenanceProblemCloseForm,
    MaintenanceRecordVoidForm,
)
from apps.maintenance.models import (
    MaintenancePlan,
    MaintenanceProblem,
    MaintenanceRecord,
)
from apps.maintenance.permissions import (
    can_close_maintenance_problem,
    can_complete_maintenance,
    can_manage_maintenance_plan,
    can_manage_maintenance_attachment,
    can_view_maintenance_attachment,
    can_view_maintenance_plan,
    can_void_maintenance_record,
    require_close_maintenance_problem,
    require_complete_maintenance,
    require_manage_maintenance_plan,
    require_view_maintenance_plan,
    require_void_maintenance_record,
    scoped_maintenance_plans,
)
from apps.maintenance.services import (
    close_maintenance_problem,
    complete_maintenance,
    create_maintenance_plan,
    due_maintenance_plans,
    require_maintenance_attachment_download,
    set_maintenance_plan_status,
    update_maintenance_plan,
    upload_maintenance_attachment,
    void_maintenance_attachment,
    void_maintenance_record,
)
from apps.masterdata.models import InitializationSetting
from apps.masterdata.permissions import current_company


def _paginate(request, rows, *, per_page=25):
    page_obj = Paginator(rows, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page_obj, params.urlencode()


def _company():
    company = current_company()
    if company is None or not company.is_active:
        raise Http404("尚未配置启用公司。")
    if not InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists():
        raise PermissionDenied("系统初始化尚未完成，保养入口暂不可用。")
    return company


def _plans(user, company):
    return scoped_maintenance_plans(
        user,
        company,
        MaintenancePlan.objects.select_related(
            "company", "asset", "asset__department", "responsible_employee"
        ),
    )


def _plan(request, pk):
    return get_object_or_404(_plans(request.user, _company()), pk=pk)


def _record(request, pk):
    company = _company()
    record = get_object_or_404(
        MaintenanceRecord.objects.select_related(
            "company", "maintenance_plan", "asset", "completed_by", "voided_by"
        ),
        pk=pk,
        company=company,
    )
    require_view_maintenance_plan(request.user, record.maintenance_plan)
    return record


def _problem(request, pk):
    company = _company()
    problem = get_object_or_404(
        MaintenanceProblem.objects.select_related(
            "company",
            "asset__department",
            "maintenance_record__maintenance_plan",
        ),
        pk=pk,
        company=company,
    )
    require_view_maintenance_plan(
        request.user, problem.maintenance_record.maintenance_plan
    )
    return problem


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            for error in errors:
                form.add_error(field if field in form.fields else None, error)
    else:
        for error in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, error)


def _plan_payload(form):
    return {
        name: form.cleaned_data[name]
        for name in (
            "asset",
            "name",
            "cycle_value",
            "cycle_unit",
            "responsible_employee",
            "advance_notice_days",
            "standard_content",
            "first_due_date",
        )
        if name in form.cleaned_data
    }


class _MaintenanceAttachmentVoidForm(forms.Form):
    reason = forms.CharField(label="作废原因", max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认作废此保养证据")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].widget.attrs["class"] = "form-control"


def _can_manage_any_plan(user, company):
    return can_manage_maintenance_plan(user, Asset(company=company))


@login_required
def plan_list(request):
    company = _company()
    plans = _plans(request.user, company)
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    if query:
        plans = plans.filter(
            Q(name__icontains=query)
            | Q(asset__asset_code__icontains=query)
            | Q(asset__asset_name__icontains=query)
        )
    if status in MaintenancePlan.Status.values:
        plans = plans.filter(status=status)
    page_obj, pagination_query = _paginate(
        request, plans.order_by("next_maintenance_date", "asset__asset_code")
    )
    return render(
        request,
        "maintenance/plan_list.html",
        {
            "plans": page_obj,
            "page_obj": page_obj,
            "pagination_query": pagination_query,
            "status_choices": MaintenancePlan.Status.choices,
            "filters": {"q": query, "status": status},
            "can_manage": _can_manage_any_plan(request.user, company),
        },
    )


@login_required
def plan_create(request):
    company = _company()
    initial = {}
    if "asset" in request.GET:
        asset = get_object_or_404(Asset.objects.filter(company=company), pk=request.GET["asset"])
        require_manage_maintenance_plan(request.user, asset)
        initial["asset"] = asset.pk
    form = MaintenancePlanForm(
        request.POST or None, actor=request.user, company=company, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        try:
            plan = create_maintenance_plan(
                actor=request.user, company=company, request=request,
                **_plan_payload(form),
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "保养计划已创建。")
            return redirect("maintenance:plan-detail", pk=plan.pk)
    return render(
        request,
        "maintenance/plan_form.html",
        {
            "form": form,
            "title": "新建保养计划",
            "has_eligible_assets": form.fields["asset"].queryset.exists(),
        },
    )


@login_required
def plan_edit(request, pk):
    plan = _plan(request, pk)
    require_manage_maintenance_plan(request.user, plan)
    form = MaintenancePlanForm(
        request.POST or None,
        actor=request.user,
        company=plan.company,
        instance=plan,
    )
    if request.method == "POST" and form.is_valid():
        try:
            plan = update_maintenance_plan(
                actor=request.user,
                plan=plan,
                request=request,
                **{
                    key: value
                    for key, value in _plan_payload(form).items()
                    if key != "asset"
                },
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "保养计划已更新。")
            return redirect("maintenance:plan-detail", pk=plan.pk)
    return render(
        request,
        "maintenance/plan_form.html",
        {"form": form, "title": "编辑保养计划", "has_eligible_assets": True},
    )


@login_required
def plan_detail(request, pk):
    plan = _plan(request, pk)
    return render(
        request,
        "maintenance/plan_detail.html",
        {
            "plan": plan,
            "records": plan.records.select_related("completed_by").all(),
            "can_manage": can_manage_maintenance_plan(request.user, plan),
            "can_complete": plan.status == "active"
            and can_complete_maintenance(request.user, plan),
        },
    )


@login_required
def due_list(request):
    company = _company()
    plans = _plans(request.user, company).filter(status="active")
    items = []
    counts = {"upcoming": 0, "due_today": 0, "overdue": 0}
    labels = {"upcoming": "即将到期", "due_today": "今日到期", "overdue": "逾期"}
    for plan, status in due_maintenance_plans(
        request.user,
        company,
        queryset=plans,
    ):
        if status in counts:
            counts[status] += 1
            items.append(
                {
                    "plan": plan,
                    "due_status": status,
                    "due_label": labels[status],
                    "can_complete": can_complete_maintenance(request.user, plan),
                }
            )
    page_obj, pagination_query = _paginate(request, items)
    return render(
        request,
        "maintenance/due_list.html",
        {
            "items": page_obj,
            "counts": counts,
            "page_obj": page_obj,
            "pagination_query": pagination_query,
        },
    )


def _complete_view(request, plan, *, scheduled_date=None):
    require_complete_maintenance(request.user, plan)
    form = MaintenanceCompletionForm(
        request.POST or None,
        request.FILES or None,
        actor=request.user,
        plan=plan,
        initial={"scheduled_date": scheduled_date} if scheduled_date else None,
    )
    if scheduled_date and request.method != "POST":
        form.initial["scheduled_date"] = scheduled_date
    if request.method == "POST" and form.is_valid():
        try:
            completion_data = {
                key: value
                for key, value in form.cleaned_data.items()
                if key not in {"uploaded_file", "security_class"}
            }
            with transaction.atomic():
                record = complete_maintenance(
                    actor=request.user,
                    plan=plan,
                    request=request,
                    **completion_data,
                )
                if form.cleaned_data.get("uploaded_file"):
                    upload_maintenance_attachment(
                        actor=request.user,
                        target=record,
                        uploaded_file=form.cleaned_data["uploaded_file"],
                        security_class=form.cleaned_data["security_class"],
                        request=request,
                    )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "保养完成记录已保存。")
            return redirect("maintenance:record-detail", pk=record.pk)
    return render(
        request,
        "maintenance/action_form.html",
        {
            "form": form,
            "title": f"完成保养：{plan.name}",
            "button_label": "确认完成",
            "cancel_url": f"/maintenance/plans/{plan.pk}/",
        },
    )


@login_required
def plan_complete(request, pk):
    return _complete_view(request, _plan(request, pk))


@login_required
def plan_status(request, pk):
    plan = _plan(request, pk)
    require_manage_maintenance_plan(request.user, plan)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST":
        status = request.POST.get("status", "")
        reason = request.POST.get("reason", "").strip()
        try:
            set_maintenance_plan_status(
                actor=request.user, plan=plan, status=status,
                reason=reason, request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, "保养计划状态已更新。")
            return redirect("maintenance:plan-detail", pk=plan.pk)
    return render(request, "maintenance/plan_status.html", {"plan": plan})


@login_required
def record_list(request):
    plans = _plans(request.user, _company())
    records = MaintenanceRecord.objects.filter(
        maintenance_plan__in=plans
    ).select_related("maintenance_plan", "asset", "completed_by")
    page_obj, pagination_query = _paginate(
        request, records.order_by("-completed_date", "-created_at")
    )
    return render(
        request,
        "maintenance/record_list.html",
        {
            "records": page_obj,
            "page_obj": page_obj,
            "pagination_query": pagination_query,
        },
    )


@login_required
def record_detail(request, pk):
    record = _record(request, pk)
    problem = getattr(record, "problem", None)
    links = []
    for link in record.attachment_links.select_related("attachment", "created_by"):
        if can_view_maintenance_attachment(request.user, link):
            links.append(
                {
                    "link": link,
                    "can_void": can_manage_maintenance_attachment(
                        request.user, record, security_class=link.security_class
                    ),
                }
            )
    if problem is not None:
        for link in problem.attachment_links.select_related("attachment", "created_by"):
            if can_view_maintenance_attachment(request.user, link):
                links.append(
                    {
                        "link": link,
                        "can_void": can_manage_maintenance_attachment(
                            request.user, problem, security_class=link.security_class
                        ),
                    }
                )
    return render(
        request,
        "maintenance/record_detail.html",
        {
            "record": record,
            "problem": problem,
            "can_void": record.status == "confirmed"
            and can_void_maintenance_record(request.user, record),
            "can_redo": record.status == "voided"
            and can_complete_maintenance(request.user, record.maintenance_plan),
            "can_close_problem": problem is not None
            and problem.status == "open"
            and record.status == "confirmed"
            and can_close_maintenance_problem(request.user, problem),
            "attachments": links,
            "can_upload": can_manage_maintenance_attachment(
                request.user, record, security_class="A0"
            )
            or can_manage_maintenance_attachment(
                request.user, record, security_class="A1"
            ),
            "can_upload_problem": problem is not None
            and (
                can_manage_maintenance_attachment(
                    request.user, problem, security_class="A0"
                )
                or can_manage_maintenance_attachment(
                    request.user, problem, security_class="A1"
                )
            ),
        },
    )


@login_required
def record_void(request, pk):
    record = _record(request, pk)
    require_void_maintenance_record(request.user, record)
    form = MaintenanceRecordVoidForm(
        request.POST or None, actor=request.user, record=record
    )
    if request.method == "POST" and form.is_valid():
        try:
            void_maintenance_record(
                actor=request.user,
                record=record,
                reason=form.cleaned_data["reason"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "错误保养记录已作废，历史与证据保留。")
            return redirect("maintenance:record-detail", pk=record.pk)
    return render(
        request, "maintenance/action_form.html",
        {"form": form, "title": "作废保养记录", "button_label": "确认作废", "button_style": "danger", "description": "原记录、问题与附件仍永久保留。", "cancel_url": f"/maintenance/records/{record.pk}/"},
    )


@login_required
def record_redo(request, pk):
    record = _record(request, pk)
    if record.status != "voided":
        raise PermissionDenied("只能重新完成已作废的记录。")
    return _complete_view(
        request,
        record.maintenance_plan,
        scheduled_date=record.scheduled_date,
    )


@login_required
def problem_list(request):
    plans = _plans(request.user, _company())
    problems = MaintenanceProblem.objects.filter(
        maintenance_record__maintenance_plan__in=plans,
        maintenance_record__status="confirmed",
    ).select_related(
        "maintenance_record__maintenance_plan", "asset__department"
    )
    page_obj, pagination_query = _paginate(
        request, problems.order_by("status", "-created_at")
    )
    items = [
        {"problem": problem, "can_close": problem.status == "open" and can_close_maintenance_problem(request.user, problem)}
        for problem in page_obj.object_list
    ]
    return render(
        request,
        "maintenance/problem_list.html",
        {
            "items": items,
            "page_obj": page_obj,
            "pagination_query": pagination_query,
        },
    )


@login_required
def problem_close(request, pk):
    problem = _problem(request, pk)
    require_close_maintenance_problem(request.user, problem)
    form = MaintenanceProblemCloseForm(
        request.POST or None, actor=request.user, problem=problem
    )
    if request.method == "POST" and form.is_valid():
        try:
            close_maintenance_problem(
                actor=request.user,
                problem=problem,
                closure_note=form.cleaned_data["closure_note"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "保养问题已关闭。")
            return redirect("maintenance:record-detail", pk=problem.maintenance_record_id)
    return render(
        request, "maintenance/action_form.html",
        {"form": form, "title": "关闭保养问题", "button_label": "确认关闭", "cancel_url": f"/maintenance/records/{problem.maintenance_record_id}/"},
    )


def _attachment_target(request, target_type, pk):
    if target_type == "record":
        return _record(request, pk)
    if target_type == "problem":
        return _problem(request, pk)
    raise Http404("附件目标类型无效。")


@login_required
def attachment_upload(request, target_type, target_pk):
    target = _attachment_target(request, target_type, target_pk)
    if not (
        can_manage_maintenance_attachment(request.user, target, security_class="A0")
        or can_manage_maintenance_attachment(
            request.user, target, security_class="A1"
        )
    ):
        raise PermissionDenied("您没有上传此保养证据的权限。")
    form = MaintenanceAttachmentUploadForm(
        request.POST or None,
        request.FILES or None,
        actor=request.user,
        target=target,
    )
    if request.method == "POST" and form.is_valid():
        try:
            upload_maintenance_attachment(
                actor=request.user, target=target,
                uploaded_file=form.cleaned_data["uploaded_file"],
                security_class=form.cleaned_data["security_class"],
                request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "保养证据已上传。")
            record_id = target.pk if target_type == "record" else target.maintenance_record_id
            return redirect("maintenance:record-detail", pk=record_id)
    return render(
        request, "maintenance/action_form.html",
        {"form": form, "title": "上传保养证据", "button_label": "上传", "cancel_url": "/maintenance/records/"},
    )


@login_required
def attachment_download(request, pk):
    link = require_maintenance_attachment_download(actor=request.user, link=pk)
    try:
        handle = default_storage.open(link.attachment.storage_key, "rb")
    except OSError as exc:
        raise Http404("附件存储文件不可用。") from exc
    response = FileResponse(handle, content_type=link.attachment.mime_type)
    response["Content-Disposition"] = "attachment"
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def attachment_void(request, pk):
    link = get_object_or_404(AttachmentLink._base_manager, pk=pk)
    target = link.maintenance_record or link.maintenance_problem
    if target is None:
        raise Http404("不是保养附件。")
    _attachment_target(request, "record" if link.maintenance_record_id else "problem", target.pk)
    if not can_manage_maintenance_attachment(
        request.user, target, security_class=link.security_class
    ):
        raise PermissionDenied("您没有作废此保养证据的权限。")
    form = _MaintenanceAttachmentVoidForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            void_maintenance_attachment(
                actor=request.user, link=link,
                reason=form.cleaned_data["reason"], request=request,
            )
        except (PermissionDenied, ValidationError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "保养证据已作废。")
            record_id = target.pk if link.maintenance_record_id else target.maintenance_record_id
            return redirect("maintenance:record-detail", pk=record_id)
    return render(
        request, "maintenance/action_form.html",
        {"form": form, "title": "作废保养证据", "button_label": "确认作废", "button_style": "danger", "cancel_url": "/maintenance/records/"},
    )
