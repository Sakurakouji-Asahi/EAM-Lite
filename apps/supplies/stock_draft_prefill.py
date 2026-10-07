"""Carry an authorized stock row into the existing unsaved issue/transfer form."""
from urllib.parse import urlencode
from uuid import UUID

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse

from .permissions import can_create_supply_document, scoped_supply_stock_balances


def attach_stock_draft_links(actor, balances, return_to):
    can_create = can_create_supply_document(actor)
    for balance in balances:
        balance.issue_draft_url = ""
        balance.transfer_draft_url = ""
        if not can_create or not balance.warehouse.is_active or not balance.item.is_active or balance.quantity_on_hand <= 0:
            continue
        query = urlencode({"stock_warehouse": str(balance.warehouse_id), "stock_item": str(balance.item_id), "return_to": return_to})
        balance.issue_draft_url = reverse("supplies:document-create", args=["issue"]) + "?" + query
        balance.transfer_draft_url = reverse("supplies:document-create", args=["transfer"]) + "?" + query


def apply_stock_draft_prefill(request, company, document_type, form, formset):
    if request.method != "GET" or document_type not in {"issue", "transfer"}:
        return None
    warehouse_value = request.GET.get("stock_warehouse", "")
    item_value = request.GET.get("stock_item", "")
    if not warehouse_value and not item_value:
        return None
    try:
        warehouse_id, item_id = UUID(warehouse_value), UUID(item_value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise Http404("库存预填来源无效，请返回当前库存重新选择。") from exc
    warehouse = get_object_or_404(form.fields["source_warehouse"].queryset, pk=warehouse_id)
    line_form = formset.forms[0]
    item = get_object_or_404(line_form.fields["item"].queryset, pk=item_id)
    balance = get_object_or_404(scoped_supply_stock_balances(request.user, company).only("quantity_on_hand"),
        warehouse_id=warehouse.pk, item_id=item.pk)
    form.initial["source_warehouse"] = warehouse.pk
    line_form.initial["item"] = item.pk
    return {"warehouse": warehouse, "item": item, "quantity": balance.quantity_on_hand}
