"""Read-only asset information cards; opening a card never records label printing."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from apps.assets.access import asset_company_for_request, asset_or_404
from apps.assets.print_workspace import asset_card_context


@login_required
@require_GET
@never_cache
def asset_information_card(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    response = render(request, "assets/information_card.html", asset_card_context(request.user, asset))
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response
