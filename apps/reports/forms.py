"""Fixed report filters; no arbitrary field names or SQL are accepted."""

from __future__ import annotations

from datetime import date

from django import forms

from apps.reports.schemas import report_choices


ASSET_STATUS_CHOICES = (
    ("", "全部状态"),
    ("draft", "草稿"), ("pending_finance", "待财务确认"),
    ("pending_label", "待贴标"), ("in_use", "在用"),
    ("idle", "闲置"), ("loaned", "借出"),
    ("under_repair", "维修中"), ("pending_disposal", "处置处理中"),
    ("disposed", "已报废"), ("sold", "已出售"),
    ("other_disposed", "已其他处置"),
)

ASSET_SCOPE_CHOICES = (("", "默认范围"), ("managed", "仅在管资产"))
LABEL_SCOPE_CHOICES = (("", "全部标签状态"), ("not_attached", "尚未贴标"))
MAINTENANCE_DUE_SCOPE_CHOICES = (
    ("", "全部到期状态"),
    ("upcoming", "即将到期（含今日）"),
    ("overdue", "逾期"),
)


class ReportFilterForm(forms.Form):
    report_type = forms.ChoiceField(label="报表类型", choices=())
    as_of_date = forms.DateField(label="基准日期", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    period_start = forms.DateField(label="期间开始", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    period_end = forms.DateField(label="期间结束", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    department = forms.IntegerField(label="部门", required=False, min_value=1)
    category = forms.IntegerField(label="实物分类", required=False, min_value=1)
    fixed_asset_category = forms.IntegerField(label="固定资产会计类别", required=False, min_value=1)
    responsible_employee = forms.IntegerField(label="责任人", required=False, min_value=1)
    asset_status = forms.ChoiceField(label="资产状态", required=False, choices=ASSET_STATUS_CHOICES)
    asset_scope = forms.ChoiceField(label="资产范围", required=False, choices=ASSET_SCOPE_CHOICES)
    label_scope = forms.ChoiceField(label="标签范围", required=False, choices=LABEL_SCOPE_CHOICES)
    maintenance_due_scope = forms.ChoiceField(
        label="保养到期范围", required=False, choices=MAINTENANCE_DUE_SCOPE_CHOICES
    )
    include_drafts = forms.BooleanField(label="纳入草稿", required=False)
    include_disposed = forms.BooleanField(label="纳入已处置资产", required=False, initial=True)

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.assets.permissions import can_view_financial_fields
        from apps.reports.permissions import can_view_report

        self.actor = actor
        self.fields["report_type"].choices = tuple(
            choice for choice in report_choices(include_tplus=False)
            if actor is None or can_view_report(actor, choice[0])
        )
        if actor is not None and not can_view_financial_fields(actor):
            self.fields.pop("fixed_asset_category", None)

    def clean(self):
        cleaned = super().clean()
        from apps.assets.permissions import can_view_financial_fields

        if (
            self.actor is not None
            and not can_view_financial_fields(self.actor)
            and self.data.get("fixed_asset_category") not in (None, "")
        ):
            raise forms.ValidationError("您无权使用固定资产会计类别筛选。")
        start, end = cleaned.get("period_start"), cleaned.get("period_end")
        if bool(start) != bool(end):
            raise forms.ValidationError("期间开始和期间结束必须同时填写。")
        if start and end and end < start:
            raise forms.ValidationError("期间结束不得早于期间开始。")
        return cleaned


class TplusExportForm(forms.Form):
    period = forms.CharField(label="会计期间", max_length=7, help_text="YYYY-MM")
    department = forms.IntegerField(label="部门", required=False, min_value=1)
    category = forms.IntegerField(label="实物分类", required=False, min_value=1)
    fixed_asset_category = forms.IntegerField(label="固定资产会计类别", required=False, min_value=1)
    include_disposed = forms.BooleanField(label="纳入本期有活动的已处置资产", required=False, initial=True)
    idempotency_key = forms.CharField(widget=forms.HiddenInput, max_length=128)

    def clean_period(self):
        raw = self.cleaned_data["period"].strip()
        try:
            year, month = (int(part) for part in raw.split("-", 1))
            date(year, month, 1)
        except (TypeError, ValueError):
            raise forms.ValidationError("会计期间必须为 YYYY-MM。")
        return f"{year:04d}-{month:02d}"


class ExternalReferenceForm(forms.Form):
    reference_value = forms.CharField(label="T+资产卡片编码", max_length=128)
    note = forms.CharField(label="备注", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    reason = forms.CharField(label="新增/更正原因", widget=forms.Textarea(attrs={"rows": 2}))

    def clean_reference_value(self):
        from apps.masterdata.normalization import clean_display_identifier

        value = clean_display_identifier(self.cleaned_data["reference_value"])
        if not value:
            raise forms.ValidationError("T+资产卡片编码不得为空。")
        return value

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if not reason:
            raise forms.ValidationError("必须填写新增/更正原因。")
        return reason


__all__ = ["ExternalReferenceForm", "ReportFilterForm", "TplusExportForm"]
