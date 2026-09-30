"""Business-facing forms and read-only review of existing finance calculations."""
from datetime import timedelta
from decimal import Decimal
import re

from django import forms
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.finance.forms import FinanceBoundForm, TheoreticalRunForm, _bootstrap_widgets
from apps.finance.models import DepreciationMethod, PostingPeriod, SalvageMode, StartRule
from apps.finance.permissions import can_manage_finance


class BatchItemFilterForm(forms.Form):
    q = forms.CharField(label="资产编号、设备编号或名称", required=False, max_length=200)
    status = forms.ChoiceField(label="试算结果", required=False, choices=(
        ("", "全部明细"), ("error", "错误"), ("ready", "可确认"), ("skipped", "跳过"),
    ))
    page_size = forms.TypedChoiceField(label="每页显示", required=False, coerce=int,
        choices=((25, "25 条"), (50, "50 条"), (100, "100 条"), (200, "200 条")))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


def batch_review_context(request, batch, *, confirm_form=None):
    from apps.finance.forms import DangerousActionForm

    items = batch.items.select_related("asset")
    totals = items.aggregate(
        count=Count("pk"), ready=Count("pk", filter=Q(status="ready")),
        errors=Count("pk", filter=Q(status="error")), skipped=Count("pk", filter=Q(status="skipped")),
        amount=Sum("planned_amount", filter=Q(status="ready")),
    )
    totals['effect_amount'] = (-totals['amount'] if batch.batch_type == 'reversal' else totals['amount']) if totals['amount'] is not None else None
    form = BatchItemFilterForm(request.GET)
    valid = form.is_valid()
    if valid:
        query = form.cleaned_data["q"]
        if query:
            items = items.filter(Q(asset__asset_code__icontains=query) | Q(asset__equipment_number__icontains=query)
                                 | Q(asset__asset_name__icontains=query))
        if form.cleaned_data["status"]:
            items = items.filter(status=form.cleaned_data["status"])
    else:
        items = items.none()
    page = Paginator(items.order_by("asset__asset_code", "pk"), form.cleaned_data.get("page_size") or 25).get_page(request.GET.get("page"))
    for item in page:
        item.display_amount = -item.planned_amount if batch.batch_type == 'reversal' and item.planned_amount else item.planned_amount
    query = request.GET.copy()
    query.pop("page", None)
    can_manage = can_manage_finance(request.user)
    return {
        "batch": batch, "items": page.object_list, "page_obj": page, "pagination_query": query.urlencode(),
        "filter_form": form, "batch_totals": totals, "can_manage": can_manage, "filters_valid": valid,
        "confirm_form": confirm_form if confirm_form is not None else (
            DangerousActionForm(actor=request.user) if can_manage and batch.status == "draft" else None
        ),
    }


class ManualAmountForm(forms.Form):
    amount = forms.DecimalField(label="本期金额", required=False, max_digits=18, decimal_places=2,
                                min_value=Decimal("0"))
    reason = forms.CharField(label="原因", required=False, max_length=2000)

    def __init__(self, *args, asset, **kwargs):
        self.asset = asset
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)
        for name,field in self.fields.items():
            field.widget.attrs['aria-label'] = f'{asset.asset_name} {field.label}'

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("amount") is not None and not cleaned.get("reason"):
            self.add_error("reason", "手工金额为 0 时也必须填写原因。")
        if cleaned.get("reason") and cleaned.get("amount") is None:
            self.add_error("amount", "请明确填写本期金额，包括 0。")
        return cleaned


class TheoreticalBusinessForm(TheoreticalRunForm):
    parameters_json = forms.CharField(required=False, widget=forms.HiddenInput)
    original_cost = forms.DecimalField(label="原值", max_digits=18, decimal_places=2, min_value=0)
    method = forms.ChoiceField(label="试算方法", choices=[choice for choice in DepreciationMethod.choices
        if choice[0] not in {"manual", "units_of_production"}],
        help_text="工作量法和手工折旧需实际工作量或逐期金额，请在月度折旧批次办理。")
    posting_period = forms.ChoiceField(label="计提周期", choices=PostingPeriod.choices)
    commissioning_date = forms.DateField(label="投入使用日期", widget=forms.DateInput(attrs={"type":"date"}))
    start_rule = forms.ChoiceField(label="起算规则", choices=StartRule.choices)
    specified_start = forms.DateField(label="指定起算日", required=False, widget=forms.DateInput(attrs={"type":"date"}))
    useful_life_months = forms.IntegerField(label="使用寿命（月）", min_value=1)
    salvage_mode = forms.ChoiceField(label="残值方式", choices=SalvageMode.choices)
    salvage_percent = forms.DecimalField(label="残值率（%）", required=False, min_value=0, max_value=100, decimal_places=6, max_digits=9)
    salvage_amount = forms.DecimalField(label="残值金额", required=False, min_value=0, decimal_places=2, max_digits=18)
    annual_posting_month = forms.IntegerField(label="年度计提月", required=False, min_value=1, max_value=12)
    opening_actual_accumulated_depreciation = forms.DecimalField(label="期初累计折旧", min_value=0, max_digits=18, decimal_places=2)
    opening_impairment = forms.DecimalField(label="期初减值", min_value=0, max_digits=18, decimal_places=2)
    opening_book_value = forms.DecimalField(label="期初账面净值", min_value=0, max_digits=18, decimal_places=2)
    actual_continuation_date = forms.DateField(label="实际接续日", required=False, widget=forms.DateInput(attrs={"type":"date"}))
    stop_date = forms.DateField(label="停止计提日（不含当天）", required=False, widget=forms.DateInput(attrs={"type":"date"}))
    suspensions_text = forms.CharField(label="暂停期间（选填）", required=False, widget=forms.Textarea(attrs={"rows":3}),
        help_text="每行粘贴开始日期、结束日期两列，日期为 YYYY-MM-DD，包含结束当天。")

    def __init__(self, *args, asset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound and self.data.get("parameters_json"):
            for name, field in self.fields.items():
                if name not in {"as_of_date", "parameters_json", "idempotency_key"}:
                    field.required = False
        if not self.is_bound and asset is not None:
            finance = asset.finance
            profile = asset.depreciation_profiles.order_by("-effective_from", "-version").first()
            self.initial.update(as_of_date=timezone.localdate(), original_cost=finance.original_cost,
                commissioning_date=asset.commissioning_date)
            if profile:
                for field in ("method", "posting_period", "start_rule", "useful_life_months", "salvage_mode",
                              "salvage_amount", "annual_posting_month", "opening_actual_accumulated_depreciation",
                              "opening_book_value", "actual_continuation_date"):
                    self.initial[field] = getattr(profile, field)
                self.initial["specified_start"] = profile.start_date
                self.initial["salvage_percent"] = profile.salvage_rate * 100 if profile.salvage_rate is not None else None
                self.initial["opening_impairment"] = finance.original_cost - profile.opening_actual_accumulated_depreciation - profile.opening_book_value

    def clean_parameters_json(self):
        return super().clean_parameters_json() if self.cleaned_data.get("parameters_json") else None

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("parameters_json") is not None or self.errors:
            return cleaned
        params = {name: cleaned[name] for name in (
            "original_cost", "method", "posting_period", "commissioning_date", "start_rule", "useful_life_months",
            "salvage_mode", "specified_start", "opening_actual_accumulated_depreciation", "opening_impairment",
            "opening_book_value", "actual_continuation_date", "stop_date",
        )}
        params["annual_posting_month"] = cleaned.get("annual_posting_month") if cleaned["posting_period"] == "yearly" else None
        params["salvage_rate"] = cleaned["salvage_percent"] / 100 if cleaned["salvage_mode"] == "rate" and cleaned.get("salvage_percent") is not None else None
        params["salvage_amount"] = cleaned.get("salvage_amount") if cleaned["salvage_mode"] == "amount" else None
        intervals = []
        for number, line in enumerate(cleaned.get("suspensions_text", "").splitlines(), 1):
            if not line.strip():
                continue
            try:
                parts = re.split(r"[\t,，]+", line.strip())
                if len(parts) != 2:
                    raise ValueError
                start, end = [forms.DateField().clean(value.strip()) for value in parts]
                if end < start:
                    raise ValueError
                intervals.append((start, end + timedelta(days=1)))
            except (ValueError, OverflowError, forms.ValidationError):
                self.add_error("suspensions_text", f"第 {number} 行需要有效的开始、结束日期，结束不得早于开始。")
        params["suspensions"] = tuple(intervals)
        if not self.errors:
            from apps.finance.domain import ScheduleInput, generate_schedule
            try:
                generate_schedule(ScheduleInput(**params))
            except (ValueError, TypeError, OverflowError) as exc:
                self.add_error(None, str(exc))
        cleaned["parameters_json"] = params
        return cleaned
