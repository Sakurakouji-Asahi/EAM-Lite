from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.assets.permissions import can_create_asset_draft
from apps.masterdata.models import InitializationSetting
from apps.masterdata.normalization import normalize_identifier
from apps.masterdata.permissions import current_company

from .forms import (
    SupplyCategoryForm,
    SupplyDeactivateForm,
    SupplyDocumentCancelForm,
    SupplyDocumentForm,
    SupplyDocumentLineFormSet,
    SupplyDocumentPostForm,
    SupplyItemForm,
    SupplyWarehouseForm,
)
from .models import (
    SupplyCategory,
    SupplyDocument,
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
    can_post_supply_document,
    can_view_supply_cost,
    require_manage_supply_category,
    require_manage_supply_item,
    require_manage_supply_warehouse,
    require_create_supply_document,
    require_post_supply_document,
    require_view_supply_documents,
    require_view_supply_master_data,
    scoped_supply_categories,
    scoped_supply_items,
    scoped_supply_documents,
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
    update_supply_category,
    update_supply_item,
    update_draft_document,
    update_supply_warehouse,
)


PAGE_SIZE = 25
SPRINT14_DOCUMENT_TYPES = frozenset(
    {SupplyDocumentType.OPENING, SupplyDocumentType.RECEIPT}
)
SPRINT14_DOCUMENT_TYPE_CHOICES = tuple(
    choice for choice in SupplyDocumentType.choices if choice[0] in SPRINT14_DOCUMENT_TYPES
)
SPRINT14_STATUSES = frozenset(
    {
        SupplyDocumentStatus.DRAFT,
        SupplyDocumentStatus.POSTED,
        SupplyDocumentStatus.CANCELLED,
    }
)
SPRINT14_STATUS_CHOICES = tuple(
    choice for choice in SupplyDocumentStatus.choices if choice[0] in SPRINT14_STATUSES
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
                "entered_unit_cost": cleaned["entered_unit_cost"],
                "line_remark": cleaned.get("line_remark", ""),
            }
        )
    return rows


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


def _require_sprint14_document_type(document_type):
    if document_type not in {
        SupplyDocumentType.OPENING,
        SupplyDocumentType.RECEIPT,
    }:
        raise Http404("Sprint 14 尚未开放该单据类型。")
    return document_type


@login_required
def document_list(request):
    company = _company_or_404()
    require_view_supply_documents(request.user)
    queryset = scoped_supply_documents(
        request.user,
        company,
        SupplyDocument.objects.select_related(
            "source_warehouse", "target_warehouse", "created_by", "posted_by"
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
    if document_type in SPRINT14_DOCUMENT_TYPES:
        queryset = queryset.filter(document_type=document_type)
    else:
        document_type = ""
    if status in SPRINT14_STATUSES:
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
            "document_types": SPRINT14_DOCUMENT_TYPE_CHOICES,
            "statuses": SPRINT14_STATUS_CHOICES,
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
    document_type = _require_sprint14_document_type(document_type)
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
        form_kwargs={"actor": request.user, "company": company},
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
            "title": "新建期初入库单" if document_type == "opening" else "新建日常入库单",
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
            SupplyDocument.objects.select_related("target_warehouse").prefetch_related(
                "lines__item"
            ),
        ),
        pk=pk,
    )
    if document.status != SupplyDocumentStatus.DRAFT:
        raise PermissionDenied("该单据已过账或取消，不能编辑。")
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
        form_kwargs={"actor": request.user, "company": company},
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
            ).prefetch_related("lines__item"),
        ),
        pk=pk,
    )
    show_cost = can_view_supply_cost(request.user)
    line_rows = []
    total_amount = Decimal("0.00")
    for line in document.lines.all():
        row = {
            "line_no": line.line_no,
            "item": line.item,
            "quantity": line.quantity,
            "line_remark": line.line_remark,
        }
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
            "can_manage": can_create_supply_document(request.user),
            "can_post": can_post_supply_document(request.user),
        },
    )


@login_required
def document_cancel(request, pk):
    company = _company_or_404()
    require_create_supply_document(request.user)
    document = get_object_or_404(
        scoped_supply_documents(request.user, company), pk=pk
    )
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
    require_post_supply_document(request.user)
    document = get_object_or_404(
        scoped_supply_documents(
            request.user,
            company,
            SupplyDocument.objects.select_related("target_warehouse").prefetch_related(
                "lines__item"
            ),
        ),
        pk=pk,
    )
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
def stock_balance_list(request):
    company = _company_or_404()
    require_view_supply_documents(request.user)
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
    require_view_supply_documents(request.user)
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
    if document_type in SPRINT14_DOCUMENT_TYPES:
        queryset = queryset.filter(document__document_type=document_type)
    else:
        document_type = ""
    if status in SPRINT14_STATUSES:
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
            "document_types": SPRINT14_DOCUMENT_TYPE_CHOICES,
            "statuses": SPRINT14_STATUS_CHOICES,
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
