import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.assets.custody_services import return_custody_asset
from apps.assets.permissions import can_create_asset_draft
from apps.assets.access import asset_or_404, asset_company_for_request


class CustodyReturnForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, initial=uuid.uuid4, max_length=128)
    returned_on = forms.DateField(label="实际归还日期", initial=timezone.localdate, widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    counterparty = forms.CharField(label="接收单位或接收人", max_length=200)
    contract_reference = forms.CharField(label="合同或受托依据", max_length=200)
    acceptance_evidence = forms.CharField(label="归还验收依据", max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    reason = forms.CharField(label="归还原因", max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "form-control"


@login_required
@require_http_methods(["GET", "POST"])
def custody_return(request, pk):
    asset = asset_or_404(request.user, asset_company_for_request(), pk)
    if not can_create_asset_draft(request.user, asset.company, asset.department):
        raise PermissionDenied("无权办理此资产的实物归还。")
    form = CustodyReturnForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            return_custody_asset(actor=request.user, asset=asset, request=request, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, "；".join(exc.messages))
        else:
            messages.success(request, "归还已记录，原编号、二维码及历史资料保留。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(request, "assets/custody_return.html", {"asset": asset, "form": form})
