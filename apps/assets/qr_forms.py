"""Chinese forms for Sprint 6 QR labels."""

import uuid

from django import forms
from django.core.exceptions import ValidationError


class LabelPrintForm(forms.Form):
    asset_ids = forms.MultipleChoiceField(
        label="资产", widget=forms.CheckboxSelectMultiple
    )
    include_responsible_employee = forms.BooleanField(label="显示责任人", required=False)
    include_location = forms.BooleanField(label="显示位置", required=False)
    include_model = forms.BooleanField(label="显示型号", required=False)
    explicit_reprint = forms.BooleanField(label="明确重新打印", required=False)
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, assets=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset_ids"].choices = [
            (str(asset.pk), f"{asset.asset_code} {asset.asset_name}")
            for asset in assets
        ]
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4().hex)

    def clean_idempotency_key(self):
        return (self.cleaned_data.get("idempotency_key") or uuid.uuid4().hex).strip()


class PrintResultForm(forms.Form):
    failure_reason = forms.CharField(label="失败说明", widget=forms.Textarea)

    def clean_failure_reason(self):
        value = (self.cleaned_data.get("failure_reason") or "").strip()
        if not value:
            raise ValidationError("取消打印必须填写失败说明。")
        return value


class SingleLabelPrintForm(forms.Form):
    idempotency_key = forms.CharField(
        max_length=128, widget=forms.HiddenInput, required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4().hex)

    def clean_idempotency_key(self):
        return (self.cleaned_data.get("idempotency_key") or uuid.uuid4().hex).strip()


class TokenRotationForm(forms.Form):
    REASONS = (
        ("damaged", "损坏"),
        ("lost", "遗失"),
        ("unscannable", "无法扫描"),
        ("information_reprint", "信息重打"),
        ("other", "其他"),
    )
    reason = forms.ChoiceField(label="换标原因", choices=REASONS)
    explanation = forms.CharField(label="说明", widget=forms.Textarea)

    def clean_explanation(self):
        value = (self.cleaned_data.get("explanation") or "").strip()
        if not value:
            raise ValidationError("换标说明不能为空。")
        return value


class LabelAttachmentForm(forms.Form):
    scanned_token = forms.CharField(label="当前二维码", widget=forms.HiddenInput)
    opaque_origin_bridge = forms.CharField(widget=forms.HiddenInput, required=False)
    label_attached = forms.BooleanField(label="已将此标签贴在该实物上")
    responsibility_confirmed = forms.BooleanField(label="已核对部门、责任人和位置")
    target_status = forms.ChoiceField(
        label="启用状态", choices=(("in_use", "在用"), ("idle", "闲置")), required=False
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, first_attachment=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.first_attachment = first_attachment
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4().hex)

    def clean(self):
        cleaned = super().clean()
        if self.first_attachment and not cleaned.get("target_status"):
            self.add_error("target_status", "首次贴标必须选择在用或闲置。")
        cleaned["idempotency_key"] = (
            cleaned.get("idempotency_key") or uuid.uuid4().hex
        ).strip()
        return cleaned


class WebLabelAttachmentForm(LabelAttachmentForm):
    """Explicit Web confirmation without exposing the QR token in form HTML."""

    qr_identity_id = forms.UUIDField(label="当前二维码身份", widget=forms.HiddenInput)

    def __init__(self, *args, qr_identity_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("scanned_token", None)
        if not self.is_bound and qr_identity_id is not None:
            self.initial["qr_identity_id"] = qr_identity_id
