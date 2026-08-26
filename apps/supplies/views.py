from __future__ import annotations

from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.assets.permissions import can_create_asset_draft
from apps.masterdata.models import InitializationSetting
from apps.masterdata.permissions import current_company

from .forms import (
    SupplyCategoryForm,
    SupplyDeactivateForm,
    SupplyItemForm,
    SupplyWarehouseForm,
)
from .models import SupplyCategory, SupplyItem, SupplyItemType, SupplyWarehouse
from .permissions import (
    can_manage_supply_category,
    can_manage_supply_item,
    can_manage_supply_warehouse,
    require_manage_supply_category,
    require_manage_supply_item,
    require_manage_supply_warehouse,
    require_view_supply_master_data,
    scoped_supply_categories,
    scoped_supply_items,
    scoped_supply_warehouses,
)
from .services import (
    create_supply_category,
    create_supply_item,
    create_supply_warehouse,
    deactivate_supply_category,
    deactivate_supply_item,
    deactivate_supply_warehouse,
    update_supply_category,
    update_supply_item,
    update_supply_warehouse,
)


PAGE_SIZE = 25


def _company_or_404():
    company = current_company()
    if company is None:
        raise Http404("请先完成公司初始化。")
    return company


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
        return
    for error in getattr(exc, "messages", (str(exc),)):
        form.add_error(None, error)


def _status_filter(queryset, request):
    selected = request.GET.get("status", "active")
    if selected == "inactive":
        return queryset.filter(is_active=False), selected
    if selected == "all":
        return queryset, selected
    return queryset.filter(is_active=True), "active"


def _page(queryset, request):
    return Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))


@login_required
def dashboard(request):
    company = _company_or_404()
    require_view_supply_master_data(request.user)
    categories = scoped_supply_categories(request.user, company)
    warehouses = scoped_supply_warehouses(request.user, company)
    items = scoped_supply_items(request.user, company)
    return render(
        request,
        "supplies/dashboard.html",
        {
            "company": company,
            "category_count": categories.filter(is_active=True).count(),
            "warehouse_count": warehouses.filter(is_active=True).count(),
            "item_count": items.filter(is_active=True).count(),
            "consumable_count": items.filter(
                is_active=True, item_type=SupplyItemType.CONSUMABLE
            ).count(),
            "durable_count": items.filter(
                is_active=True, item_type=SupplyItemType.DURABLE_QUANTITY
            ).count(),
            "can_manage_categories": can_manage_supply_category(request.user),
            "can_manage_warehouses": can_manage_supply_warehouse(request.user),
            "can_manage_items": can_manage_supply_item(
                request.user, SupplyItemType.DURABLE_QUANTITY
            ),
            "can_create_individual_asset": can_create_asset_draft(
                request.user, company
            )
            and InitializationSetting.objects.filter(
                company=company, initialization_completed=True
            ).exists(),
        },
    )


@login_required
def category_list(request):
    company = _company_or_404()
    require_view_supply_master_data(request.user)
    queryset = scoped_supply_categories(
        request.user,
        company,
        SupplyCategory.objects.select_related("parent", "updated_by"),
    )
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(Q(code__icontains=query) | Q(name__icontains=query))
    queryset, selected_status = _status_filter(queryset, request)
    return render(
        request,
        "supplies/category_list.html",
        {
            "page_obj": _page(queryset.order_by("normalized_code"), request),
            "query": query,
            "selected_status": selected_status,
            "can_manage": can_manage_supply_category(request.user),
        },
    )


@login_required
def category_create(request):
    company = _company_or_404()
    require_manage_supply_category(request.user)
    form = SupplyCategoryForm(
        request.POST or None, actor=request.user, company=company
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_supply_category(
                actor=request.user,
                company=company,
                data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "低值物品分类已新增。")
            return redirect("supplies:category-list")
    return render(
        request,
        "supplies/category_form.html",
        {"form": form, "title": "新增低值物品分类"},
    )


@login_required
def category_edit(request, pk):
    company = _company_or_404()
    require_manage_supply_category(request.user)
    category = get_object_or_404(
        scoped_supply_categories(request.user, company), pk=pk
    )
    form = SupplyCategoryForm(
        request.POST or None,
        instance=category,
        actor=request.user,
        company=company,
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_supply_category(
                actor=request.user,
                category=category,
                data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "低值物品分类已更新。")
            return redirect("supplies:category-list")
    return render(
        request,
        "supplies/category_form.html",
        {"form": form, "title": "编辑低值物品分类", "object": category},
    )


@login_required
def category_deactivate(request, pk):
    company = _company_or_404()
    require_manage_supply_category(request.user)
    category = get_object_or_404(
        scoped_supply_categories(request.user, company), pk=pk
    )
    form = SupplyDeactivateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        deactivate_supply_category(
            actor=request.user,
            category=category,
            reason=form.cleaned_data["reason"],
            request=request,
        )
        messages.success(request, "低值物品分类已停用，历史引用仍会保留。")
        return redirect("supplies:category-list")
    return render(
        request,
        "supplies/deactivate_confirm.html",
        {
            "form": form,
            "title": "停用低值物品分类",
            "object": category,
            "cancel_url": "supplies:category-list",
        },
    )


@login_required
def warehouse_list(request):
    company = _company_or_404()
    require_view_supply_master_data(request.user)
    queryset = scoped_supply_warehouses(
        request.user,
        company,
        SupplyWarehouse.objects.select_related(
            "location", "manager_employee", "manager_employee__department"
        ),
    )
    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(Q(code__icontains=query) | Q(name__icontains=query))
    queryset, selected_status = _status_filter(queryset, request)
    return render(
        request,
        "supplies/warehouse_list.html",
        {
            "page_obj": _page(queryset.order_by("normalized_code"), request),
            "query": query,
            "selected_status": selected_status,
            "can_manage": can_manage_supply_warehouse(request.user),
        },
    )


@login_required
def warehouse_create(request):
    company = _company_or_404()
    require_manage_supply_warehouse(request.user)
    form = SupplyWarehouseForm(
        request.POST or None, actor=request.user, company=company
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_supply_warehouse(
                actor=request.user,
                company=company,
                data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "低值物品仓库已新增。")
            return redirect("supplies:warehouse-list")
    return render(
        request,
        "supplies/warehouse_form.html",
        {"form": form, "title": "新增低值物品仓库"},
    )


@login_required
def warehouse_edit(request, pk):
    company = _company_or_404()
    require_manage_supply_warehouse(request.user)
    warehouse = get_object_or_404(
        scoped_supply_warehouses(request.user, company), pk=pk
    )
    form = SupplyWarehouseForm(
        request.POST or None,
        instance=warehouse,
        actor=request.user,
        company=company,
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_supply_warehouse(
                actor=request.user,
                warehouse=warehouse,
                data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "低值物品仓库已更新。")
            return redirect("supplies:warehouse-list")
    return render(
        request,
        "supplies/warehouse_form.html",
        {"form": form, "title": "编辑低值物品仓库", "object": warehouse},
    )


@login_required
def warehouse_deactivate(request, pk):
    company = _company_or_404()
    require_manage_supply_warehouse(request.user)
    warehouse = get_object_or_404(
        scoped_supply_warehouses(request.user, company), pk=pk
    )
    form = SupplyDeactivateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        deactivate_supply_warehouse(
            actor=request.user,
            warehouse=warehouse,
            reason=form.cleaned_data["reason"],
            request=request,
        )
        messages.success(request, "低值物品仓库已停用，历史引用仍会保留。")
        return redirect("supplies:warehouse-list")
    return render(
        request,
        "supplies/deactivate_confirm.html",
        {
            "form": form,
            "title": "停用低值物品仓库",
            "object": warehouse,
            "cancel_url": "supplies:warehouse-list",
        },
    )


@login_required
def item_list(request):
    company = _company_or_404()
    require_view_supply_master_data(request.user)
    queryset = scoped_supply_items(
        request.user,
        company,
        SupplyItem.objects.select_related("category", "default_warehouse"),
    )
    query = request.GET.get("q", "").strip()
    category_id = request.GET.get("category", "").strip()
    item_type = request.GET.get("item_type", "").strip()
    if query:
        queryset = queryset.filter(
            Q(item_code__icontains=query) | Q(name__icontains=query)
        )
    if category_id:
        try:
            category_uuid = UUID(category_id)
        except (TypeError, ValueError):
            queryset = queryset.none()
            category_id = ""
        else:
            queryset = queryset.filter(category_id=category_uuid)
    if item_type in SupplyItemType.values:
        queryset = queryset.filter(item_type=item_type)
    else:
        item_type = ""
    queryset, selected_status = _status_filter(queryset, request)
    return render(
        request,
        "supplies/item_list.html",
        {
            "page_obj": _page(queryset.order_by("normalized_item_code"), request),
            "query": query,
            "selected_category": category_id,
            "selected_item_type": item_type,
            "selected_status": selected_status,
            "categories": scoped_supply_categories(request.user, company).filter(
                is_active=True
            ),
            "item_types": SupplyItemType.choices,
            "can_manage": can_manage_supply_item(
                request.user, SupplyItemType.DURABLE_QUANTITY
            ),
            "can_manage_consumables": can_manage_supply_item(
                request.user, SupplyItemType.CONSUMABLE
            ),
        },
    )


@login_required
def item_create(request):
    company = _company_or_404()
    require_manage_supply_item(request.user, SupplyItemType.DURABLE_QUANTITY)
    form = SupplyItemForm(
        request.POST or None, actor=request.user, company=company
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_supply_item(
                actor=request.user,
                company=company,
                data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "低值物品档案已新增。")
            return redirect("supplies:item-list")
    return render(
        request,
        "supplies/item_form.html",
        {"form": form, "title": "新增低值物品"},
    )


@login_required
def item_edit(request, pk):
    company = _company_or_404()
    item = get_object_or_404(scoped_supply_items(request.user, company), pk=pk)
    require_manage_supply_item(request.user, item.item_type)
    form = SupplyItemForm(
        request.POST or None,
        instance=item,
        actor=request.user,
        company=company,
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_supply_item(
                actor=request.user,
                item=item,
                data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "低值物品档案已更新。")
            return redirect("supplies:item-list")
    return render(
        request,
        "supplies/item_form.html",
        {"form": form, "title": "编辑低值物品", "object": item},
    )


@login_required
def item_deactivate(request, pk):
    company = _company_or_404()
    item = get_object_or_404(scoped_supply_items(request.user, company), pk=pk)
    require_manage_supply_item(request.user, item.item_type)
    form = SupplyDeactivateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        deactivate_supply_item(
            actor=request.user,
            item=item,
            reason=form.cleaned_data["reason"],
            request=request,
        )
        messages.success(request, "低值物品档案已停用，历史引用仍会保留。")
        return redirect("supplies:item-list")
    return render(
        request,
        "supplies/deactivate_confirm.html",
        {
            "form": form,
            "title": "停用低值物品档案",
            "object": item,
            "cancel_url": "supplies:item-list",
        },
    )


@login_required
def item_import(request):
    company = _company_or_404()
    require_manage_supply_item(request.user, SupplyItemType.DURABLE_QUANTITY)
    return redirect("imports:upload", import_type="item_master")
