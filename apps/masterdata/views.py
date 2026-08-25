"""Server-rendered Sprint 1 master-data and setup views."""

from __future__ import annotations

from collections import defaultdict, deque

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.db.utils import OperationalError, ProgrammingError
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import ROLE_LABELS
from apps.masterdata.forms import (
    ApplicationUserCreateForm,
    AssetCategoryForm,
    AssetCodingSchemeForm,
    AssetCodingSegmentFormSet,
    CompanyForm,
    ConfirmStatusForm,
    DepartmentForm,
    EmployeeForm,
    EmployeeTechnicalLinkForm,
    LocationForm,
    ScopeAssignForm,
    ScopeRevokeForm,
    SystemSettingForm,
    UserRoleForm,
)
from apps.masterdata.models import (
    AssetCategory,
    AssetCodingScheme,
    AssetCodingSegment,
    Company,
    Department,
    Employee,
    InitializationSetting,
    Location,
    UserDepartmentScope,
)
from apps.masterdata.permissions import (
    assigned_role_names_for,
    can_access_setup,
    can_manage_masterdata,
    can_view_masterdata,
    current_company,
    is_login_capable,
    require_manage_masterdata,
    require_view_masterdata,
    role_names_for,
    scoped_departments,
    scoped_employees,
)
from apps.masterdata.services import (
    assign_department_scope,
    compute_initialization_progress,
    complete_initialization,
    create_application_user,
    create_asset_category,
    create_company,
    create_department,
    create_employee,
    create_location,
    get_system_setting,
    link_employee_user,
    refresh_initialization_progress,
    revoke_department_scope,
    set_employee_active,
    set_system_setting,
    set_user_roles,
    update_asset_category,
    update_company,
    update_department,
    update_employee,
    update_location,
)
from apps.coding.domain import preview_codes
from apps.coding.services import (
    activate_scheme,
    clone_scheme,
    create_scheme,
    replace_segments,
    retire_scheme,
    set_default_scheme,
    update_draft_scheme,
)


RESOURCE_CONFIG = {
    "company": {
        "label": "公司",
        "plural": "公司",
        "form": CompanyForm,
        "list_url": "masterdata:company-list",
    },
    "department": {
        "label": "部门",
        "plural": "部门",
        "form": DepartmentForm,
        "list_url": "masterdata:department-list",
    },
    "employee": {
        "label": "人员",
        "plural": "人员",
        "form": EmployeeForm,
        "list_url": "masterdata:employee-list",
    },
    "location": {
        "label": "位置",
        "plural": "位置",
        "form": LocationForm,
        "list_url": "masterdata:location-list",
    },
    "asset_category": {
        "label": "实物分类",
        "plural": "实物分类",
        "form": AssetCategoryForm,
        "list_url": "masterdata:category-list",
    },
}


def _company_or_404(*, include_inactive=False):
    company = current_company(include_inactive=include_inactive)
    if company is None:
        raise Http404("尚未配置公司")
    return company


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for error in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, error)


def _status_filter(queryset, request):
    selected = request.GET.get("status", "active")
    if selected == "inactive":
        return queryset.filter(is_active=False), selected
    if selected == "all":
        return queryset, selected
    return queryset.filter(is_active=True), "active"


def _tree_rows(objects, *, level_attr=None):
    objects = list(objects)
    children = {}
    for obj in objects:
        children.setdefault(obj.parent_id, []).append(obj)
    for values in children.values():
        values.sort(key=lambda item: (getattr(item, "normalized_code", ""), item.name))
    rows = []

    def walk(parent_id, depth, visited):
        for obj in children.get(parent_id, []):
            if obj.pk in visited:
                continue
            rows.append({"object": obj, "depth": depth})
            walk(obj.pk, depth + 1, {*visited, obj.pk})

    walk(None, 0, set())
    # Defensive fallback for corrupt legacy records; do not hide them.
    included = {row["object"].pk for row in rows}
    for obj in objects:
        if obj.pk not in included:
            rows.append(
                {
                    "object": obj,
                    "depth": max(getattr(obj, level_attr, 1) - 1, 0)
                    if level_attr
                    else 0,
                }
            )
    return rows


def _detail_rows(instance, fields):
    rows = []
    for field_name in fields:
        field = instance._meta.get_field(field_name)
        value = getattr(instance, field_name)
        display_method = getattr(instance, f"get_{field_name}_display", None)
        if callable(display_method):
            value = display_method()
        elif value is None:
            value = "—"
        elif isinstance(value, bool):
            value = "是" if value else "否"
        rows.append((field.verbose_name, value))
    return rows


def _department_scope_impact_preview(*, company, department_id, new_parent_id):
    departments = {
        department["id"]: department
        for department in Department.objects.filter(company=company).values(
            "id", "parent_id", "code", "name"
        )
    }
    current = departments[department_id]
    old_parent_id = current["parent_id"]

    children_before = defaultdict(list)
    for item in departments.values():
        children_before[item["parent_id"]].append(item["id"])
    children_after = defaultdict(list)
    for parent_id, child_ids in children_before.items():
        children_after[parent_id] = list(child_ids)
    children_after[old_parent_id].remove(department_id)
    children_after[new_parent_id].append(department_id)

    scopes_by_user = defaultdict(list)
    users = {}
    for scope in UserDepartmentScope.objects.filter(
        company=company, is_active=True
    ).select_related("user"):
        scopes_by_user[scope.user_id].append(
            (scope.department_id, scope.include_descendants)
        )
        users[scope.user_id] = scope.user

    def resolve(scopes, children):
        resolved = set()
        for root_id, include_descendants in scopes:
            resolved.add(root_id)
            if not include_descendants:
                continue
            queue = deque(children.get(root_id, ()))
            while queue:
                child_id = queue.popleft()
                if child_id in resolved:
                    continue
                resolved.add(child_id)
                queue.extend(children.get(child_id, ()))
        return resolved

    def labels(ids):
        return [
            f"{departments[pk]['name']}（{departments[pk]['code']}）"
            for pk in sorted(ids, key=lambda pk: departments[pk]["code"])
        ]

    rows = []
    confirmation_impacts = []
    for user_id, scopes in scopes_by_user.items():
        before = resolve(scopes, children_before)
        after = resolve(scopes, children_after)
        added = after - before
        removed = before - after
        if added or removed:
            confirmation_impacts.append(
                {
                    "user": user_id,
                    "added": sorted(added),
                    "removed": sorted(removed),
                }
            )
            rows.append(
                {
                    "user": users[user_id],
                    "added": labels(added),
                    "removed": labels(removed),
                }
            )
    rows.sort(key=lambda row: row["user"].username)

    def parent_label(parent_id):
        if parent_id is None:
            return "顶级部门"
        parent = departments[parent_id]
        return f"{parent['name']}（{parent['code']}）"

    return {
        "department": f"{current['name']}（{current['code']}）",
        "old_parent": parent_label(old_parent_id),
        "new_parent": parent_label(new_parent_id),
        "rows": rows,
        "confirmation_data": {
            "department": department_id,
            "old_parent": old_parent_id,
            "new_parent": new_parent_id,
            "impacts": sorted(
                confirmation_impacts, key=lambda impact: impact["user"]
            ),
        },
    }


@login_required
def company_list(request):
    require_view_masterdata(request.user, "company")
    company = current_company(include_inactive=True)
    queryset = Company.objects.filter(pk=company.pk) if company else Company.objects.none()
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(
            Q(code__icontains=q) | Q(name__icontains=q) | Q(short_name__icontains=q)
        )
    queryset, status = _status_filter(queryset, request)
    return render(
        request,
        "masterdata/company_list.html",
        {
            "objects": queryset,
            "q": q,
            "status": status,
            "has_company": Company.objects.exists(),
            "can_manage": can_manage_masterdata(request.user, "company"),
        },
    )


@login_required
def company_detail(request, pk):
    require_view_masterdata(request.user, "company")
    current = _company_or_404(include_inactive=True)
    company = get_object_or_404(Company.objects.filter(pk=current.pk), pk=pk)
    return render(
        request,
        "masterdata/detail.html",
        {
            "resource": "company",
            "resource_config": RESOURCE_CONFIG["company"],
            "object": company,
            "rows": _detail_rows(
                company,
                ("code", "name", "short_name", "currency", "timezone", "is_active"),
            ),
            "can_manage": can_manage_masterdata(request.user, "company"),
            "edit_url": reverse("masterdata:company-edit", args=[company.pk]),
            "status_url": reverse("masterdata:company-status", args=[company.pk]),
            "back_url": reverse("masterdata:company-list"),
        },
    )


@login_required
def company_create_view(request):
    require_manage_masterdata(request.user, "company")
    if Company.objects.exists():
        raise PermissionDenied("V1 已配置公司，不能创建第二个公司。")
    form = CompanyForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            company = create_company(
                actor=request.user, data=form.cleaned_data, request=request
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "公司已创建，初始化进度已保存。")
            return redirect("masterdata:company-detail", pk=company.pk)
    return render(
        request,
        "masterdata/form.html",
        {
            "form": form,
            "title": "新增公司",
            "cancel_url": reverse("masterdata:company-list"),
        },
    )


@login_required
def company_edit(request, pk):
    require_manage_masterdata(request.user, "company")
    current = _company_or_404(include_inactive=True)
    company = get_object_or_404(Company.objects.filter(pk=current.pk), pk=pk)
    form = CompanyForm(
        request.POST or None, instance=company, actor=request.user, company=company
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_company(
                actor=request.user,
                company=company,
                data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "公司资料已更新。")
            return redirect("masterdata:company-detail", pk=company.pk)
    return render(
        request,
        "masterdata/form.html",
        {
            "form": form,
            "title": "编辑公司",
            "cancel_url": reverse("masterdata:company-detail", args=[company.pk]),
        },
    )


@login_required
def department_list(request):
    require_view_masterdata(request.user, "department")
    company = _company_or_404()
    queryset = scoped_departments(
        request.user,
        company,
        Department.objects.select_related("parent", "manager_employee"),
    )
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(Q(code__icontains=q) | Q(name__icontains=q))
    queryset, status = _status_filter(queryset, request)
    return render(
        request,
        "masterdata/department_list.html",
        {
            "rows": _tree_rows(queryset),
            "q": q,
            "status": status,
            "company": company,
            "can_manage": can_manage_masterdata(request.user, "department"),
        },
    )


@login_required
def employee_list(request):
    require_view_masterdata(request.user, "employee")
    company = _company_or_404()
    queryset = scoped_employees(
        request.user,
        company,
        Employee.objects.select_related("department", "user"),
    )
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(
            Q(employee_no__icontains=q)
            | Q(name__icontains=q)
            | Q(mobile__icontains=q)
        )
    queryset, status = _status_filter(queryset, request)
    employment_status = request.GET.get("employment_status", "")
    if employment_status in {"active", "leaving", "resigned"}:
        queryset = queryset.filter(employment_status=employment_status)
    return render(
        request,
        "masterdata/employee_list.html",
        {
            "objects": queryset.order_by("normalized_employee_no"),
            "q": q,
            "status": status,
            "employment_status": employment_status,
            "company": company,
            "can_manage": can_manage_masterdata(request.user, "employee"),
            "show_user_link": can_manage_masterdata(
                request.user, "employee_user"
            ),
        },
    )


@login_required
def location_list(request):
    require_view_masterdata(request.user, "location")
    company = _company_or_404()
    queryset = Location.objects.filter(company=company).select_related("parent")
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(Q(code__icontains=q) | Q(name__icontains=q))
    queryset, status = _status_filter(queryset, request)
    return render(
        request,
        "masterdata/location_list.html",
        {
            "rows": _tree_rows(queryset, level_attr="level"),
            "q": q,
            "status": status,
            "company": company,
            "can_manage": can_manage_masterdata(request.user, "location"),
        },
    )


@login_required
def category_list(request):
    require_view_masterdata(request.user, "asset_category")
    company = _company_or_404()
    queryset = AssetCategory.objects.filter(company=company).select_related("parent")
    q = request.GET.get("q", "").strip()
    if q:
        queryset = queryset.filter(Q(code__icontains=q) | Q(name__icontains=q))
    queryset, status = _status_filter(queryset, request)
    return render(
        request,
        "masterdata/category_list.html",
        {
            "rows": _tree_rows(queryset, level_attr="category_level"),
            "q": q,
            "status": status,
            "company": company,
            "can_manage": can_manage_masterdata(request.user, "asset_category"),
        },
    )


def _segment_payload(formset):
    payload = []
    for item in formset.forms:
        if not item.cleaned_data or item.cleaned_data.get("DELETE"):
            continue
        segment_type = item.cleaned_data["segment_type"]
        payload.append({
            "sequence_order": item.cleaned_data["sequence_order"],
            "segment_type": segment_type,
            "fixed_value": item.cleaned_data.get("fixed_value") or None,
            "format_string": None,
            "sequence_length": item.cleaned_data.get("sequence_length"),
            "zero_pad": (
                item.cleaned_data.get("zero_pad")
                if segment_type == AssetCodingSegment.SegmentType.SEQUENCE
                else None
            ),
        })
    return payload


@login_required
def coding_scheme_list(request):
    require_view_masterdata(request.user, "coding_scheme")
    company = _company_or_404()
    schemes = AssetCodingScheme.objects.filter(company=company).prefetch_related(
        "segments"
    )
    return render(
        request,
        "masterdata/coding_scheme_list.html",
        {
            "schemes": schemes.order_by("scheme_key", "-version"),
            "can_manage": can_manage_masterdata(request.user, "coding_scheme"),
        },
    )


def _coding_form_context(*, request, scheme=None):
    company = _company_or_404()
    form = AssetCodingSchemeForm(
        request.POST or None,
        instance=scheme,
        actor=request.user,
        company=company,
    )
    formset = AssetCodingSegmentFormSet(
        request.POST or None,
        instance=scheme or AssetCodingScheme(company=company),
        prefix="segments",
    )
    return company, form, formset


@login_required
def coding_scheme_create(request):
    require_manage_masterdata(request.user, "coding_scheme")
    company, form, formset = _coding_form_context(request=request)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            scheme = create_scheme(
                actor=request.user,
                company=company,
                data=form.cleaned_data,
                segments=_segment_payload(formset),
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "编码方案草稿已创建；预览不会占用正式序号。")
            return redirect("masterdata:coding-scheme-detail", pk=scheme.pk)
    return render(
        request,
        "masterdata/coding_scheme_form.html",
        {"form": form, "formset": formset, "title": "新增编码方案草稿"},
    )


@login_required
def coding_scheme_edit(request, pk):
    require_manage_masterdata(request.user, "coding_scheme")
    company = _company_or_404()
    scheme = _company_object_or_404(AssetCodingScheme, pk, company)
    if scheme.status != AssetCodingScheme.Status.DRAFT:
        raise PermissionDenied("有效或历史版本不能原地编辑；请复制为新版本。")
    company, form, formset = _coding_form_context(request=request, scheme=scheme)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            with transaction.atomic():
                scheme = update_draft_scheme(
                    actor=request.user,
                    scheme=scheme,
                    data=form.cleaned_data,
                    request=request,
                )
                replace_segments(
                    actor=request.user,
                    scheme=scheme,
                    segments=_segment_payload(formset),
                    request=request,
                )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "编码方案草稿和片段已更新。")
            return redirect("masterdata:coding-scheme-detail", pk=scheme.pk)
    return render(
        request,
        "masterdata/coding_scheme_form.html",
        {"form": form, "formset": formset, "title": f"编辑 {scheme}"},
    )


def _preview_context(request, scheme):
    company = scheme.company
    category = AssetCategory.objects.filter(
        company=company, is_active=True
    ).order_by("category_level", "pk").last()
    department = Department.objects.filter(company=company, is_active=True).first()
    effective_date = scheme.effective_from or timezone.localdate()
    return {
        "company": company,
        "category": category,
        "department": department,
        "effective_date": effective_date,
    }


@login_required
def coding_scheme_detail(request, pk):
    require_view_masterdata(request.user, "coding_scheme")
    company = _company_or_404()
    scheme = get_object_or_404(
        AssetCodingScheme.objects.prefetch_related("segments"), pk=pk, company=company
    )
    example_count = 10 if request.GET.get("examples") == "10" else 1
    examples, preview_error = [], None
    try:
        examples = preview_codes(
            scheme, _preview_context(request, scheme), count=example_count
        )
    except ValidationError as exc:
        preview_error = "; ".join(exc.messages)
    return render(
        request,
        "masterdata/coding_scheme_detail.html",
        {
            "scheme": scheme,
            "segments": scheme.segments.order_by("sequence_order"),
            "examples": examples,
            "preview_error": preview_error,
            "can_manage": can_manage_masterdata(request.user, "coding_scheme"),
        },
    )


@login_required
def coding_scheme_action(request, pk, action):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    require_manage_masterdata(request.user, "coding_scheme")
    company = _company_or_404()
    scheme = _company_object_or_404(AssetCodingScheme, pk, company)
    try:
        if action == "activate":
            activate_scheme(actor=request.user, scheme=scheme, request=request)
            message = "编码方案已启用。"
        elif action == "retire":
            retire_scheme(actor=request.user, scheme=scheme, request=request)
            message = "编码方案已退役并保留历史。"
        elif action == "default":
            set_default_scheme(actor=request.user, scheme=scheme, request=request)
            message = "公司唯一默认编码方案已切换。"
        elif action == "clone":
            clone = clone_scheme(actor=request.user, scheme=scheme, request=request)
            messages.success(request, "已复制为新草稿版本。")
            return redirect("masterdata:coding-scheme-edit", pk=clone.pk)
        else:
            raise Http404("未知编码方案动作")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, message)
    return redirect("masterdata:coding-scheme-detail", pk=scheme.pk)


def _company_object_or_404(model, pk, company):
    return get_object_or_404(model, pk=pk, company=company)


@login_required
def department_detail(request, pk):
    require_view_masterdata(request.user, "department")
    company = _company_or_404()
    obj = get_object_or_404(scoped_departments(request.user, company), pk=pk)
    return _render_master_detail(
        request,
        "department",
        obj,
        ("code", "name", "parent", "manager_employee", "is_active"),
    )


@login_required
def employee_detail(request, pk):
    require_view_masterdata(request.user, "employee")
    company = _company_or_404()
    obj = get_object_or_404(scoped_employees(request.user, company), pk=pk)
    fields = [
        "employee_no",
        "name",
        "department",
        "employment_status",
        "hire_date",
        "termination_date",
        "mobile",
        "remark",
        "is_active",
    ]
    if can_manage_masterdata(request.user, "employee_user"):
        fields.append("user")
    clearance_url = None
    clearance_label = None
    try:
        from apps.offboarding.permissions import scoped_clearances

        active_clearance = scoped_clearances(request.user, company).filter(
            employee=obj, status__in=("open", "blocked")
        ).first()
        latest_clearance = (
            scoped_clearances(request.user, company)
            .filter(employee=obj)
            .order_by("-initiated_at")
            .first()
        )
        if active_clearance is not None:
            clearance_url = reverse(
                "offboarding:clearance-detail", args=[active_clearance.pk]
            )
            clearance_label = "查看活动清退单"
        elif latest_clearance is not None:
            clearance_url = reverse(
                "offboarding:clearance-detail", args=[latest_clearance.pk]
            )
            clearance_label = "查看清退历史"
        elif obj.employment_status == "active" and "hr" in role_names_for(request.user):
            clearance_url = (
                reverse("offboarding:clearance-initiate") + f"?employee={obj.pk}"
            )
            clearance_label = "发起离职清退"
    except (ImportError, OperationalError, ProgrammingError):
        clearance_url = None
    return _render_master_detail(
        request,
        "employee",
        obj,
        fields,
        technical_link_url=(
            reverse("masterdata:employee-user-link", args=[obj.pk])
            if can_manage_masterdata(request.user, "employee_user")
            else None
        ),
        clearance_url=clearance_url,
        clearance_label=clearance_label,
    )


@login_required
def location_detail(request, pk):
    require_view_masterdata(request.user, "location")
    company = _company_or_404()
    obj = _company_object_or_404(Location, pk, company)
    return _render_master_detail(
        request,
        "location",
        obj,
        ("code", "name", "parent", "level", "location_type", "is_active"),
    )


@login_required
def category_detail(request, pk):
    require_view_masterdata(request.user, "asset_category")
    company = _company_or_404()
    obj = _company_object_or_404(AssetCategory, pk, company)
    return _render_master_detail(
        request,
        "asset_category",
        obj,
        (
            "code",
            "name",
            "parent",
            "category_level",
            "category_type",
            "default_coding_scheme",
            "is_maintenance_required_default",
            "is_active",
        ),
    )


def _render_master_detail(
    request,
    resource,
    obj,
    fields,
    technical_link_url=None,
    clearance_url=None,
    clearance_label=None,
):
    slug = "category" if resource == "asset_category" else resource
    return render(
        request,
        "masterdata/detail.html",
        {
            "resource": resource,
            "resource_config": RESOURCE_CONFIG[resource],
            "object": obj,
            "rows": _detail_rows(obj, fields),
            "can_manage": can_manage_masterdata(request.user, resource),
            "can_change_status": can_manage_masterdata(request.user, resource)
            and not (
                resource == "employee"
                and obj.employment_status in {"leaving", "resigned"}
            ),
            "edit_url": reverse(f"masterdata:{slug}-edit", args=[obj.pk]),
            "status_url": reverse(f"masterdata:{slug}-status", args=[obj.pk]),
            "back_url": reverse(f"masterdata:{slug}-list"),
            "technical_link_url": technical_link_url,
            "clearance_url": clearance_url,
            "clearance_label": clearance_label,
        },
    )


def _master_form_view(
    request,
    *,
    resource,
    instance=None,
    create_service,
    update_service,
    update_kwarg,
):
    require_manage_masterdata(request.user, resource)
    company = _company_or_404()
    original_parent_id = (
        instance.parent_id
        if resource == "department" and instance is not None
        else None
    )
    department_scope_impact = None
    form_class = RESOURCE_CONFIG[resource]["form"]
    form = form_class(
        request.POST or None,
        instance=instance,
        actor=request.user,
        company=company,
    )
    if instance is None:
        form.instance.company = company
    if request.method == "POST" and form.is_valid():
        new_parent = form.cleaned_data.get("parent")
        parent_changed = (
            resource == "department"
            and instance is not None
            and getattr(new_parent, "pk", None) != original_parent_id
        )
        if parent_changed:
            department_scope_impact = _department_scope_impact_preview(
                company=company,
                department_id=instance.pk,
                new_parent_id=getattr(new_parent, "pk", None),
            )
            confirmed = False
            if request.POST.get("confirm_scope_impact") == "1":
                try:
                    confirmation_data = signing.loads(
                        request.POST.get("scope_impact_token", ""),
                        salt="masterdata.department-reparent",
                        max_age=900,
                    )
                except signing.BadSignature:
                    pass
                else:
                    confirmed = (
                        confirmation_data
                        == department_scope_impact["confirmation_data"]
                    )
                if not confirmed:
                    form.add_error(
                        None,
                        "部门树或授权范围已变化，请重新核对当前影响摘要后再确认。",
                    )
            department_scope_impact["confirmation_token"] = signing.dumps(
                department_scope_impact["confirmation_data"],
                salt="masterdata.department-reparent",
            )
        else:
            confirmed = True
        if confirmed:
            try:
                if instance is None:
                    obj = create_service(
                        actor=request.user,
                        company=company,
                        data=form.cleaned_data,
                        request=request,
                    )
                else:
                    obj = update_service(
                        actor=request.user,
                        **{update_kwarg: instance},
                        data=form.cleaned_data,
                        request=request,
                    )
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                messages.success(
                    request,
                    f"{RESOURCE_CONFIG[resource]['label']}资料已{'新增' if instance is None else '更新'}。",
                )
                if getattr(obj, "scope_impact", None):
                    messages.warning(
                        request,
                        f"部门改挂已使 {len(obj.scope_impact)} 名用户的授权部门集合发生变化，影响摘要已写入审计日志。",
                    )
                slug = "category" if resource == "asset_category" else resource
                return redirect(f"masterdata:{slug}-detail", pk=obj.pk)
    slug = "category" if resource == "asset_category" else resource
    return render(
        request,
        "masterdata/form.html",
        {
            "form": form,
            "title": f"{'新增' if instance is None else '编辑'}{RESOURCE_CONFIG[resource]['label']}",
            "cancel_url": reverse(
                f"masterdata:{slug}-detail", args=[instance.pk]
            )
            if instance
            else reverse(f"masterdata:{slug}-list"),
            "department_scope_impact": department_scope_impact,
        },
    )


@login_required
def department_create_view(request):
    return _master_form_view(
        request,
        resource="department",
        create_service=create_department,
        update_service=update_department,
        update_kwarg="department",
    )


@login_required
def department_edit(request, pk):
    company = _company_or_404()
    obj = _company_object_or_404(Department, pk, company)
    return _master_form_view(
        request,
        resource="department",
        instance=obj,
        create_service=create_department,
        update_service=update_department,
        update_kwarg="department",
    )


@login_required
def employee_create_view(request):
    return _master_form_view(
        request,
        resource="employee",
        create_service=create_employee,
        update_service=update_employee,
        update_kwarg="employee",
    )


@login_required
def employee_edit(request, pk):
    company = _company_or_404()
    obj = _company_object_or_404(Employee, pk, company)
    return _master_form_view(
        request,
        resource="employee",
        instance=obj,
        create_service=create_employee,
        update_service=update_employee,
        update_kwarg="employee",
    )


@login_required
def location_create_view(request):
    return _master_form_view(
        request,
        resource="location",
        create_service=create_location,
        update_service=update_location,
        update_kwarg="location",
    )


@login_required
def location_edit(request, pk):
    company = _company_or_404()
    obj = _company_object_or_404(Location, pk, company)
    return _master_form_view(
        request,
        resource="location",
        instance=obj,
        create_service=create_location,
        update_service=update_location,
        update_kwarg="location",
    )


@login_required
def category_create_view(request):
    return _master_form_view(
        request,
        resource="asset_category",
        create_service=create_asset_category,
        update_service=update_asset_category,
        update_kwarg="category",
    )


@login_required
def category_edit(request, pk):
    company = _company_or_404()
    obj = _company_object_or_404(AssetCategory, pk, company)
    return _master_form_view(
        request,
        resource="asset_category",
        instance=obj,
        create_service=create_asset_category,
        update_service=update_asset_category,
        update_kwarg="category",
    )


@login_required
def employee_user_link(request, pk):
    require_manage_masterdata(request.user, "employee_user")
    company = _company_or_404()
    employee = _company_object_or_404(Employee, pk, company)
    form = EmployeeTechnicalLinkForm(
        request.POST or None,
        instance=employee,
        actor=request.user,
        company=company,
    )
    if request.method == "POST" and form.is_valid():
        try:
            link_employee_user(
                actor=request.user,
                employee=employee,
                user=form.cleaned_data["user"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "人员登录账号技术关联已更新。")
            return redirect("masterdata:employee-detail", pk=employee.pk)
    return render(
        request,
        "masterdata/form.html",
        {
            "form": form,
            "title": "关联人员登录账号",
            "cancel_url": reverse("masterdata:employee-detail", args=[employee.pk]),
        },
    )


def _status_target(resource, pk):
    if resource == "company":
        current = _company_or_404(include_inactive=True)
        return get_object_or_404(Company.objects.filter(pk=current.pk), pk=pk), None
    company = _company_or_404()
    model = {
        "department": Department,
        "employee": Employee,
        "location": Location,
        "asset_category": AssetCategory,
    }[resource]
    return _company_object_or_404(model, pk, company), company


@login_required
def status_change(request, resource, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    require_manage_masterdata(request.user, resource)
    obj, _ = _status_target(resource, pk)
    form = ConfirmStatusForm(request.POST, actor=request.user, resource=resource)
    if not form.is_valid():
        messages.error(request, "请确认后再执行启用/停用操作。")
    else:
        new_status = not obj.is_active
        try:
            if resource == "company":
                update_company(
                    actor=request.user,
                    company=obj,
                    data={"is_active": new_status},
                    request=request,
                )
            elif resource == "department":
                update_department(
                    actor=request.user,
                    department=obj,
                    data={"is_active": new_status},
                    request=request,
                )
            elif resource == "employee":
                set_employee_active(
                    actor=request.user,
                    employee=obj,
                    is_active=new_status,
                    request=request,
                )
            elif resource == "location":
                update_location(
                    actor=request.user,
                    location=obj,
                    data={"is_active": new_status},
                    request=request,
                )
            else:
                update_asset_category(
                    actor=request.user,
                    category=obj,
                    data={"is_active": new_status},
                    request=request,
                )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, f"已{'启用' if new_status else '停用'}该记录。")
    slug = "category" if resource == "asset_category" else resource
    return redirect(f"masterdata:{slug}-detail", pk=obj.pk)


@login_required
def system_settings(request):
    require_view_masterdata(request.user, "system_setting")
    company = _company_or_404()
    can_manage = can_manage_masterdata(request.user, "system_setting")
    initial = {
        "attachment_allowed_extensions": get_system_setting(
            company=company, key="attachment_allowed_extensions"
        ),
        "attachment_max_size_bytes": get_system_setting(
            company=company, key="attachment_max_size_bytes"
        ),
    }
    form = None
    if can_manage:
        form = SystemSettingForm(
            request.POST or None, actor=request.user, initial=initial
        )
        if request.method == "POST" and form.is_valid():
            try:
                with transaction.atomic():
                    set_system_setting(
                        actor=request.user,
                        company=company,
                        key="attachment_allowed_extensions",
                        value=form.cleaned_data["attachment_allowed_extensions"],
                        request=request,
                    )
                    set_system_setting(
                        actor=request.user,
                        company=company,
                        key="attachment_max_size_bytes",
                        value=form.cleaned_data["attachment_max_size_bytes"],
                        request=request,
                    )
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                messages.success(request, "附件技术设置已保存。")
                return redirect("masterdata:system-settings")
    elif request.method == "POST":
        raise PermissionDenied("您只有系统技术设置只读权限。")
    return render(
        request,
        "masterdata/system_settings.html",
        {"form": form, "values": initial, "can_manage": can_manage},
    )


@login_required
def user_permissions_list(request):
    require_view_masterdata(request.user, "user_permissions")
    company = _company_or_404()
    User = get_user_model()
    users = list(
        User.objects.filter(is_superuser=False)
        .prefetch_related("groups")
        .order_by("username")
    )
    active_scopes = UserDepartmentScope.objects.filter(
        company=company, is_active=True
    ).select_related("department", "user")
    scopes_by_user = {}
    for scope in active_scopes:
        scopes_by_user.setdefault(scope.user_id, []).append(scope)
    rows = [
        {
            "user": user,
            "roles": [
                ROLE_LABELS[name] for name in sorted(assigned_role_names_for(user))
            ],
            "scopes": scopes_by_user.get(user.pk, []),
            "login_capable": is_login_capable(user),
        }
        for user in users
    ]
    return render(
        request,
        "masterdata/user_permissions_list.html",
        {"rows": rows, "company": company},
    )


@login_required
def user_create(request):
    require_manage_masterdata(request.user, "user_permissions")
    company = _company_or_404()
    form = ApplicationUserCreateForm(
        request.POST or None,
        actor=request.user,
        company=company,
    )
    if request.method == "POST" and form.is_valid():
        try:
            user = create_application_user(
                actor=request.user,
                company=company,
                username=form.cleaned_data["username"],
                display_name=form.cleaned_data["display_name"],
                email=form.cleaned_data["email"],
                mobile=form.cleaned_data["mobile"],
                password=form.cleaned_data["password"],
                roles=form.cleaned_data["roles"],
                initial_department=form.cleaned_data["initial_department"],
                include_descendants=form.cleaned_data["include_descendants"],
                reason=form.cleaned_data["reason"],
                current_password=form.cleaned_data["current_password"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, f"应用用户 {user.username} 已创建并完成角色配置。")
            return redirect("masterdata:user-permissions-detail", user_id=user.pk)
    return render(
        request,
        "masterdata/user_create.html",
        {"form": form, "company": company},
    )


@login_required
def user_permissions_detail(request, user_id):
    require_manage_masterdata(request.user, "user_permissions")
    company = _company_or_404()
    User = get_user_model()
    target = get_object_or_404(User, pk=user_id, is_superuser=False)
    assigned_role_names = sorted(assigned_role_names_for(target))
    role_form = UserRoleForm(
        actor=request.user,
        initial={"roles": assigned_role_names},
    )
    scope_form = ScopeAssignForm(actor=request.user, company=company)
    scopes = UserDepartmentScope.objects.filter(
        company=company, user=target, is_active=True
    ).select_related("department")
    return render(
        request,
        "masterdata/user_permissions_detail.html",
        {
            "target_user": target,
            "role_form": role_form,
            "scope_form": scope_form,
            "scopes": scopes,
            "assigned_roles": [ROLE_LABELS[name] for name in assigned_role_names],
            "finance_fields_visible": "finance" in assigned_role_names,
        },
    )


@login_required
def user_roles_update(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    require_manage_masterdata(request.user, "user_permissions")
    company = _company_or_404()
    target = get_object_or_404(get_user_model(), pk=user_id, is_superuser=False)
    form = UserRoleForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            set_user_roles(
                actor=request.user,
                company=company,
                user=target,
                roles=form.cleaned_data["roles"],
                reason=form.cleaned_data["reason"],
                current_password=form.cleaned_data["current_password"],
                request=request,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "固定角色已更新。")
    else:
        messages.error(request, "角色变更表单校验失败，请检查原因和身份确认。")
    return redirect("masterdata:user-permissions-detail", user_id=target.pk)


@login_required
def user_scope_assign(request, user_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    require_manage_masterdata(request.user, "user_permissions")
    company = _company_or_404()
    target = get_object_or_404(get_user_model(), pk=user_id, is_superuser=False)
    form = ScopeAssignForm(request.POST, actor=request.user, company=company)
    if form.is_valid():
        try:
            assign_department_scope(
                actor=request.user,
                company=company,
                user=target,
                department=form.cleaned_data["department"],
                include_descendants=form.cleaned_data["include_descendants"],
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "部门数据范围已分配。")
    else:
        messages.error(request, "部门范围表单校验失败。")
    return redirect("masterdata:user-permissions-detail", user_id=target.pk)


@login_required
def user_scope_revoke(request, user_id, scope_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    require_manage_masterdata(request.user, "user_permissions")
    company = _company_or_404()
    target = get_object_or_404(get_user_model(), pk=user_id, is_superuser=False)
    scope = get_object_or_404(
        UserDepartmentScope,
        pk=scope_id,
        company=company,
        user=target,
        is_active=True,
    )
    form = ScopeRevokeForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            revoke_department_scope(
                actor=request.user,
                scope=scope,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "部门数据范围已撤销并保留历史。")
    else:
        messages.error(request, "撤销范围必须填写原因。")
    return redirect("masterdata:user-permissions-detail", user_id=target.pk)


SETUP_STEPS = {
    1: {
        "name": "公司",
        "flag": "company_configured",
        "url": "masterdata:company-list",
        "writer": "system_admin",
    },
    2: {
        "name": "部门",
        "flag": "departments_configured",
        "url": "masterdata:department-list",
        "writer": "system_admin",
    },
    3: {
        "name": "人员",
        "flag": "employees_configured",
        "url": "masterdata:employee-list",
        "writer": "hr",
    },
    4: {
        "name": "实物分类",
        "flag": "categories_configured",
        "url": "masterdata:category-list",
        "writer": "system_admin / equipment",
    },
    5: {
        "name": "位置",
        "flag": "locations_configured",
        "url": "masterdata:location-list",
        "writer": "system_admin / equipment",
    },
    6: {
        "name": "编码规则",
        "flag": "coding_scheme_configured",
        "url": "masterdata:coding-scheme-list",
        "writer": "system_admin",
    },
    7: {
        "name": "折旧规则与财务参数",
        "flag": "finance_rules_configured",
        "url": "finance:policy-list",
        "writer": "finance",
    },
    8: {
        "name": "用户、角色及部门数据范围",
        "flag": "users_configured",
        "url": "masterdata:user-permissions-list",
        "writer": "system_admin",
    },
    9: {
        "name": "校验并完成",
        "flag": "initialization_completed",
        "url": "masterdata:setup",
        "writer": "system_admin",
    },
}


def _setup_context(company):
    User = get_user_model()
    progress = compute_initialization_progress(company)
    setting = InitializationSetting.objects.filter(company=company).first()
    application_users = list(
        User.objects.filter(is_superuser=False)
        .prefetch_related("groups")
        .order_by("username")
    )
    login_capable_users = [
        user for user in application_users
        if is_login_capable(user)
    ]
    has_login_admin = any(
        "system_admin" in role_names_for(user) for user in login_capable_users
    )
    has_login_finance = any(
        "finance" in role_names_for(user) for user in login_capable_users
    )
    managers_without_scope = [
        user
        for user in application_users
        if user.is_active
        and "department_manager" in assigned_role_names_for(user)
        and not UserDepartmentScope.objects.filter(
            company=company,
            user=user,
            is_active=True,
            department__is_active=True,
        ).exists()
    ]
    steps = []
    for number, definition in SETUP_STEPS.items():
        complete = (
            bool(setting and setting.initialization_completed)
            if number == 9
            else progress[definition["flag"]]
        )
        if number == 8:
            complete = progress["users_configured"] and progress["permissions_configured"]
        writer_names = [part.strip() for part in definition["writer"].split("/")]
        steps.append(
            {
                "number": number,
                **definition,
                "writer_label": " / ".join(
                    f"{ROLE_LABELS[name]}（{name}）" for name in writer_names
                ),
                "complete": complete,
            }
        )
    return {
        "company": company,
        "setting": setting,
        "steps": steps,
        "progress": progress,
        "has_login_admin": has_login_admin,
        "has_login_finance": has_login_finance,
        "managers_without_scope": managers_without_scope,
        "has_application_users": bool(application_users),
        "needs_bootstrap_guidance": (
            not application_users or not has_login_admin or not has_login_finance
        ),
    }


@login_required
def setup_overview(request):
    if not can_access_setup(request.user):
        raise PermissionDenied("普通用户不得进入初始化向导。")
    company = current_company(include_inactive=True)
    if company is None:
        if not can_manage_masterdata(request.user, "company"):
            raise PermissionDenied("尚未配置公司，请联系 system_admin。")
        return render(
            request,
            "masterdata/setup_overview.html",
            {"company": None, "steps": [], "progress": {}},
        )
    return render(request, "masterdata/setup_overview.html", _setup_context(company))


@login_required
def setup_step(request, step):
    if not can_access_setup(request.user):
        raise PermissionDenied("普通用户不得进入初始化向导。")
    if step not in SETUP_STEPS:
        raise Http404("该初始化步骤尚未实现")
    company = current_company(include_inactive=True)
    if company is None:
        return redirect("masterdata:setup")
    if request.method == "POST":
        if step not in {6, 7, 8, 9}:
            return HttpResponseNotAllowed(["GET"])
        try:
            if step == 6:
                require_manage_masterdata(request.user, "coding_scheme")
                refresh_initialization_progress(
                    company=company, actor=request.user, request=request
                )
            elif step == 7:
                from apps.finance.permissions import require_manage_finance

                require_manage_finance(request.user)
                refresh_initialization_progress(
                    company=company, actor=request.user, request=request
                )
            elif step == 8:
                require_manage_masterdata(request.user, "user_permissions")
                refresh_initialization_progress(
                    company=company, actor=request.user, request=request
                )
            else:
                complete_initialization(
                    actor=request.user, company=company, request=request
                )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(
                request,
                "九项真实条件已重新校验，初始化已原子完成。"
                if step == 9
                else f"步骤 {step} 已按当前真实数据重新校验并保存。",
            )
        return redirect("masterdata:setup-step", step=step)
    context = _setup_context(company)
    definition = next(item for item in context["steps"] if item["number"] == step)
    writer_roles = {part.strip() for part in definition["writer"].split("/")}
    context.update(
        {
            "step": definition,
            "has_writer_role": bool(role_names_for(request.user).intersection(writer_roles)),
            "can_view_step": {
                1: can_view_masterdata(request.user, "company"),
                2: can_view_masterdata(request.user, "department"),
                3: can_view_masterdata(request.user, "employee"),
                4: can_view_masterdata(request.user, "asset_category"),
                5: can_view_masterdata(request.user, "location"),
                6: can_view_masterdata(request.user, "coding_scheme"),
                7: bool(role_names_for(request.user).intersection({"finance", "system_admin"})),
                8: can_view_masterdata(request.user, "user_permissions"),
                9: "system_admin" in role_names_for(request.user),
            }[step],
            "bootstrap_hint": step in {3, 8},
            "show_bootstrap_guidance": (
                step == 8
                and "system_admin" in role_names_for(request.user)
                and context["needs_bootstrap_guidance"]
            ),
        }
    )
    return render(request, "masterdata/setup_step.html", context)
