"""Server-rendered preventive-maintenance UI."""

from __future__ import annotations

from apps.core.multi_upload import upload_many
from django.contrib import messages
from apps.core.pagination import paginate_query
from apps.audit.models import AuditLog
from apps.maintenance.assignment import ProblemAssignmentForm, assign_problem
from apps.maintenance.domain import business_date
from django.db.models.functions import Coalesce
from django.contrib.auth.decorators import login_required
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, Q, When
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.encoding import escape_uri_path

from apps.assets.models import Asset
from apps.assets.models import AttachmentLink
from apps.maintenance.forms import (
    MaintenanceCompletionForm,
    MaintenanceAttachmentUploadForm,
    MaintenancePlanForm,
    MaintenanceProblemCloseForm,
    MaintenanceProblemFilterForm,
    MaintenanceRecordVoidForm,
    MaintenanceRecordFilterForm,
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
from apps.masterdata.models import InitializationSetting, Employee
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
            "company", "maintenance_plan", "asset", "completed_by", "voided_by",
            "problem__owner_employee", "problem__closed_by",
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
    from .workspaces import PlanQueryForm
    company = _company()
    plans = _plans(request.user,company)
    form = PlanQueryForm(request.GET,plans=plans)
    selected = form.apply(plans).order_by('next_maintenance_date','asset__asset_code','pk')
    page,query = _paginate(request,selected)
    return render(request,'maintenance/plan_list.html',{'plans':page,'page_obj':page,'pagination_query':query,
        'filter_form':form,'filters':{'q':request.GET.get('q',''),'status':request.GET.get('status','')},
        'can_manage':_can_manage_any_plan(request.user,company)},status=200 if form.is_valid() else 400)


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
    from .services import plan_date_preview
    plan = _plan(request, pk)
    require_manage_maintenance_plan(request.user, plan)
    form = MaintenancePlanForm(
        request.POST or None,
        actor=request.user,
        company=plan.company,
        instance=plan,
    )
    if request.method == "POST" and form.is_valid():
        if request.POST.get('action') == 'preview':
            try:
                dates = plan_date_preview(plan, **{key:form.cleaned_data[key] for key in ('cycle_value','cycle_unit','first_due_date')})
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                return render(request, 'maintenance/plan_form.html', {'form':form, 'title':'编辑保养计划',
                    'has_eligible_assets':True, 'editing':True, 'date_preview':dates})
            return render(request, 'maintenance/plan_form.html', {'form':form, 'title':'编辑保养计划',
                'has_eligible_assets':True, 'editing':True})
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
        {"form": form, "title": "编辑保养计划", "has_eligible_assets": True, "editing":True},
    )


@login_required
def plan_detail(request, pk):
    from .workspaces import PlanHistoryForm
    plan = _plan(request,pk)
    form = PlanHistoryForm(request.GET)
    records = form.apply(plan.records.select_related('completed_by','maintenance_plan','asset')).order_by('-completed_date','-created_at','pk')
    page,query = _paginate(request,records,per_page=form.cleaned_data.get('page_size') or 25)
    return render(request,'maintenance/plan_detail.html',{'plan':plan,'records':page,'page_obj':page,
        'pagination_query':query,'filter_form':form,'can_manage':can_manage_maintenance_plan(request.user,plan),
        'can_complete':plan.status == 'active' and can_complete_maintenance(request.user,plan)},status=200 if form.is_valid() else 400)


@login_required
def due_list(request):
    company = _company()
    plans = _plans(request.user, company).filter(status="active")
    from .workspaces import PlanQueryForm
    form = PlanQueryForm(request.GET,plans=plans,due=True)
    plans = form.apply(plans)
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
    if form.is_valid() and form.cleaned_data["due_scope"]:
        items = [item for item in items if item["due_status"] == form.cleaned_data["due_scope"]]
    page_obj, pagination_query = _paginate(request, items)
    return render(
        request,
        "maintenance/due_list.html",
        {
            "items": page_obj,
            "filter_form": form,
            "counts": counts,
            "page_obj": page_obj,
            "pagination_query": pagination_query,
        }, status=200 if form.is_valid() else 400,
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
                    upload_many(upload_maintenance_attachment,
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
    form = MaintenanceRecordFilterForm(request.GET)
    valid = form.is_valid()
    if valid:
        query = form.cleaned_data["q"]
        if query:
            records = records.filter(Q(asset__asset_code__icontains=query) | Q(asset__equipment_number__icontains=query)
                                     | Q(asset__asset_name__icontains=query) | Q(maintenance_plan__name__icontains=query))
        if form.cleaned_data["date_from"]:
            records = records.filter(completed_date__gte=form.cleaned_data["date_from"])
        if form.cleaned_data["date_to"]:
            records = records.filter(completed_date__lte=form.cleaned_data["date_to"])
        if form.cleaned_data["status"]:
            records = records.filter(status=form.cleaned_data["status"])
    else:
        records = records.none()
    page_obj, pagination_query = _paginate(
        request, records.order_by("-completed_date", "-created_at", "pk"), per_page=form.cleaned_data.get("page_size") or 25
    )
    return render(
        request,
        "maintenance/record_list.html",
        {
            "records": page_obj,
            "page_obj": page_obj,
            "pagination_query": pagination_query,
            "filter_form": form,
            "show_asset_columns": True,
        },
        status=200 if valid else 400,
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
    assignment_history = AuditLog.objects.filter(company=record.company,object_type="MaintenanceProblem",
        object_id=str(problem.pk),action="maintenance.problem_assigned").select_related("user").order_by("-created_at","pk") if problem else []
    assignment_page, assignment_query = paginate_query(request,assignment_history,parameter="assignment_page",per_page=10)
    return render(
        request,
        "maintenance/record_detail.html",
        {
            "record": record, "assignment_page":assignment_page, "assignment_query":assignment_query,
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
        "maintenance_record__maintenance_plan", "asset__department", "closed_by", "owner_employee"
    )
    form = MaintenanceProblemFilterForm(request.GET)
    form.fields["owner_employee"].queryset = Employee.objects.filter(company=_company(),
        owned_maintenance_problems__maintenance_record__maintenance_plan__in=plans).distinct().order_by("normalized_employee_no")
    today = business_date()
    valid = form.is_valid()
    if valid:
        query = form.cleaned_data["q"]
        if query:
            problems = problems.filter(
                Q(asset__asset_code__icontains=query)
                | Q(asset__equipment_number__icontains=query)
                | Q(asset__asset_name__icontains=query)
                | Q(maintenance_record__maintenance_plan__name__icontains=query)
                | Q(description__icontains=query)
                | Q(closure_note__icontains=query)
            )
        if form.cleaned_data["owner_employee"]:
            problems = problems.filter(owner_employee=form.cleaned_data["owner_employee"])
        if form.cleaned_data["mine"]:
            problems = problems.filter(owner_employee__user=request.user)
        if form.cleaned_data["due_scope"] == "overdue":
            problems = problems.filter(status="open",target_date__lt=today)
        elif form.cleaned_data["due_scope"] == "today":
            problems = problems.filter(status="open",target_date=today)
        elif form.cleaned_data["due_scope"] == "unassigned":
            problems = problems.filter(status="open").filter(Q(owner_employee__isnull=True)|Q(target_date__isnull=True))
        if form.cleaned_data["date_from"]:
            problems = problems.filter(maintenance_record__completed_date__gte=form.cleaned_data["date_from"])
        if form.cleaned_data["date_to"]:
            problems = problems.filter(maintenance_record__completed_date__lte=form.cleaned_data["date_to"])
    else:
        problems = problems.none()
    counts = problems.aggregate(
        open=Count("pk", filter=Q(status="open")),
        closed=Count("pk", filter=Q(status="closed")),
    )
    if valid and form.cleaned_data["status"]:
        problems = problems.filter(status=form.cleaned_data["status"])
    page_obj, pagination_query = _paginate(
        request,
        problems.order_by(Case(When(status="open", then=0), default=1), Coalesce("target_date","maintenance_record__completed_date"), "created_at", "pk"),
        per_page=form.cleaned_data.get("page_size") or 25,
    )
    summary_params = request.GET.copy()
    summary_params.pop("page", None)
    problem_links = {}
    for status in ("open", "closed"):
        summary_params["status"] = status
        problem_links[status] = "?" + summary_params.urlencode()
    items = [
        {"problem": problem, "is_overdue":problem.status == "open" and problem.target_date is not None and problem.target_date < today, "can_close": problem.status == "open" and can_close_maintenance_problem(request.user, problem)}
        for problem in page_obj.object_list
    ]
    return render(
        request,
        "maintenance/problem_list.html",
        {
            "items": items,
            "page_obj": page_obj,
            "pagination_query": pagination_query,
            "filter_form": form,
            "problem_counts": counts,
            "problem_links": problem_links,
        },
        status=200 if valid else 400,
    )


@login_required
def problem_assign(request, pk):
    problem = _problem(request,pk)
    require_close_maintenance_problem(request.user,problem)
    if problem.status != "open" or problem.maintenance_record.status != "confirmed":
        raise PermissionDenied("已关闭或来源已作废的问题不能分派。")
    form = ProblemAssignmentForm(request.POST or None,actor=request.user,problem=problem)
    if request.method == "POST" and form.is_valid():
        try:
            assign_problem(actor=request.user,problem=problem,request=request,**form.cleaned_data)
        except ValidationError as exc:
            _service_error(form,exc)
        else:
            messages.success(request,"负责人和目标日期已保存，原分派历史已保留。")
            return redirect("maintenance:record-detail",pk=problem.maintenance_record_id)
    return render(request,"maintenance/action_form.html",{"form":form,"title":"分派或调整问题跟进",
        "button_label":"保存分派","description":"登记负责人不改变账号权限；问题仍由设备管理员或授权部门主管核验后关闭。",
        "cancel_url":f"/maintenance/records/{problem.maintenance_record_id}/"})


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
            upload_many(upload_maintenance_attachment,
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
    response["Content-Disposition"] = "attachment; filename*=UTF-8''" + escape_uri_path(link.attachment.safe_filename)
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
