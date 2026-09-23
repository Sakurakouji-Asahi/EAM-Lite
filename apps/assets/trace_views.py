import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.assets.models import Asset
from apps.assets.permissions import can_create_asset_draft, scoped_assets_p1
from apps.assets.trace_services import normalize_members, record_asset_composition, record_asset_origin
from apps.assets.access import asset_or_404, asset_company_for_request


class OriginForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, initial=uuid.uuid4, max_length=128)
    source = forms.ModelChoiceField(label="来源资产", queryset=Asset.objects.none())
    relation_type = forms.ChoiceField(label="来源关系", choices=(("split", "由来源资产拆分"), ("merge", "由来源资产合并")))
    reason = forms.CharField(label="依据说明", max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, actor, asset, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source"].queryset = scoped_assets_p1(actor, asset.company, Asset.objects.filter(
            record_status="active", current_issued_code__isnull=False,
        )).exclude(pk=asset.pk).order_by("asset_code")
        self.fields["source"].label_from_instance = lambda item: f"{item.asset_code} · {item.asset_name}"
        _style(self)


class CompositionForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, initial=uuid.uuid4, max_length=128)
    members = forms.CharField(label="成员清单", max_length=100000, widget=forms.Textarea(attrs={"rows": 8}),
        help_text="每行填写：名称 | 数量 | 单位 | 说明（可留空）。例如：办公椅 | 4 | 把 | 蓝色")
    reason = forms.CharField(label="本次核对或调整依据", max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, asset, **kwargs):
        super().__init__(*args, **kwargs)
        latest = asset.composition_revisions.first()
        if latest:
            self.fields["members"].initial = "\n".join(" | ".join(row[key] for key in ("name", "quantity", "unit", "note")) for row in latest.members)
        _style(self)

    def clean_members(self):
        members = []
        for number, line in enumerate(self.cleaned_data["members"].splitlines(), 1):
            if not line.strip():
                continue
            values = [value.strip() for value in line.split("|", 3)]
            if len(values) < 3:
                raise ValidationError(f"第 {number} 行请用 | 分隔名称、数量和单位。")
            members.append(dict(zip(("name", "quantity", "unit", "note"), values + [""] * (4 - len(values)))))
        return normalize_members(members)


def _style(form):
    for field in form.fields.values():
        if not isinstance(field.widget, forms.HiddenInput):
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"


@login_required
@require_http_methods(["GET", "POST"])
def asset_trace_edit(request, pk, kind):
    asset = asset_or_404(request.user, asset_company_for_request(), pk)
    if not can_create_asset_draft(request.user, asset.company, asset.department):
        raise PermissionDenied("无权维护该资产的来源或组合资料。")
    if kind == "origin":
        form = OriginForm(request.POST or None, actor=request.user, asset=asset)
    else:
        form = CompositionForm(request.POST or None, asset=asset)
    if request.method == "POST" and form.is_valid():
        try:
            common = {"actor": request.user, "reason": form.cleaned_data["reason"],
                      "idempotency_key": form.cleaned_data["idempotency_key"], "request": request}
            if kind == "origin":
                record_asset_origin(source=form.cleaned_data["source"], target=asset,
                    relation_type=form.cleaned_data["relation_type"], **common)
            else:
                record_asset_composition(asset=asset, members=form.cleaned_data["members"], **common)
        except ValidationError as exc:
            form.add_error(None, "；".join(exc.messages))
        else:
            messages.success(request, "来源记录已保存，原资产停用及财务处理仍需按各自流程办理。" if kind == "origin" else "组合清单已保存为新版本，原清单保留。")
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(request, "assets/trace_edit.html", {"asset": asset, "form": form, "kind": kind})
