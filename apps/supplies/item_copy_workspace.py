"""Reusable item archive values for a new, unsaved item form."""
from urllib.parse import urlencode
from uuid import UUID

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse

from .item_workspace import item_list_return
from .permissions import require_manage_supply_item, scoped_supply_items, scoped_supply_categories, scoped_supply_warehouses


COPY_FIELDS = ("name", "item_type", "unit", "specification", "model", "brand", "minimum_stock_quantity")


def item_copy_source(request, actor, company):
    value = request.POST.get("copy_from") or request.GET.get("copy_from", "")
    if not value:
        return None
    try:
        source_id = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise Http404("复制来源不存在。")
    source = get_object_or_404(scoped_supply_items(actor, company).select_related("category", "default_warehouse"), pk=source_id)
    require_manage_supply_item(actor, source.item_type)
    return source


def item_copy_initial(actor, source):
    initial = {name: getattr(source, name) for name in COPY_FIELDS}
    notices = []
    category = scoped_supply_categories(actor, source.company).filter(pk=source.category_id, is_active=True).first()
    if category is not None:
        initial["category"] = category.pk
    else:
        notices.append("来源分类已停用或不在可选范围，请为新物品重新选择分类。")
    if source.default_warehouse_id:
        warehouse = scoped_supply_warehouses(actor, source.company).filter(pk=source.default_warehouse_id, is_active=True).first()
        if warehouse is not None:
            initial["default_warehouse"] = warehouse.pk
        else:
            notices.append("来源默认仓库已停用或不在可选范围，请按需选择新物品的默认仓库。")
    return initial, notices


def item_archive_url(item, list_url):
    return reverse("supplies:item-detail", args=[item.pk]) + "?" + urlencode({"return_to": list_url})


def item_copy_url(item, list_url):
    return reverse("supplies:item-create") + "?" + urlencode({"copy_from": str(item.pk), "return_to": list_url})


def item_form_navigation(request):
    list_url = item_list_return(request)
    return {"item_list_url": list_url}
