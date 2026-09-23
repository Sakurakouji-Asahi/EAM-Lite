import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from apps.assets.forms import AssetDraftForm
from apps.coding.preview import preview_next_asset_code

PREVIEW_FIELDS = frozenset({"category", "department", "component_of", "management_attribute",
                            "coding_year", "acquisition_date", "idempotency_key"})


@login_required
@require_GET
def asset_code_preview(request):
    from apps.assets.access import asset_company_for_request
    company = asset_company_for_request()
    if set(request.GET) - PREVIEW_FIELDS:
        return JsonResponse({"message": "预览包含不支持的字段。"}, status=400)
    form = AssetDraftForm(request.GET, actor=request.user, company=company)
    for name in list(form.fields):
        if name not in PREVIEW_FIELDS:
            form.fields.pop(name)
    if not form.is_valid():
        messages = [str(message) for values in form.errors.values() for message in values]
        return JsonResponse({"message": "；".join(messages)}, status=400)
    try:
        code = preview_next_asset_code(actor=request.user, asset=form.instance)
    except ValidationError as exc:
        return JsonResponse({"message": "；".join(exc.messages)}, status=400)
    return JsonResponse({"code": code, "message": "预览不占号，实际编号以保存时分配为准。"})


class IdentificationConfirmForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, initial=uuid.uuid4, max_length=128)
    qr_token = forms.CharField(widget=forms.HiddenInput)
    method = forms.ChoiceField(label="确认方式")
    target_status = forms.ChoiceField(label="确认后状态", choices=(("in_use", "在用"), ("idle", "闲置")))
    identification_evidence = forms.CharField(
        label="核对依据", max_length=1000, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="填写许可或电子档案标识，或挂牌、铭牌、位置图等替代方式与核对情况。",
    )

    def __init__(self, *args, asset, qr, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["qr_token"].initial = qr.public_token
        self.fields["method"].choices = (
            [("electronic", "电子台账确认"), ("alternative", "替代标识确认")]
            if asset.management_attribute == "IA" else [("alternative", "替代标识确认")]
        )
        self.fields["method"].initial = self.fields["method"].choices[0][0]
        self.fields["target_status"].initial = asset.asset_status if asset.asset_status in {"in_use", "idle"} else "in_use"
        for field in self.fields.values():
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"


@login_required
@require_http_methods(["GET", "POST"])
def identification_confirm(request, pk):
    from apps.assets.access import asset_or_404, asset_company_for_request
    from apps.assets.qr_permissions import require_label_action
    from apps.assets.qr_services import confirm_label_attachment
    asset = asset_or_404(request.user, asset_company_for_request(), pk)
    require_label_action(request.user, asset)
    qr = asset.qr_identities.filter(status="active").first()
    if qr is None or (request.method == "GET" and qr.label_status == "attached"):
        return redirect("assets:asset-detail", pk=asset.pk)
    form = IdentificationConfirmForm(request.POST or None, asset=asset, qr=qr)
    if request.method == "POST" and form.is_valid():
        try:
            confirm_label_attachment(actor=request.user, asset=asset, scanned_token=form.cleaned_data["qr_token"],
                target_status=form.cleaned_data["target_status"], idempotency_key=form.cleaned_data["idempotency_key"],
                confirmation_method=form.cleaned_data["method"], identification_evidence=form.cleaned_data["identification_evidence"], request=request)
        except ValidationError as exc:
            form.add_error(None, "；".join(exc.messages))
        else:
            messages.success(request, "标识已确认，资产可以投入管理；原编号和二维码保持不变。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(request, "assets/identification_confirm.html", {"asset": asset, "form": form})
