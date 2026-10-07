"""Reference a readable document in a fresh, unsaved manual-entry form."""
from urllib.parse import urlencode
from uuid import UUID

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse

from .forms import SupplyDocumentLineFormSet
from .permissions import can_create_supply_document, scoped_supply_documents


COPY_DOCUMENT_TYPES = frozenset({"receipt", "issue", "transfer"})


def document_copy_url(actor, document, return_to=""):
    if document.document_type not in COPY_DOCUMENT_TYPES or not can_create_supply_document(actor):
        return ""
    if len(document.lines.all()) > SupplyDocumentLineFormSet.max_num:
        return ""
    query = {"copy_from": str(document.pk)}
    if return_to:
        query["return_to"] = return_to
    return reverse("supplies:document-create", args=[document.document_type]) + "?" + urlencode(query)


def document_copy_source(request, company, document_type):
    value = request.POST.get("copy_from", request.GET.get("copy_from", ""))
    if not value:
        return None
    try:
        source_id = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise Http404("参考单据无效，请返回原单重新打开。") from exc
    if document_type not in COPY_DOCUMENT_TYPES:
        raise Http404("该单据类型没有重复开单入口。")
    if request.GET.get("stock_warehouse") or request.GET.get("stock_item"):
        raise Http404("请分别使用库存开单或参考原单入口。")
    source = get_object_or_404(scoped_supply_documents(request.user, company).select_related(
        "source_warehouse", "target_warehouse", "department", "employee").prefetch_related("lines__item"), pk=source_id)
    if source.document_type != document_type or len(source.lines.all()) > SupplyDocumentLineFormSet.max_num:
        raise Http404("参考单据类型或明细行数不适用于当前录入页。")
    return source


def apply_document_copy_prefill(request, source, form, formset):
    if source is None or request.method != "GET":
        return formset, []
    notices = []
    for name in ("source_warehouse", "target_warehouse", "department", "employee"):
        if name not in form.fields:
            continue
        value = getattr(source, name + "_id")
        available = value is not None and form.fields[name].queryset.filter(pk=value).exists()
        if name == "employee" and available:
            available = source.employee.department_id == form.initial.get("department")
        form.initial[name] = value if available else None
        if value is not None and not available:
            notices.append(f"{form.fields[name].label}已停用或不在当前可选范围，请重新选择。")
    if "counterparty_name" in form.fields:
        form.initial["counterparty_name"] = source.counterparty_name
    item_ids = set(formset.empty_form.fields["item"].queryset.values_list("pk", flat=True))
    initial = []
    for index, line in enumerate(source.lines.all(), start=1):
        available = line.item_id in item_ids
        initial.append({"item": line.item_id if available else None, "quantity": line.quantity})
        if not available:
            notices.append(f"第 {index} 行物品已停用或不在当前可选范围，原数量已保留，请重新选择物品并核对计量单位。")
    return SupplyDocumentLineFormSet(prefix=formset.prefix, form_kwargs=formset.form_kwargs, initial=initial), notices
