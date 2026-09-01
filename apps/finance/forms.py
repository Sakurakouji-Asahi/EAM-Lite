"""Chinese, role-bound forms for Sprint 4 finance and depreciation."""

from __future__ import annotations

import uuid
import json
from decimal import Decimal

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.finance.models import (
    AssetFinance,
    AssetValueAdjustment,
    DepreciationMethod,
    DepreciationPolicy,
    DepreciationProfileEvent,
    PostingPeriod,
    SalvageMode,
    StartRule,
    StopRule,
)
from apps.finance.permissions import require_manage_finance
from apps.masterdata.models import AssetCategory, FixedAssetCategory


def _bootstrap_widgets(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


class FinanceBoundForm(forms.Form):
    def __init__(self, *args, actor=None, **kwargs):
        if actor is None:
            raise PermissionDenied("财务表单必须绑定当前操作用户。")
        require_manage_finance(actor)
        self.actor = actor
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class DepreciationPolicyForm(FinanceBoundForm, forms.ModelForm):
    class Meta:
        model = DepreciationPolicy
        fields = (
            "policy_key",
            "name",
            "method",
            "posting_period",
            "start_rule",
            "stop_rule",
            "default_useful_life_months",
            "default_salvage_mode",
            "default_salvage_rate",
            "default_salvage_amount",
            "annual_posting_month",
            "work_unit",
            "effective_from",
            "effective_to",
        )
        labels = {
            "policy_key": "政策稳定键",
            "name": "政策名称",
            "method": "折旧方法",
            "posting_period": "计提期间",
            "start_rule": "起算规则",
            "stop_rule": "停止规则",
            "default_useful_life_months": "默认使用年限（月）",
            "default_salvage_mode": "残值方式",
            "default_salvage_rate": "默认残值率",
            "default_salvage_amount": "默认残值金额",
            "annual_posting_month": "年度计提月",
            "work_unit": "工作量单位",
            "effective_from": "生效开始日",
            "effective_to": "生效结束日",
        }
        widgets = {
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "effective_to": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "policy_key": "用于识别同一政策的不同版本，保存后请保持稳定。",
            "default_salvage_rate": "填写 0 至 1 的小数，例如 5% 填写 0.05。",
            "default_salvage_amount": "仅选择“固定残值金额”时填写。",
            "annual_posting_month": "仅年度计提时填写 1 至 12；月度计提请留空。",
            "work_unit": "仅工作量法必填，例如“小时”或“件”。",
        }

    def __init__(self, *args, actor=None, instance=None, **kwargs):
        super().__init__(*args, actor=actor, instance=instance, **kwargs)
        if instance is not None and instance.pk and instance.status != "draft":
            for field in self.fields.values():
                field.disabled = True

    def clean(self):
        cleaned = super().clean()

        salvage_mode = cleaned.get("default_salvage_mode")
        salvage_rate = cleaned.get("default_salvage_rate")
        salvage_amount = cleaned.get("default_salvage_amount")
        if salvage_mode == SalvageMode.RATE:
            if salvage_rate is None:
                self.add_error(
                    "default_salvage_rate",
                    "残值率模式必须填写默认残值率，例如 5% 填写 0.05。",
                )
            elif salvage_rate < Decimal("0") or salvage_rate > Decimal("1"):
                self.add_error(
                    "default_salvage_rate",
                    "默认残值率必须在 0 至 1 之间，例如 5% 填写 0.05。",
                )
            cleaned["default_salvage_amount"] = None
        elif salvage_mode == SalvageMode.AMOUNT:
            if salvage_amount is None:
                self.add_error(
                    "default_salvage_amount", "固定金额模式必须填写默认残值金额。"
                )
            elif salvage_amount < Decimal("0"):
                self.add_error("default_salvage_amount", "默认残值金额不得为负数。")
            cleaned["default_salvage_rate"] = None

        posting_period = cleaned.get("posting_period")
        annual_posting_month = cleaned.get("annual_posting_month")
        useful_life_months = cleaned.get("default_useful_life_months")
        if posting_period == PostingPeriod.YEARLY:
            if annual_posting_month is None:
                self.add_error(
                    "annual_posting_month", "年度计提必须填写 1 至 12 的计提月。"
                )
            elif not 1 <= annual_posting_month <= 12:
                self.add_error("annual_posting_month", "年度计提月必须在 1 至 12 之间。")
            if useful_life_months and useful_life_months % 12:
                self.add_error(
                    "default_useful_life_months",
                    "年度计提的默认使用年限必须为 12 的整数倍。",
                )
        else:
            cleaned["annual_posting_month"] = None

        if (
            cleaned.get("method") == DepreciationMethod.SUM_OF_YEARS_DIGITS
            and useful_life_months
            and useful_life_months % 12
            and posting_period != PostingPeriod.YEARLY
        ):
            self.add_error(
                "default_useful_life_months", "年数总和法的默认使用年限必须为 12 的整数倍。"
            )
        if (
            cleaned.get("method") == DepreciationMethod.UNITS_OF_PRODUCTION
            and not str(cleaned.get("work_unit") or "").strip()
        ):
            self.add_error("work_unit", "工作量法必须填写工作量单位。")

        effective_from = cleaned.get("effective_from")
        effective_to = cleaned.get("effective_to")
        if effective_to and not effective_from:
            self.add_error("effective_from", "填写生效结束日时必须同时填写开始日。")
        elif effective_from and effective_to and effective_to < effective_from:
            self.add_error("effective_to", "生效结束日不得早于开始日。")
        return cleaned


class FixedAssetWarningAmountForm(FinanceBoundForm):
    fixed_asset_warning_amount = forms.DecimalField(
        label="固定资产认定提示金额（CNY）",
        min_value=Decimal("0"),
        max_digits=18,
        decimal_places=2,
        help_text="当前参考值为 5,000 元；该值只产生警告，不自动认定固定资产。",
    )


class FixedAssetCategoryForm(FinanceBoundForm, forms.ModelForm):
    class Meta:
        model = FixedAssetCategory
        fields = ("code", "name", "useful_life_months_default", "note")
        labels = {
            "code": "会计类别编码",
            "name": "会计类别名称",
            "useful_life_months_default": "默认使用年限（月）",
            "note": "备注",
        }
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}


class FinanceDraftForm(FinanceBoundForm):
    accounting_treatment = forms.ChoiceField(
        label="会计认定", choices=AssetFinance.AccountingTreatment.choices
    )
    accounting_treatment_reason = forms.CharField(
        label="会计认定说明", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    original_cost = forms.DecimalField(
        label="原值", min_value=Decimal("0"), max_digits=18, decimal_places=2
    )
    fixed_asset_category = forms.ModelChoiceField(
        label="固定资产会计类别", queryset=None, required=False
    )
    capitalization_date = forms.DateField(
        label="资本化日期", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    depreciation_policy = forms.ModelChoiceField(
        label="单项折旧政策", queryset=None, required=False,
        help_text="留空时按实物分类默认、公司默认顺序解析。",
    )
    useful_life_months = forms.IntegerField(
        label="使用年限（月）", min_value=1, required=False
    )
    salvage_mode = forms.ChoiceField(
        label="残值方式", choices=SalvageMode.choices, required=False
    )
    salvage_rate = forms.DecimalField(
        label="残值率", min_value=Decimal("0"), max_value=Decimal("1"),
        max_digits=12, decimal_places=8, required=False,
    )
    salvage_amount = forms.DecimalField(
        label="固定残值金额", min_value=Decimal("0"), max_digits=18,
        decimal_places=2, required=False,
    )
    method = forms.ChoiceField(
        label="折旧方法", choices=DepreciationMethod.choices, required=False
    )
    posting_period = forms.ChoiceField(
        label="计提期间", choices=PostingPeriod.choices, required=False
    )
    start_rule = forms.ChoiceField(
        label="起算规则", choices=StartRule.choices, required=False
    )
    stop_rule = forms.ChoiceField(
        label="停止规则", choices=StopRule.choices, required=False
    )
    specified_start_date = forms.DateField(
        label="指定起算日期", required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    actual_continuation_date = forms.DateField(
        label="实际接续日",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="旧资产期初余额截至后的实际接续起点；新资产留空时等于折旧起算日。",
    )
    expected_total_units = forms.DecimalField(
        label="预计总工作量", min_value=Decimal("0.000001"),
        max_digits=24, decimal_places=6, required=False,
    )
    work_unit = forms.CharField(label="工作量单位", max_length=64, required=False)
    annual_posting_month = forms.IntegerField(
        label="年度计提月", min_value=1, max_value=12, required=False
    )
    opening_actual_accumulated_depreciation = forms.DecimalField(
        label="期初实际累计折旧", min_value=Decimal("0"), max_digits=18,
        decimal_places=2, initial=Decimal("0.00"), required=False,
    )
    opening_impairment = forms.DecimalField(
        label="期初减值", min_value=Decimal("0"), max_digits=18,
        decimal_places=2, initial=Decimal("0.00"), required=False,
    )
    finance_remark = forms.CharField(
        label="财务备注", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    code_effective_date = forms.DateField(
        label="正式编号生效日期", initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    code_effective_reason = forms.CharField(
        label="历史生效日期原因", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, actor=None, company=None, asset=None, initial=None, **kwargs):
        super().__init__(*args, actor=actor, initial=initial, **kwargs)
        if company is None or asset is None:
            raise ValidationError("财务表单必须绑定公司和资产。")
        from apps.masterdata.models import FixedAssetCategory

        self.company = company
        self.asset = asset
        today = timezone.localdate()
        self.fields["fixed_asset_category"].queryset = FixedAssetCategory.objects.filter(
            company=company, is_active=True
        ).order_by("normalized_code")
        self.fields["depreciation_policy"].queryset = DepreciationPolicy.objects.filter(
            company=company,
            status=DepreciationPolicy.Status.ACTIVE,
            effective_from__lte=today,
        ).order_by("policy_key", "-version")
        if not self.is_bound and not self.initial.get("idempotency_key"):
            self.initial["idempotency_key"] = uuid.uuid4().hex

    def clean(self):
        cleaned = super().clean()
        treatment = cleaned.get("accounting_treatment")
        if treatment == AssetFinance.AccountingTreatment.FIXED_ASSET:
            required = {
                "fixed_asset_category": "固定资产认定必须选择会计类别。",
                "capitalization_date": "固定资产认定必须填写资本化日期。",
            }
            for field, message in required.items():
                if cleaned.get(field) in (None, ""):
                    self.add_error(field, message)
        elif treatment == AssetFinance.AccountingTreatment.CONTROLLED_NON_FIXED:
            for field in (
                "fixed_asset_category", "capitalization_date", "depreciation_policy",
                "useful_life_months", "salvage_mode", "salvage_rate", "salvage_amount",
                "method", "posting_period", "start_rule", "stop_rule",
                "specified_start_date", "expected_total_units", "work_unit",
                "annual_posting_month", "actual_continuation_date",
            ):
                if (
                    field not in {"fixed_asset_category", "capitalization_date"}
                    and cleaned.get(field) not in (None, "")
                ):
                    self.add_error(field, "受控非固定资产不得填写折旧配置。")
                cleaned[field] = None
            if cleaned.get("opening_actual_accumulated_depreciation") not in (None, Decimal("0")):
                self.add_error("opening_actual_accumulated_depreciation", "受控非固定资产必须为 0。")
            if cleaned.get("opening_impairment") not in (None, Decimal("0")):
                self.add_error("opening_impairment", "受控非固定资产必须为 0。")
        if cleaned.get("code_effective_date") and cleaned["code_effective_date"] > timezone.localdate():
            self.add_error("code_effective_date", "正式编号生效日期不得晚于当前上海业务日。")
        if (
            cleaned.get("code_effective_date")
            and cleaned["code_effective_date"] < timezone.localdate()
            and not str(cleaned.get("code_effective_reason") or "").strip()
        ):
            self.add_error("code_effective_reason", "历史生效日期必须填写原因。")
        if not str(cleaned.get("idempotency_key") or "").strip():
            cleaned["idempotency_key"] = uuid.uuid4().hex
        return cleaned

    def finance_data(self):
        return {
            key: self.cleaned_data.get(key)
            for key in (
                "accounting_treatment", "accounting_treatment_reason", "original_cost",
                "fixed_asset_category", "capitalization_date", "finance_remark",
            )
        }

    def profile_data(self):
        values = {}
        for key in (
            "depreciation_policy", "useful_life_months", "salvage_mode",
            "salvage_rate", "salvage_amount", "method", "posting_period",
            "start_rule", "stop_rule", "expected_total_units", "work_unit",
            "annual_posting_month", "opening_actual_accumulated_depreciation",
            "opening_impairment", "actual_continuation_date",
        ):
            value = self.cleaned_data.get(key)
            # Blank form controls mean “use the selected policy/default”.
            # Explicit Decimal zero remains a real value and is retained.
            if value not in (None, ""):
                values[key] = value
        specified = self.cleaned_data.get("specified_start_date")
        if specified is not None:
            values["specified_start"] = specified
        return values


class ConfirmFormalizationForm(FinanceDraftForm):
    action_reason = forms.CharField(
        label="财务正式化原因", widget=forms.Textarea(attrs={"rows": 3})
    )
    confirm_permanent_code = forms.BooleanField(
        label="我确认正式编号生成后永久占用，不会因更正或处置而复用。"
    )


class WorkUsageForm(FinanceBoundForm):
    period_start = forms.DateField(label="期间开始日", widget=forms.DateInput(attrs={"type": "date"}))
    period_end = forms.DateField(label="期间结束日", widget=forms.DateInput(attrs={"type": "date"}))
    current_units = forms.DecimalField(label="当期工作量", min_value=Decimal("0"), max_digits=24, decimal_places=6)
    work_unit = forms.CharField(label="工作量单位", max_length=64)
    remark = forms.CharField(label="备注", required=False, widget=forms.Textarea(attrs={"rows": 2}))


class DepreciationBatchGenerateForm(FinanceBoundForm):
    period_start = forms.DateField(label="期间开始日", widget=forms.DateInput(attrs={"type": "date"}))
    period_end = forms.DateField(label="期间结束日", widget=forms.DateInput(attrs={"type": "date"}))
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    manual_inputs_json = forms.CharField(
        label="手工折旧输入（JSON）",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=(
            '仅手工折旧需要。格式：{"资产UUID": {"amount": "100.00", '
            '"reason": "本期批准金额"}}；金额必须是十进制字符串。'
        ),
    )

    def clean_idempotency_key(self):
        return str(self.cleaned_data.get("idempotency_key") or uuid.uuid4().hex)

    def clean_manual_inputs_json(self):
        raw = str(self.cleaned_data.get("manual_inputs_json") or "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("手工折旧输入必须是合法 JSON。") from exc
        if not isinstance(value, dict):
            raise ValidationError("手工折旧输入最外层必须是对象。")
        for asset_id, item in value.items():
            try:
                uuid.UUID(str(asset_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValidationError("手工折旧输入的键必须是资产 UUID。") from exc
            if not isinstance(item, dict) or set(item) != {"amount", "reason"}:
                raise ValidationError("每项必须且只能包含 amount 和 reason。")
            if not isinstance(item["amount"], str):
                raise ValidationError("手工折旧金额必须使用十进制字符串，不能使用 JSON 浮点数。")
            try:
                amount = Decimal(item["amount"])
            except Exception as exc:
                raise ValidationError("手工折旧金额不是合法十进制数。") from exc
            if not amount.is_finite() or amount < 0:
                raise ValidationError("手工折旧金额必须是非负有限数。")
            if not str(item["reason"] or "").strip():
                raise ValidationError("手工折旧即使为 0 也必须填写原因。")
        return value


class DangerousActionForm(FinanceBoundForm):
    """Shared explicit confirmation envelope for irreversible finance POSTs."""

    reason = forms.CharField(label="操作原因", widget=forms.Textarea(attrs={"rows": 3}))
    confirm = forms.BooleanField(label="我已核对影响摘要并确认执行。")


class PolicyActionForm(DangerousActionForm):
    make_default = forms.BooleanField(label="同时设为公司默认", required=False)


class IdempotentReasonForm(FinanceBoundForm):
    reason = forms.CharField(label="原因", widget=forms.Textarea(attrs={"rows": 3}))
    confirm = forms.BooleanField(label="我确认执行此不可逆财务操作。")
    idempotency_key = forms.CharField(widget=forms.HiddenInput)


class AssetCategoryPolicyForm(FinanceBoundForm):
    category = forms.ModelChoiceField(label="实物分类", queryset=AssetCategory.objects.none())
    policy = forms.ModelChoiceField(
        label="当前默认折旧政策",
        queryset=DepreciationPolicy.objects.none(),
        required=False,
        help_text="留空表示该实物分类继续使用公司默认政策。",
    )
    reason = forms.CharField(label="分类默认政策变更原因", widget=forms.Textarea(attrs={"rows": 3}))
    confirm = forms.BooleanField(label="我已核对此变更对未来政策解析的影响。")

    def __init__(self, *args, actor=None, company=None, **kwargs):
        super().__init__(*args, actor=actor, **kwargs)
        if company is None:
            raise ValidationError("分类政策表单必须绑定公司。")
        today = timezone.localdate()
        self.fields["category"].queryset = AssetCategory.objects.filter(
            company=company, is_active=True
        ).order_by("category_level", "normalized_code")
        self.fields["policy"].queryset = DepreciationPolicy.objects.filter(
            company=company,
            status=DepreciationPolicy.Status.ACTIVE,
            effective_from__lte=today,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today)).order_by(
            "policy_key", "-version"
        )


class ProfileVersionForm(FinanceBoundForm):
    effective_from = forms.DateField(
        label="新版本生效日", widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(label="变更原因", widget=forms.Textarea(attrs={"rows": 3}))
    confirm = forms.BooleanField(label="我确认只前瞻生成新版本，不改写已确认历史。")
    method = forms.ChoiceField(label="折旧方法", choices=DepreciationMethod.choices)
    posting_period = forms.ChoiceField(label="计提期间", choices=PostingPeriod.choices)
    start_rule = forms.ChoiceField(label="起算规则", choices=StartRule.choices)
    stop_rule = forms.ChoiceField(label="停止规则", choices=StopRule.choices)
    useful_life_months = forms.IntegerField(label="使用年限（月）", min_value=1)
    salvage_mode = forms.ChoiceField(label="残值方式", choices=SalvageMode.choices)
    salvage_rate = forms.DecimalField(
        label="残值率", required=False, min_value=Decimal("0"), max_value=Decimal("1"),
        max_digits=12, decimal_places=8,
    )
    salvage_amount = forms.DecimalField(
        label="固定残值金额", required=False, min_value=Decimal("0"),
        max_digits=18, decimal_places=2,
    )
    expected_total_units = forms.DecimalField(
        label="预计总工作量", required=False, min_value=Decimal("0.000001"),
        max_digits=24, decimal_places=6,
    )
    work_unit = forms.CharField(label="工作量单位", required=False, max_length=64)
    annual_posting_month = forms.IntegerField(
        label="年度计提月", required=False, min_value=1, max_value=12
    )

    def clean(self):
        cleaned = super().clean()
        salvage_mode = cleaned.get("salvage_mode")
        if salvage_mode == SalvageMode.RATE:
            if cleaned.get("salvage_rate") is None:
                self.add_error("salvage_rate", "残值率模式必须填写残值率。")
            cleaned["salvage_amount"] = None
        elif salvage_mode == SalvageMode.AMOUNT:
            if cleaned.get("salvage_amount") is None:
                self.add_error("salvage_amount", "固定金额模式必须填写残值金额。")
            cleaned["salvage_rate"] = None
        if cleaned.get("posting_period") == PostingPeriod.YEARLY:
            if cleaned.get("annual_posting_month") is None:
                self.add_error("annual_posting_month", "年度计提必须填写计提月。")
        else:
            cleaned["annual_posting_month"] = None
        if cleaned.get("method") == DepreciationMethod.UNITS_OF_PRODUCTION:
            if cleaned.get("expected_total_units") is None:
                self.add_error("expected_total_units", "工作量法必须填写预计总工作量。")
            if not str(cleaned.get("work_unit") or "").strip():
                self.add_error("work_unit", "工作量法必须填写工作量单位。")
        else:
            cleaned["expected_total_units"] = None
            cleaned["work_unit"] = ""
        # Estimate changes always start prospectively at the explicitly
        # approved month boundary.  The service owns this field; callers do
        # not get to restart the asset from current/next commissioning rules.
        cleaned.pop("start_rule", None)
        return cleaned


class TheoreticalRunForm(FinanceBoundForm):
    as_of_date = forms.DateField(
        label="试算截至日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    parameters_json = forms.CharField(
        label="计算参数 JSON",
        widget=forms.Textarea(attrs={"rows": 12}),
        help_text="使用计算引擎 ScheduleInput 的字段，金额和比率均填写十进制字符串。",
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)

    def clean_parameters_json(self):
        try:
            value = json.loads(self.cleaned_data["parameters_json"])
        except json.JSONDecodeError as exc:
            raise ValidationError("计算参数必须是合法 JSON。") from exc
        if not isinstance(value, dict):
            raise ValidationError("计算参数最外层必须是对象。")
        for field in (
            "commissioning_date",
            "specified_start",
            "actual_continuation_date",
            "stop_date",
        ):
            if field in value and value[field] is not None:
                try:
                    value[field] = forms.DateField().to_python(value[field])
                except ValidationError as exc:
                    raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期。") from exc
        suspensions = value.get("suspensions")
        if suspensions is not None:
            if not isinstance(suspensions, list):
                raise ValidationError("suspensions 必须是日期区间数组。")
            converted = []
            for interval in suspensions:
                if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                    raise ValidationError("suspensions 每项必须是 [开始日, 结束日]。")
                try:
                    converted.append(tuple(forms.DateField().to_python(item) for item in interval))
                except ValidationError as exc:
                    raise ValidationError("suspensions 中的日期必须为 YYYY-MM-DD。") from exc
            value["suspensions"] = converted
        return value

    def clean_idempotency_key(self):
        return str(self.cleaned_data.get("idempotency_key") or uuid.uuid4().hex)


class ReasonForm(FinanceBoundForm):
    reason = forms.CharField(label="原因", widget=forms.Textarea(attrs={"rows": 3}))
    confirm = forms.BooleanField(label="我确认执行此不可逆财务操作。")


class ProfileEventForm(ReasonForm):
    event_type = forms.ChoiceField(label="事件类型", choices=DepreciationProfileEvent.EventType.choices)
    effective_date = forms.DateField(label="生效日期", widget=forms.DateInput(attrs={"type": "date"}))


class ProfileContinuationReviewForm(ReasonForm):
    actual_continuation_date = forms.DateField(
        label="实际接续日", widget=forms.DateInput(attrs={"type": "date"})
    )


class ValueAdjustmentForm(ReasonForm):
    adjustment_type = forms.ChoiceField(label="调整类型", choices=AssetValueAdjustment.AdjustmentType.choices)
    amount = forms.DecimalField(label="调整金额", max_digits=18, decimal_places=2)
    effective_date = forms.DateField(label="生效日期", widget=forms.DateInput(attrs={"type": "date"}))
