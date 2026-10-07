"""Read-only, scoped paper checklists for published inventory snapshots."""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.inventory.forms import InventoryResultFilterForm
from apps.inventory.print_workspace import paper_filter_summary, paper_result_items
from apps.inventory.views import _task, _filtered_result_rows, task_detail


@login_required
@never_cache
@require_GET
def task_paper_checklist(request, pk):
    task = _task(request, pk)
    if task.status == "draft":
        return HttpResponse("任务尚未发布，暂无应盘快照可打印。", status=400)
    form = InventoryResultFilterForm(request.GET)
    if not form.is_valid():
        return task_detail(request, pk)
    items = paper_result_items(_filtered_result_rows(task, form))
    query = request.GET.urlencode()
    back_url = reverse("inventory:task-detail", args=[task.pk])
    if query:
        back_url += "?" + query
    response = render(request, "inventory/paper_checklist.html", {
        "task": task, "paper_items": items, "paper_count": len(items),
        "paper_filters": paper_filter_summary(form), "generated_at": timezone.now(),
        "back_url": back_url + "#inventory-results",
    })
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response
