import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.assets.custody_services import reverse_custody_return
from apps.assets.models import AssetCustodyReturn, AssetOriginLink
from apps.assets.permissions import can_create_asset_draft, can_view_asset_p1
from apps.assets.trace_services import reverse_asset_origin
from apps.assets.access import asset_or_404, asset_company_for_request


class ReverseTraceForm(forms.Form):
    idempotency_key = forms.CharField(max_length=128, initial=uuid.uuid4, widget=forms.HiddenInput)
    reason = forms.CharField(label="撤销原因与依据", max_length=1000,
                             widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    confirmed = forms.BooleanField(label="已核对原登记，确认撤销并保留全部历史")


@login_required
@require_http_methods(["GET", "POST"])
def reverse_trace(request, pk, record_pk, kind):
    asset = asset_or_404(request.user, asset_company_for_request(), pk)
    if not can_view_asset_p1(request.user, asset) or not can_create_asset_draft(request.user, asset.company, asset.department):
        raise PermissionDenied("无权撤销该资产的登记。")
    if kind == "origin":
        record = get_object_or_404(AssetOriginLink.objects.select_related("source_asset", "source_issued_code"),
                                   pk=record_pk, target_asset=asset, company=asset.company)
        if not can_view_asset_p1(request.user, record.source_asset) or not can_create_asset_draft(request.user, asset.company, record.source_asset.department):
            raise PermissionDenied("无权维护该来源资产。")
    else:
        record = get_object_or_404(AssetCustodyReturn, pk=record_pk, asset=asset, company=asset.company)
    form = ReverseTraceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            kwargs = {"actor": request.user, "reason": form.cleaned_data["reason"],
                      "idempotency_key": form.cleaned_data["idempotency_key"], "request": request}
            if kind == "origin":
                reverse_asset_origin(origin=record, **kwargs)
            else:
                reverse_custody_return(custody_return=record, **kwargs)
        except ValidationError as exc:
            form.add_error(None, "；".join(exc.messages))
        else:
            messages.success(request, "原登记已撤销并保留历史，可重新登记正确资料。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(request, "assets/trace_reverse.html", {"asset": asset, "record": record, "kind": kind, "form": form})
