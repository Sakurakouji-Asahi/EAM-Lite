from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.assets.permissions import can_create_asset_draft
from apps.masterdata.models import InitializationSetting
from apps.masterdata.normalization import normalize_identifier
from apps.masterdata.permissions import (
    current_company,
    role_names_for,
    scoped_departments,
    scoped_employees,
)

from .forms import (
    SupplyCategoryForm,
    SupplyConsumableReturnForm,
    SupplyCustodyTransferForm,
    SupplyCustodyWriteOffForm,
    SupplyDurableReturnForm,
    SupplyDeactivateForm,
    SupplyDocumentCancelForm,
    SupplyDocumentForm,
    SupplyDocumentLineFormSet,
    SupplyDocumentPostForm,
    SupplyDocumentReverseForm,
    SupplyItemForm,
    SupplyWarehouseForm,
)
from .models import (
    SupplyCategory,
    SupplyCustody,
    SupplyCustodyAction,
    SupplyDocument,
    SupplyDocumentLine,
    SupplyDocumentStatus,
    SupplyDocumentType,
    SupplyItem,
    SupplyItemType,
    SupplyStockBalance,
    SupplyStockLedger,
    SupplyWarehouse,
)
from .permissions import (
    can_create_supply_document,
    can_manage_supply_category,
    can_manage_supply_item,
    can_manage_supply_warehouse,
    can_manage_supply_custody,
    can_post_supply_document,
    can_reverse_supply_document,
    can_view_supply_custodies,
    can_view_supply_cost,
    can_view_supply_master_data,
    require_manage_supply_category,
    require_manage_supply_item,
    require_manage_supply_warehouse,
    require_create_supply_document,
    require_post_supply_document,
    require_reverse_supply_document,
    require_view_supply_custodies,
    require_view_supply_documents,
    require_view_supply_master_data,
    require_view_supply_module,
    require_view_supply_stock,
    scoped_supply_categories,
    scoped_supply_items,
    scoped_supply_documents,
    scoped_supply_custodies,
    scoped_supply_stock_balances,
    scoped_supply_stock_ledgers,
    scoped_supply_warehouses,
)
from .services import (
    cancel_supply_document,
    create_supply_document,
    create_supply_category,
    create_supply_item,
    create_supply_warehouse,
    deactivate_supply_category,
    deactivate_supply_item,
    deactivate_supply_warehouse,
    post_supply_document,
    return_custody_to_warehouse,
    reverse_supply_document,
    transfer_custody,
    update_supply_category,
    update_supply_item,
    update_draft_document,
    update_supply_warehouse,
    write_off_custody,
)


PAGE_SIZE = 25
SPRINT15_DOCUMENT_TYPES = frozenset(
    {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
        SupplyDocumentType.ISSUE,
        SupplyDocumentType.RETURN,
        SupplyDocumentType.TRANSFER,
        SupplyDocumentType.REVERSAL,
    }
)
SPRINT15_DOCUMENT_TYPE_CHOICES = tuple(
    choice for choice in SupplyDocumentType.choices if choice[0] in SPRINT15_DOCUMENT_TYPES
)
SPRINT15_STATUSES = frozenset(
    {
        SupplyDocumentStatus.DRAFT,
        SupplyDocumentStatus.POSTED,
        SupplyDocumentStatus.CANCELLED,
        SupplyDocumentStatus.REVERSED,
    }
)
SPRINT15_STATUS_CHOICES = tuple(
    choice for choice in SupplyDocumentStatus.choices if choice[0] in SPRINT15_STATUSES
)


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


def _pagination_query(request):
    values = request.GET.copy()
    values.pop("page", None)
    return values.urlencode()


def _iso_date(value):
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _uuid_or_none(value):
    try:
        return UUID(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _line_formset_initial(document):
    return [
        {
            "item": line.item_id,
            "quantity": line.quantity,
            "entered_unit_cost": line.entered_unit_cost,
            "line_remark": line.line_remark,
        }
        for line in document.lines.all()
    ]


def _cleaned_line_rows(formset):
    rows = []
    for form in formset.forms:
        cleaned = getattr(form, "cleaned_data", None) or {}
        if not cleaned or cleaned.get("DELETE"):
            continue
        rows.append(
            {
                "item": cleaned["item"],
                "quantity": cleaned["quantity"],
                "entered_unit_cost": cleaned.get("entered_unit_cost"),
                "line_remark": cleaned.get("line_remark", ""),
            }
        )
    return rows


@login_required
def dashboard(request):
    company = _company_or_404()
    require_view_supply_module(request.user)
    categories = scoped_supply_categories(request.user, company)
    warehouses = scoped_supply_warehouses(request.user, company)
    items = scoped_supply_items(request.user, company)
    individual_asset_initialized = InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists()
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
            "document_count": scoped_supply_documents(
                request.user, company
            ).count(),
            "custody_count": scoped_supply_custodies(
                request.user, company
            ).filter(status="open").count(),
            "can_view_master_data": can_view_supply_master_data(request.user),
            "can_view_custodies": can_view_supply_custodies(request.user),
            "can_manage_categories": can_manage_supply_category(request.user),
            "can_manage_warehouses": can_manage_supply_warehouse(request.user),
            "can_manage_items": can_manage_supply_item(
                request.user, SupplyItemType.DURABLE_QUANTITY
            ),
            "can_create_individual_asset": can_create_asset_draft(
                request.user, company
            )
            and individual_asset_initialized,
            "individual_asset_initialized": individual_asset_initialized,
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


def _require_sprint15_manual_document_type(document_type):
    if document_type not in {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
        SupplyDocumentType.ISSUE,
        SupplyDocumentType.TRANSFER,
    }:
        raise Http404("该单据类型只能由受控来源或系统生成。")
    return document_type


@login_required
def document_list(request):
    company = _company_or_404()
    require_view_supply_documents(request.user)
    queryset = scoped_supply_documents(
        request.user,
        company,
        SupplyDocument.objects.select_related(
            "source_warehouse",
            "target_warehouse",
            "department",
            "employee",
            "created_by",
            "posted_by",
        ).prefetch_related("lines__item"),
    )
    query = request.GET.get("q", "").strip()
    document_type = request.GET.get("document_type", "").strip()
    status = request.GET.get("status", "").strip()
    warehouse_value = request.GET.get("warehouse", "").strip()
    item_value = request.GET.get("item", "").strip()
    date_from_value = request.GET.get("date_from", "").strip()
    date_to_value = request.GET.get("date_to", "").strip()
    if query:
        queryset = queryset.filter(
            Q(document_no__icontains=query)
            | Q(external_reference__icontains=query)
            | Q(counterparty_name__icontains=query)
        )
    if document_type in SPRINT15_DOCUMENT_TYPES:
        queryset = queryset.filter(document_type=document_type)
    else:
        document_type = ""
    if status in SPRINT15_STATUSES:
        queryset = queryset.filter(status=status)
    else:
        status = ""
    warehouse_id = _uuid_or_none(warehouse_value)
    if warehouse_value:
        queryset = (
            queryset.filter(
                Q(source_warehouse_id=warehouse_id) | Q(target_warehouse_id=warehouse_id)
            )
            if warehouse_id
            else queryset.none()
        )
    if item_value:
        queryset = queryset.filter(
            lines__item__normalized_item_code=normalize_identifier(item_value)
        )
    date_from = _iso_date(date_from_value)
    date_to = _iso_date(date_to_value)
    if date_from_value:
        queryset = queryset.filter(business_date__gte=date_from) if date_from else queryset.none()
    if date_to_value:
        queryset = queryset.filter(business_date__lte=date_to) if date_to else queryset.none()
    queryset = queryset.distinct().order_by("-business_date", "-document_no")
    return render(
        request,
        "supplies/document_list.html",
        {
            "page_obj": _page(queryset, request),
            "pagination_query": _pagination_query(request),
            "query": query,
            "selected_document_type": document_type,
            "selected_status": status,
            "selected_warehouse": warehouse_value,
            "selected_item": item_value,
            "date_from": date_from_value,
            "date_to": date_to_value,
            "document_types": SPRINT15_DOCUMENT_TYPE_CHOICES,
            "statuses": SPRINT15_STATUS_CHOICES,
            "warehouses": scoped_supply_warehouses(request.user, company).order_by(
                "normalized_code"
            ),
            "can_manage": can_create_supply_document(request.user),
            "can_post": can_post_supply_document(request.user),
        },
    )


@login_required
def document_create(request, document_type):
    company = _company_or_404()
    document_type = _require_sprint15_manual_document_type(document_type)
    require_create_supply_document(request.user)
    form = SupplyDocumentForm(
        request.POST or None,
        actor=request.user,
        company=company,
        document_type=document_type,
    )
    formset = SupplyDocumentLineFormSet(
        request.POST or None,
        prefix="lines",
        form_kwargs={
            "actor": request.user,
            "company": company,
            "document_type": document_type,
        },
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            document = create_supply_document(
                actor=request.user,
                company=company,
                document_type=document_type,
                data=form.cleaned_data,
                lines=_cleaned_line_rows(formset),
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "库存单据草稿已创建，尚未影响库存。")
            return redirect("supplies:document-detail", pk=document.pk)
    return render(
        request,
        "supplies/document_form.html",
        {
            "form": form,
            "formset": formset,
            "document_type": document_type,
            "show_entered_cost": document_type
            in {SupplyDocumentType.OPENING, SupplyDocumentType.RECEIPT},
            "title": {
                SupplyDocumentType.OPENING: "新建期初入库单",
                SupplyDocumentType.RECEIPT: "新建日常入库单",
                SupplyDocumentType.ISSUE: "新建领用单",
                SupplyDocumentType.TRANSFER: "新建仓库调拨单",
            }[document_type],
        },
    )


@login_required
def document_edit(request, pk):
    company = _company_or_404()
    require_create_supply_document(request.user)
    document = get_object_or_404(
        scoped_supply_documents(
            request.user,
            company,
            SupplyDocument.objects.select_related(
                "source_warehouse", "target_warehouse", "department", "employee"
            ).prefetch_related(
                "lines__item"
            ),
        ),
        pk=pk,
    )
    if document.status != SupplyDocumentStatus.DRAFT:
        raise PermissionDenied("该单据已过账或取消，不能编辑。")
    if document.document_type not in {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
        SupplyDocumentType.ISSUE,
        SupplyDocumentType.TRANSFER,
    }:
        raise PermissionDenied("该来源单据草稿不提供普通编辑入口；可取消后重新发起。")
    form = SupplyDocumentForm(
        request.POST or None,
        actor=request.user,
        company=company,
        document_type=document.document_type,
        instance=document,
    )
    formset = SupplyDocumentLineFormSet(
        request.POST or None,
        initial=None if request.method == "POST" else _line_formset_initial(document),
        prefix="lines",
        form_kwargs={
            "actor": request.user,
            "company": company,
            "document_type": document.document_type,
        },
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        data = dict(form.cleaned_data)
        data.pop("idempotency_key", None)
        try:
            update_draft_document(
                actor=request.user,
                document=document,
                data=data,
                lines=_cleaned_line_rows(formset),
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "库存单据草稿已更新，库存仍未变化。")
            return redirect("supplies:document-detail", pk=document.pk)
    return render(
        request,
        "supplies/document_form.html",
        {
            "form": form,
            "formset": formset,
            "document": document,
            "document_type": document.document_type,
            "show_entered_cost": document.document_type
            in {SupplyDocumentType.OPENING, SupplyDocumentType.RECEIPT},
            "title": f"编辑草稿 {document.document_no}",
        },
    )


@login_required
def document_detail(request, pk):
    company = _company_or_404()
    require_view_supply_documents(request.user)
    document = get_object_or_404(
        scoped_supply_documents(
            request.user,
            company,
            SupplyDocument.objects.select_related(
                "source_warehouse",
                "target_warehouse",
                "created_by",
                "posted_by",
                "cancelled_by",
                "reversed_by",
                "reversal_of",
            ).prefetch_related(
                "lines__item",
                "lines__source_issue_line__document",
                "lines__source_custody",
            ),
        ),
        pk=pk,
    )
    show_cost = can_view_supply_cost(request.user)
    line_rows = []
    total_amount = Decimal("0.00")
    for line in document.lines.all():
        row = {
            "line_id": line.pk,
            "line_no": line.line_no,
            "item": line.item,
            "quantity": line.quantity,
            "line_remark": line.line_remark,
            "source_issue_line": line.source_issue_line,
            "source_custody": line.source_custody,
        }
        if (
            document.document_type == SupplyDocumentType.ISSUE
            and document.status == SupplyDocumentStatus.POSTED
            and line.item.item_type == SupplyItemType.CONSUMABLE
        ):
            returned_quantity = line.return_lines.filter(
                document__document_type=SupplyDocumentType.RETURN,
                document__status=SupplyDocumentStatus.POSTED,
            ).aggregate(total=Sum("quantity"))["total"] or Decimal("0.0000")
            row["returnable_quantity"] = line.quantity - returned_quantity
        if show_cost:
            row.update(
                {
                    "entered_unit_cost": line.entered_unit_cost,
                    "posted_unit_cost": line.posted_unit_cost,
                    "posted_amount": line.posted_amount,
                }
            )
            if line.posted_amount is not None:
                total_amount += line.posted_amount
        line_rows.append(row)
    return render(
        request,
        "supplies/document_detail.html",
        {
            "document": document,
            "line_rows": line_rows,
            "show_cost": show_cost,
            "total_amount": total_amount if show_cost else None,
            "can_manage": can_create_supply_document(
                request.user, document=document
            ),
            "can_post": can_post_supply_document(
                request.user, document=document
            ),
            "can_reverse": can_reverse_supply_document(
                request.user, document=document
            ),
            "can_edit": document.document_type
            in {
                SupplyDocumentType.OPENING,
                SupplyDocumentType.RECEIPT,
                SupplyDocumentType.ISSUE,
                SupplyDocumentType.TRANSFER,
            },
            "reversal_document": SupplyDocument.objects.filter(
                company=company, reversal_of=document
            ).first(),
        },
    )


@login_required
def document_cancel(request, pk):
    company = _company_or_404()
    document = get_object_or_404(
        scoped_supply_documents(request.user, company), pk=pk
    )
    require_create_supply_document(request.user, document=document)
    if document.status != SupplyDocumentStatus.DRAFT:
        raise PermissionDenied("只有草稿单据可以取消。")
    form = SupplyDocumentCancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            cancel_supply_document(
                actor=request.user,
                document=document,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "库存单据草稿已取消，库存未发生变化。")
            return redirect("supplies:document-detail", pk=document.pk)
    return render(
        request,
        "supplies/document_cancel_confirm.html",
        {"document": document, "form": form},
    )


@login_required
def document_post(request, pk):
    company = _company_or_404()
    if not role_names_for(request.user).intersection(
        {"system_admin", "finance", "warehouse", "equipment"}
    ):
        require_post_supply_document(request.user)
    document = get_object_or_404(
        scoped_supply_documents(
            request.user,
            company,
            SupplyDocument.objects.select_related(
                "source_warehouse", "target_warehouse", "department", "employee"
            ).prefetch_related(
                "lines__item"
            ),
        ),
        pk=pk,
    )
    require_post_supply_document(request.user, document=document)
    if request.method == "GET" and document.status == SupplyDocumentStatus.POSTED:
        return redirect("supplies:document-detail", pk=document.pk)
    if document.status not in {
        SupplyDocumentStatus.DRAFT,
        SupplyDocumentStatus.POSTED,
    }:
        raise PermissionDenied("该单据不能过账。")
    form = SupplyDocumentPostForm(
        request.POST or None,
        document=document,
    )
    if request.method == "POST" and form.is_valid():
        try:
            post_supply_document(
                actor=request.user,
                document=document,
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "库存单据已过账，余额和不可变流水已同步生成。")
            return redirect("supplies:document-detail", pk=document.pk)
    return render(
        request,
        "supplies/document_post_confirm.html",
        {
            "document": document,
            "form": form,
            "lines": document.lines.all(),
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def consumable_return_create(request, line_pk):
    company = _company_or_404()
    require_create_supply_document(request.user)
    source_line = get_object_or_404(
        SupplyDocumentLine.objects.select_related(
            "document", "document__department", "document__employee", "item"
        ),
        pk=line_pk,
        company=company,
        document__document_type=SupplyDocumentType.ISSUE,
        document__status=SupplyDocumentStatus.POSTED,
        item__item_type=SupplyItemType.CONSUMABLE,
    )
    returned = source_line.return_lines.filter(
        document__document_type=SupplyDocumentType.RETURN,
        document__status=SupplyDocumentStatus.POSTED,
    ).aggregate(quantity=Sum("quantity"), amount=Sum("posted_amount"))
    returned_quantity = returned["quantity"] or Decimal("0.0000")
    returnable_quantity = source_line.quantity - returned_quantity
    form = SupplyConsumableReturnForm(
        request.POST or None,
        actor=request.user,
        company=company,
        source_issue_line=source_line,
        initial={"quantity": returnable_quantity if returnable_quantity > 0 else None},
    )
    if request.method == "POST" and form.is_valid():
        if returnable_quantity <= 0:
            form.add_error("quantity", "该原领用明细已无可退数量。")
        else:
            try:
                document = create_supply_document(
                    actor=request.user,
                    company=company,
                    document_type=SupplyDocumentType.RETURN,
                    data={
                        "business_date": form.cleaned_data["business_date"],
                        "target_warehouse": form.cleaned_data["target_warehouse"],
                        "remark": form.cleaned_data["reason"],
                        "idempotency_key": form.cleaned_data["idempotency_key"],
                    },
                    lines=[
                        {
                            "item": source_line.item,
                            "quantity": form.cleaned_data["quantity"],
                            "entered_unit_cost": None,
                            "source_issue_line": source_line,
                            "line_remark": form.cleaned_data["reason"],
                        }
                    ],
                    request=request,
                )
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                messages.success(request, "易耗品退回单草稿已创建，尚未影响库存。")
                return redirect("supplies:document-detail", pk=document.pk)
    return render(
        request,
        "supplies/consumable_return_form.html",
        {
            "form": form,
            "source_line": source_line,
            "returned_quantity": returned_quantity,
            "returnable_quantity": returnable_quantity,
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def document_reverse(request, pk):
    company = _company_or_404()
    if not role_names_for(request.user).intersection(
        {"system_admin", "finance", "warehouse", "equipment"}
    ):
        require_reverse_supply_document(request.user)
    document = get_object_or_404(
        scoped_supply_documents(
            request.user,
            company,
            SupplyDocument.objects.select_related(
                "source_warehouse", "target_warehouse", "department", "employee"
            ).prefetch_related("lines__item"),
        ),
        pk=pk,
    )
    require_reverse_supply_document(request.user, document=document)
    if document.status == SupplyDocumentStatus.REVERSED:
        reversal = SupplyDocument.objects.filter(
            company=company, reversal_of=document
        ).first()
        if reversal is not None:
            return redirect("supplies:document-detail", pk=reversal.pk)
    if document.status != SupplyDocumentStatus.POSTED:
        raise PermissionDenied("只允许冲销已过账单据。")
    if document.document_type == SupplyDocumentType.REVERSAL:
        raise PermissionDenied("冲销单不能再次冲销。")
    form = SupplyDocumentReverseForm(
        request.POST or None, actor=request.user, document=document
    )
    if request.method == "POST" and form.is_valid():
        try:
            reversal = reverse_supply_document(
                actor=request.user,
                document=document,
                idempotency_key=form.cleaned_data["idempotency_key"],
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "完整冲销已过账，原流水保留并已生成精确反向流水。")
            return redirect("supplies:document-detail", pk=reversal.pk)
    return render(
        request,
        "supplies/document_reverse_confirm.html",
        {
            "document": document,
            "form": form,
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def custody_list(request):
    company = _company_or_404()
    require_view_supply_custodies(request.user)
    queryset = scoped_supply_custodies(
        request.user,
        company,
        SupplyCustody.objects.select_related(
            "item",
            "item__category",
            "department",
            "employee",
            "origin_issue_line__document",
            "origin_import_row__batch",
            "parent_custody",
        ),
    )
    query = request.GET.get("q", "").strip()
    item_value = request.GET.get("item", "").strip()
    department_value = request.GET.get("department", "").strip()
    employee_value = request.GET.get("employee", "").strip()
    status = request.GET.get("status", "").strip()
    date_from_value = request.GET.get("date_from", "").strip()
    source_document = request.GET.get("source_document", "").strip()
    source_type = request.GET.get("source_type", "").strip()
    if query:
        queryset = queryset.filter(
            Q(item__item_code__icontains=query)
            | Q(item__name__icontains=query)
            | Q(department__name__icontains=query)
            | Q(employee__name__icontains=query)
            | Q(origin_issue_line__document__document_no__icontains=query)
        )
    if item_value:
        queryset = queryset.filter(
            item__normalized_item_code=normalize_identifier(item_value)
        )
    department_id = _uuid_or_none(department_value)
    if department_value:
        queryset = (
            queryset.filter(department_id=department_id)
            if department_id
            else queryset.none()
        )
    employee_id = _uuid_or_none(employee_value)
    if employee_value:
        queryset = (
            queryset.filter(employee_id=employee_id)
            if employee_id
            else queryset.none()
        )
    if status in {"open", "closed"}:
        queryset = queryset.filter(status=status)
    else:
        status = ""
    date_from = _iso_date(date_from_value)
    if date_from_value:
        queryset = (
            queryset.filter(started_on__gte=date_from)
            if date_from
            else queryset.none()
        )
    if source_document:
        root_ids = list(
            SupplyCustody.objects.filter(
                company=company,
                parent_custody__isnull=True,
                origin_issue_line__document__document_no__icontains=source_document,
            ).values_list("pk", flat=True)
        )
        matched_ids = set(root_ids)
        frontier = root_ids
        while frontier:
            frontier = list(
                SupplyCustody.objects.filter(
                    company=company,
                    parent_custody_id__in=frontier,
                )
                .exclude(pk__in=matched_ids)
                .values_list("pk", flat=True)
            )
            matched_ids.update(frontier)
        queryset = queryset.filter(pk__in=matched_ids)
    if source_type == "issue":
        queryset = queryset.filter(
            parent_custody__isnull=True, origin_issue_line__isnull=False
        )
    elif source_type == "opening":
        queryset = queryset.filter(
            parent_custody__isnull=True, origin_import_row__isnull=False
        )
    elif source_type == "transfer":
        queryset = queryset.filter(parent_custody__isnull=False)
    else:
        source_type = ""
    return render(
        request,
        "supplies/custody_list.html",
        {
            "page_obj": _page(queryset.order_by("-started_on", "-created_at"), request),
            "pagination_query": _pagination_query(request),
            "query": query,
            "selected_item": item_value,
            "selected_department": department_value,
            "selected_employee": employee_value,
            "selected_status": status,
            "date_from": date_from_value,
            "source_document": source_document,
            "source_type": source_type,
            "departments": scoped_departments(
                request.user, company
            ).order_by("normalized_code"),
            "employees": scoped_employees(
                request.user, company
            ).select_related("department").order_by("normalized_employee_no"),
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def custody_detail(request, pk):
    company = _company_or_404()
    require_view_supply_custodies(request.user)
    custody = get_object_or_404(
        scoped_supply_custodies(
            request.user,
            company,
            SupplyCustody.objects.select_related(
                "item",
                "item__category",
                "department",
                "employee",
                "origin_issue_line__document",
                "origin_import_row__batch",
                "parent_custody__department",
                "parent_custody__employee",
            ),
        ),
        pk=pk,
    )
    movements = (
        custody.incoming_movements.filter(company=company)
        | custody.outgoing_movements.filter(company=company)
    ).select_related(
        "from_custody",
        "to_custody",
        "source_document_line__document",
        "created_by",
        "reverses_movement",
    ).order_by("created_at")
    ancestor_chain = []
    current = custody.parent_custody
    seen = set()
    while current is not None and current.pk not in seen:
        seen.add(current.pk)
        ancestor_chain.append(current)
        current = (
            SupplyCustody.objects.select_related(
                "parent_custody", "department", "employee", "item"
            )
            .filter(pk=current.parent_custody_id, company=company)
            .first()
            if current.parent_custody_id
            else None
        )
    ancestor_chain.reverse()
    child_custodies = custody.child_custodies.select_related(
        "department", "employee", "item"
    ).order_by("started_on", "created_at")
    return render(
        request,
        "supplies/custody_detail.html",
        {
            "custody": custody,
            "movements": movements,
            "show_cost": can_view_supply_cost(request.user),
            "ancestor_chain": ancestor_chain,
            "child_custodies": child_custodies,
            "can_return": custody.status == "open"
            and custody.item.is_active
            and can_manage_supply_custody(
                request.user, custody, action="return_draft"
            ),
            "can_transfer": custody.status == "open"
            and can_manage_supply_custody(
                request.user, custody, action="transfer"
            ),
            "can_write_off": custody.status == "open"
            and can_manage_supply_custody(
                request.user, custody, action="loss"
            ),
        },
    )


def _custody_for_action(request, company, pk):
    return get_object_or_404(
        scoped_supply_custodies(
            request.user,
            company,
            SupplyCustody.objects.select_related(
                "company",
                "item",
                "department",
                "employee",
                "origin_issue_line__document",
                "origin_import_row__batch",
                "parent_custody",
            ),
        ),
        pk=pk,
    )


@login_required
def durable_return_create(request, pk):
    company = _company_or_404()
    custody = _custody_for_action(request, company, pk)
    form = SupplyDurableReturnForm(
        request.POST or None,
        actor=request.user,
        company=company,
        custody=custody,
    )
    if request.method == "POST" and form.is_valid():
        try:
            document = return_custody_to_warehouse(
                custody=custody,
                target_warehouse=form.cleaned_data["target_warehouse"],
                quantity=form.cleaned_data["quantity"],
                business_date=form.cleaned_data["business_date"],
                reason=form.cleaned_data["reason"],
                actor=request.user,
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "耐用品归还草稿已创建；过账前尚未改变库存或保管。")
            return redirect("supplies:document-detail", pk=document.pk)
    return render(
        request,
        "supplies/custody_action_form.html",
        {
            "form": form,
            "custody": custody,
            "title": "耐用品归还仓库",
            "submit_label": "创建归还草稿",
            "warning": "归还将在库存单据过账时原子减少保管并增加目标仓库库存。",
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def custody_transfer(request, pk):
    company = _company_or_404()
    custody = _custody_for_action(request, company, pk)
    form = SupplyCustodyTransferForm(
        request.POST or None,
        actor=request.user,
        company=company,
        custody=custody,
    )
    if request.method == "POST" and form.is_valid():
        try:
            target = transfer_custody(
                custody=custody,
                quantity=form.cleaned_data["quantity"],
                target_department=form.cleaned_data["target_department"],
                target_employee=form.cleaned_data["target_employee"],
                business_date=form.cleaned_data["business_date"],
                reason=form.cleaned_data["reason"],
                actor=request.user,
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "责任转交已完成；目标形成独立保管来源链，仓库库存未变化。")
            return redirect("supplies:custody-detail", pk=target.pk)
    return render(
        request,
        "supplies/custody_action_form.html",
        {
            "form": form,
            "custody": custody,
            "title": "责任转交",
            "submit_label": "确认转交",
            "warning": "每次转交新建目标保管，不与其他来源或成本批次自动合并。",
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def custody_write_off(request, pk, action):
    if action not in {SupplyCustodyAction.LOSS, SupplyCustodyAction.SCRAP}:
        raise Http404("不支持的保管动作。")
    company = _company_or_404()
    custody = _custody_for_action(request, company, pk)
    form = SupplyCustodyWriteOffForm(
        request.POST or None,
        actor=request.user,
        company=company,
        custody=custody,
        action=action,
    )
    title = "耐用品报损" if action == SupplyCustodyAction.LOSS else "耐用品报废"
    if request.method == "POST" and form.is_valid():
        try:
            write_off_custody(
                custody=custody,
                quantity=form.cleaned_data["quantity"],
                action=action,
                business_date=form.cleaned_data["business_date"],
                reason=form.cleaned_data["reason"],
                actor=request.user,
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, f"{title}已记录；未增加仓库库存，也未生成会计凭证。")
            return redirect("supplies:custody-detail", pk=custody.pk)
    return render(
        request,
        "supplies/custody_action_form.html",
        {
            "form": form,
            "custody": custody,
            "title": title,
            "submit_label": f"确认{title[-2:]}",
            "warning": "该动作会减少当前在管数量和管理金额，且本 Sprint 不提供撤销。",
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def my_custodies(request):
    company = _company_or_404()
    require_view_supply_custodies(request.user)
    queryset = SupplyCustody.objects.filter(
        company=company,
        employee__user=request.user,
        status="open",
    ).select_related("item", "department", "employee")
    return render(
        request,
        "supplies/my_custodies.html",
        {
            "page_obj": _page(queryset.order_by("-started_on", "-created_at"), request),
            "pagination_query": _pagination_query(request),
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def opening_custody_import(request):
    company = _company_or_404()
    from .permissions import require_import_opening_custody

    require_import_opening_custody(request.user)
    if company is None:
        raise Http404
    return redirect("imports:upload", import_type="opening_custody")


@login_required
def individual_durable_create(request):
    return redirect(f"{reverse('assets:asset-create')}?source=individual_durable")


@login_required
def individual_durable_list(request):
    return redirect(
        f"{reverse('assets:asset-list')}?accounting_treatment=controlled_non_fixed"
    )


@login_required
def stock_balance_list(request):
    company = _company_or_404()
    require_view_supply_stock(request.user)
    queryset = scoped_supply_stock_balances(
        request.user,
        company,
        SupplyStockBalance.objects.select_related("warehouse", "item", "item__category"),
    )
    query = request.GET.get("q", "").strip()
    warehouse_value = request.GET.get("warehouse", "").strip()
    item_value = request.GET.get("item", "").strip()
    if query:
        queryset = queryset.filter(
            Q(item__item_code__icontains=query)
            | Q(item__name__icontains=query)
            | Q(warehouse__code__icontains=query)
            | Q(warehouse__name__icontains=query)
        )
    warehouse_id = _uuid_or_none(warehouse_value)
    if warehouse_value:
        queryset = queryset.filter(warehouse_id=warehouse_id) if warehouse_id else queryset.none()
    if item_value:
        queryset = queryset.filter(
            item__normalized_item_code=normalize_identifier(item_value)
        )
    return render(
        request,
        "supplies/stock_balance_list.html",
        {
            "page_obj": _page(
                queryset.order_by(
                    "warehouse__normalized_code", "item__normalized_item_code"
                ),
                request,
            ),
            "pagination_query": _pagination_query(request),
            "query": query,
            "selected_warehouse": warehouse_value,
            "selected_item": item_value,
            "warehouses": scoped_supply_warehouses(request.user, company).order_by(
                "normalized_code"
            ),
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def stock_ledger_list(request):
    company = _company_or_404()
    require_view_supply_stock(request.user)
    queryset = scoped_supply_stock_ledgers(
        request.user,
        company,
        SupplyStockLedger.objects.select_related(
            "warehouse", "item", "document", "document_line", "created_by"
        ),
    )
    query = request.GET.get("q", "").strip()
    document_type = request.GET.get("document_type", "").strip()
    status = request.GET.get("status", "").strip()
    warehouse_value = request.GET.get("warehouse", "").strip()
    item_value = request.GET.get("item", "").strip()
    date_from_value = request.GET.get("date_from", "").strip()
    date_to_value = request.GET.get("date_to", "").strip()
    if query:
        queryset = queryset.filter(
            Q(document__document_no__icontains=query)
            | Q(item__item_code__icontains=query)
            | Q(item__name__icontains=query)
        )
    if document_type in SPRINT15_DOCUMENT_TYPES:
        queryset = queryset.filter(document__document_type=document_type)
    else:
        document_type = ""
    if status in SPRINT15_STATUSES:
        queryset = queryset.filter(document__status=status)
    else:
        status = ""
    warehouse_id = _uuid_or_none(warehouse_value)
    if warehouse_value:
        queryset = queryset.filter(warehouse_id=warehouse_id) if warehouse_id else queryset.none()
    if item_value:
        queryset = queryset.filter(
            item__normalized_item_code=normalize_identifier(item_value)
        )
    date_from = _iso_date(date_from_value)
    date_to = _iso_date(date_to_value)
    if date_from_value:
        queryset = queryset.filter(document__business_date__gte=date_from) if date_from else queryset.none()
    if date_to_value:
        queryset = queryset.filter(document__business_date__lte=date_to) if date_to else queryset.none()
    return render(
        request,
        "supplies/stock_ledger_list.html",
        {
            "page_obj": _page(queryset.order_by("-occurred_at", "document_line__line_no"), request),
            "pagination_query": _pagination_query(request),
            "query": query,
            "selected_document_type": document_type,
            "selected_status": status,
            "selected_warehouse": warehouse_value,
            "selected_item": item_value,
            "date_from": date_from_value,
            "date_to": date_to_value,
            "document_types": SPRINT15_DOCUMENT_TYPE_CHOICES,
            "statuses": SPRINT15_STATUS_CHOICES,
            "warehouses": scoped_supply_warehouses(request.user, company).order_by(
                "normalized_code"
            ),
            "show_cost": can_view_supply_cost(request.user),
        },
    )


@login_required
def opening_stock_import(request):
    company = _company_or_404()
    require_create_supply_document(request.user)
    if company is None:
        raise Http404
    return redirect("imports:upload", import_type="opening_stock")
