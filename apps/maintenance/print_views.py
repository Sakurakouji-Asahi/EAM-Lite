"""Paper completion records use exactly the detail page's object and evidence scope."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from .print_workspace import record_attachment_rows, record_paper_url


@login_required
@require_GET
@never_cache
def record_print(request, pk):
    from .views import _record
    record = _record(request, pk)
    problem = getattr(record, "problem", None)
    response = render(request, "maintenance/record_print.html", {
        "record":record, "problem":problem,
        "attachments":record_attachment_rows(request.user,record,problem),
        "detail_url":record_paper_url(request,record,detail=True), "generated_at":timezone.now(),
    })
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response
