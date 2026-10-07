"""Read-only tracing of posted depreciation entries and their source records."""
from urllib.parse import urlencode, urlsplit

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.masterdata.permissions import current_company
from .models import DepreciationEntry
from .permissions import require_view_finance, scoped_finance_assets


def _return_path(request, asset_id):
    fallback = reverse("finance:asset-finance-detail", args=[asset_id])
    value = request.GET.get("return_to", "")
    if value and len(value) <= 6000 and "\\" not in value and not any(ord(char) < 32 for char in value):
        try:
            parts = urlsplit(value)
            if (not parts.scheme and not parts.netloc and not parts.fragment
                    and value.startswith("/") and not value.startswith("//") and parts.path == fallback):
                return value
        except ValueError:
            pass
    return fallback


@login_required
@never_cache
@require_GET
def depreciation_entry_detail(request, pk):
    require_view_finance(request.user)
    company = current_company()
    entries = DepreciationEntry.objects.filter(
        company=company, asset__in=scoped_finance_assets(request.user, company)
    ).select_related("asset", "depreciation_profile", "posted_by", "batch_item__batch",
                     "value_adjustment", "reversal_of", "reversal")
    entry = get_object_or_404(entries, pk=pk)
    return_path = _return_path(request, entry.asset_id)
    return_query = urlencode({"return_to": return_path})
    relations = []
    for name, label in (("reversal_of", "对应原分录"), ("reversal", "对应冲销分录")):
        related = getattr(entry, name, None)
        if related and related.company_id == entry.company_id and related.asset_id == entry.asset_id:
            relations.append({"label": label, "entry": related,
                "url": reverse("finance:entry-detail", args=[related.pk]) + "?" + return_query})
    batch = entry.batch_item.batch if entry.batch_item_id else None
    return render(request, "finance/entry_detail.html", {
        "entry": entry, "asset": entry.asset, "entry_relations": relations,
        "source_batch": batch, "source_adjustment": entry.value_adjustment,
        "entry_return_url": return_path + "#depreciation-entries",
        "source_batch_url": reverse("finance:batch-detail", args=[batch.pk]) if batch else "",
    })
