"""Warehouse business facts and navigation without changing stock decisions."""
from urllib.parse import urlencode, urlsplit

from django.db.models import Exists, OuterRef
from django.urls import reverse

from .models import SupplyCountDomain, SupplyCountTask
from .permissions import (can_view_supply_master_data, can_view_supply_stock,
    scoped_supply_count_tasks)
from .services import ACTIVE_SUPPLY_COUNT_STATUSES


def warehouse_freeze_tasks(company):
    return SupplyCountTask.objects.filter(company=company,
        count_domain=SupplyCountDomain.WAREHOUSE_STOCK, status__in=ACTIVE_SUPPLY_COUNT_STATUSES)


def annotate_warehouse_freeze(queryset, company):
    return queryset.annotate(count_frozen=Exists(warehouse_freeze_tasks(company).filter(warehouse_id=OuterRef("pk"))))


def warehouse_list_return(request):
    fallback = reverse("supplies:warehouse-list")
    value = request.GET.get("return_to", "")
    if not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return fallback
    try:
        parts = urlsplit(value)
        if not parts.scheme and not parts.netloc and value.startswith("/") and not value.startswith("//") and parts.path == fallback:
            return value
    except ValueError:
        pass
    return fallback


def warehouse_business_links(actor, warehouse):
    params = {"warehouse": str(warehouse.pk)}
    links = {}
    if can_view_supply_stock(actor):
        links["stock"] = reverse("supplies:stock-balance-list") + "?" + urlencode(params)
        links["ledger"] = reverse("supplies:stock-ledger-list") + "?" + urlencode(params)
    if can_view_supply_master_data(actor):
        links["counts"] = reverse("supplies:count-task-list") + "?" + urlencode({**params, "count_domain": "warehouse_stock"})
    return links


def warehouse_business_rows(actor, company, warehouses, return_to):
    warehouses = list(warehouses)
    task_map = {task.warehouse_id: task for task in scoped_supply_count_tasks(actor, company,
        warehouse_freeze_tasks(company)).filter(warehouse_id__in=[warehouse.pk for warehouse in warehouses])}
    rows = []
    for warehouse in warehouses:
        links = warehouse_business_links(actor, warehouse)
        task = task_map.get(warehouse.pk)
        task_url = ""
        if task is not None:
            task_url = reverse("supplies:count-task-detail", args=[task.pk]) + "?" + urlencode({"return_to": links["counts"]})
        rows.append({"warehouse": warehouse, "freeze_task": task, "freeze_task_url": task_url,
            "links": links, "detail_url": reverse("supplies:warehouse-detail", args=[warehouse.pk]) + "?" + urlencode({"return_to": return_to})})
    return rows
