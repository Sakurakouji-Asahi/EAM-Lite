from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


MONEY = {"max_digits": 18, "decimal_places": 2}
RATE = {"max_digits": 12, "decimal_places": 8}
UNITS = {"max_digits": 24, "decimal_places": 6}
UNROUNDED = {"max_digits": 30, "decimal_places": 12}


class ProtectedFinanceQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("财务历史记录不得批量删除。")


class DepreciationMethod(models.TextChoices):
    STRAIGHT_LINE = "straight_line", "年限平均法"
    UNITS_OF_PRODUCTION = "units_of_production", "工作量法"
    DOUBLE_DECLINING_BALANCE = "double_declining_balance", "双倍余额递减法"
    SUM_OF_YEARS_DIGITS = "sum_of_years_digits", "年数总和法"
    MANUAL = "manual", "手工折旧"
    NO_DEPRECIATION = "no_depreciation", "不计提折旧"


class PostingPeriod(models.TextChoices):
    MONTHLY = "monthly", "月度"
    YEARLY = "yearly", "年度"


class StartRule(models.TextChoices):
    CURRENT_MONTH = "current_month", "当月"
    NEXT_MONTH = "next_month", "次月"
    SPECIFIED_MONTH = "specified_month", "指定月份"
    SPECIFIED_DATE = "specified_date", "指定日期"


class StopRule(models.TextChoices):
    EVENT_DATE = "event_date", "事件日"
    NEXT_MONTH = "next_month", "次月"


class SalvageMode(models.TextChoices):
    RATE = "rate", "残值率"
    AMOUNT = "amount", "固定残值金额"


class AssetFinance(models.Model):
    class AccountingTreatment(models.TextChoices):
        FIXED_ASSET = "fixed_asset", "固定资产"
        CONTROLLED_NON_FIXED = "controlled_non_fixed", "受控非固定资产"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company", on_delete=models.PROTECT, related_name="asset_finances"
    )
    asset = models.OneToOneField(
        "assets.Asset", on_delete=models.PROTECT, related_name="finance"
    )
    accounting_treatment = models.CharField(
        max_length=32,
        choices=AccountingTreatment.choices,
        null=True,
        blank=True,
    )
    accounting_treatment_reason = models.TextField(blank=True)
    recognition_threshold_snapshot = models.DecimalField(
        null=True, blank=True, **MONEY
    )
    fixed_asset_category = models.ForeignKey(
        "masterdata.FixedAssetCategory",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_finances",
    )
    original_cost = models.DecimalField(null=True, blank=True, **MONEY)
    capitalization_date = models.DateField(null=True, blank=True)
    impairment_balance_cache = models.DecimalField(
        default=0, editable=False, **MONEY
    )
    finance_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_asset_finances",
    )
    finance_confirmed_at = models.DateTimeField(null=True, blank=True)
    finance_remark = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(accounting_treatment__isnull=True)
                | Q(accounting_treatment__in=("fixed_asset", "controlled_non_fixed")),
                name="ck_asset_finance_treatment",
            ),
            models.CheckConstraint(
                condition=Q(original_cost__isnull=True) | Q(original_cost__gte=0),
                name="ck_asset_finance_cost_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(recognition_threshold_snapshot__isnull=True)
                | Q(recognition_threshold_snapshot__gte=0),
                name="ck_asset_finance_threshold_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(impairment_balance_cache__gte=0),
                name="ck_asset_finance_impairment_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        finance_confirmed_by__isnull=True,
                        finance_confirmed_at__isnull=True,
                        recognition_threshold_snapshot__isnull=True,
                    )
                    | Q(
                        finance_confirmed_at__isnull=False,
                        recognition_threshold_snapshot__isnull=False,
                        accounting_treatment__isnull=False,
                        original_cost__isnull=False,
                    )
                ),
                name="ck_asset_finance_confirmation_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(finance_confirmed_at__isnull=False, accounting_treatment="fixed_asset")
                    | Q(
                        fixed_asset_category__isnull=False,
                        capitalization_date__isnull=False,
                    )
                ),
                name="ck_asset_finance_fixed_confirmed",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(accounting_treatment="controlled_non_fixed")
                    | Q(
                        fixed_asset_category__isnull=True,
                        impairment_balance_cache=0,
                    )
                ),
                name="ck_asset_finance_nonfixed_fields",
            ),
        ]

    def __str__(self):
        return str(self.asset)


class DepreciationPolicy(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        ACTIVE = "active", "生效"
        RETIRED = "retired", "历史版本"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="depreciation_policies",
    )
    policy_key = models.CharField(max_length=100)
    version = models.PositiveIntegerField()
    name = models.CharField(max_length=200)
    method = models.CharField(max_length=32, choices=DepreciationMethod.choices)
    posting_period = models.CharField(max_length=16, choices=PostingPeriod.choices)
    start_rule = models.CharField(max_length=32, choices=StartRule.choices)
    stop_rule = models.CharField(max_length=16, choices=StopRule.choices)
    default_useful_life_months = models.PositiveIntegerField()
    default_salvage_mode = models.CharField(
        max_length=16, choices=SalvageMode.choices
    )
    default_salvage_rate = models.DecimalField(null=True, blank=True, **RATE)
    default_salvage_amount = models.DecimalField(null=True, blank=True, **MONEY)
    annual_posting_month = models.PositiveSmallIntegerField(null=True, blank=True)
    work_unit = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    is_default = models.BooleanField(default=False)
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)
    previous_version = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="next_versions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_depreciation_policies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("company_id", "policy_key", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "policy_key", "version"),
                name="uq_depr_policy_company_key_version",
            ),
            models.UniqueConstraint(
                fields=("company",),
                condition=Q(status="active", is_default=True),
                name="uq_depr_policy_active_default",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="ck_depr_policy_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(default_useful_life_months__gt=0),
                name="ck_depr_policy_life_positive",
            ),
            models.CheckConstraint(
                condition=Q(method__in=DepreciationMethod.values),
                name="ck_depr_policy_method_valid",
            ),
            models.CheckConstraint(
                condition=Q(posting_period__in=PostingPeriod.values),
                name="ck_depr_policy_posting_valid",
            ),
            models.CheckConstraint(
                condition=Q(start_rule__in=StartRule.values),
                name="ck_depr_policy_start_valid",
            ),
            models.CheckConstraint(
                condition=Q(stop_rule__in=StopRule.values),
                name="ck_depr_policy_stop_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("draft", "active", "retired")),
                name="ck_depr_policy_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        default_salvage_mode="rate",
                        default_salvage_rate__isnull=False,
                        default_salvage_rate__gte=0,
                        default_salvage_rate__lte=1,
                        default_salvage_amount__isnull=True,
                    )
                    | Q(
                        default_salvage_mode="amount",
                        default_salvage_rate__isnull=True,
                        default_salvage_amount__isnull=False,
                        default_salvage_amount__gte=0,
                    )
                ),
                name="ck_depr_policy_salvage_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        posting_period="yearly",
                        annual_posting_month__gte=1,
                        annual_posting_month__lte=12,
                    )
                    | Q(posting_period="monthly", annual_posting_month__isnull=True)
                ),
                name="ck_depr_policy_period_fields",
            ),
            models.CheckConstraint(
                condition=~Q(method="units_of_production") | ~Q(work_unit=""),
                name="ck_depr_policy_work_unit",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_from__isnull=False, effective_to__gte=F("effective_from")),
                name="ck_depr_policy_effective_dates",
            ),
            models.CheckConstraint(
                condition=~Q(status="active") | Q(effective_from__isnull=False),
                name="ck_depr_policy_active_from",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(status="active"),
                name="ck_depr_policy_default_active",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("previous_version_id")),
                name="ck_depr_policy_not_self_previous",
            ),
        ]

    def __str__(self):
        return f"{self.name} v{self.version}"


class AssetDepreciationProfile(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        ACTIVE = "active", "生效"
        SUSPENDED = "suspended", "暂停"
        COMPLETED = "completed", "已完成"
        STOPPED = "stopped", "已停止"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="depreciation_profiles",
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="depreciation_profiles"
    )
    depreciation_policy = models.ForeignKey(
        DepreciationPolicy,
        on_delete=models.PROTECT,
        related_name="asset_profiles",
    )
    version = models.PositiveIntegerField()
    method = models.CharField(max_length=32, choices=DepreciationMethod.choices)
    posting_period = models.CharField(max_length=16, choices=PostingPeriod.choices)
    start_rule = models.CharField(max_length=32, choices=StartRule.choices)
    stop_rule = models.CharField(max_length=16, choices=StopRule.choices)
    start_date = models.DateField()
    useful_life_months = models.PositiveIntegerField()
    salvage_mode = models.CharField(max_length=16, choices=SalvageMode.choices)
    salvage_rate = models.DecimalField(null=True, blank=True, **RATE)
    salvage_amount = models.DecimalField(null=True, blank=True, **MONEY)
    opening_book_value = models.DecimalField(**MONEY)
    opening_actual_accumulated_depreciation = models.DecimalField(default=0, **MONEY)
    expected_total_units = models.DecimalField(null=True, blank=True, **UNITS)
    work_unit = models.CharField(max_length=64, blank=True)
    annual_posting_month = models.PositiveSmallIntegerField(null=True, blank=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    change_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_depreciation_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("asset_id", "version")
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "version"), name="uq_depr_profile_asset_version"
            ),
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(status__in=("active", "suspended")),
                name="uq_depr_profile_asset_current",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="ck_depr_profile_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(useful_life_months__gt=0),
                name="ck_depr_profile_life_positive",
            ),
            models.CheckConstraint(
                condition=Q(method__in=DepreciationMethod.values),
                name="ck_depr_profile_method_valid",
            ),
            models.CheckConstraint(
                condition=Q(posting_period__in=PostingPeriod.values),
                name="ck_depr_profile_posting_valid",
            ),
            models.CheckConstraint(
                condition=Q(start_rule__in=StartRule.values),
                name="ck_depr_profile_start_valid",
            ),
            models.CheckConstraint(
                condition=Q(stop_rule__in=StopRule.values),
                name="ck_depr_profile_stop_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("draft", "active", "suspended", "completed", "stopped")),
                name="ck_depr_profile_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(opening_book_value__gte=0)
                & Q(opening_actual_accumulated_depreciation__gte=0),
                name="ck_depr_profile_opening_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        salvage_mode="rate",
                        salvage_rate__isnull=False,
                        salvage_rate__gte=0,
                        salvage_rate__lte=1,
                        salvage_amount__isnull=True,
                    )
                    | Q(
                        salvage_mode="amount",
                        salvage_rate__isnull=True,
                        salvage_amount__isnull=False,
                        salvage_amount__gte=0,
                    )
                ),
                name="ck_depr_profile_salvage_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        posting_period="yearly",
                        annual_posting_month__gte=1,
                        annual_posting_month__lte=12,
                    )
                    | Q(posting_period="monthly", annual_posting_month__isnull=True)
                ),
                name="ck_depr_profile_period_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        method="units_of_production",
                        expected_total_units__isnull=False,
                        expected_total_units__gt=0,
                    )
                    & ~Q(work_unit="")
                    | (~Q(method="units_of_production")
                       & Q(expected_total_units__isnull=True, work_unit=""))
                ),
                name="ck_depr_profile_work_fields",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=F("effective_from")),
                name="ck_depr_profile_effective_dates",
            ),
            models.CheckConstraint(
                condition=Q(version=1) | ~Q(change_reason=""),
                name="ck_depr_profile_change_reason",
            ),
        ]

    def __str__(self):
        return f"{self.asset} / v{self.version}"


class DepreciationSchedule(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "计划"
        SUPERSEDED = "superseded", "已替代"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="depreciation_schedules",
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="depreciation_schedules"
    )
    depreciation_profile = models.ForeignKey(
        AssetDepreciationProfile,
        on_delete=models.PROTECT,
        related_name="schedules",
    )
    sequence_no = models.PositiveIntegerField()
    period_start = models.DateField()
    period_end = models.DateField()
    opening_book_value = models.DecimalField(**MONEY)
    calculated_unrounded = models.DecimalField(**UNROUNDED)
    planned_amount = models.DecimalField(**MONEY)
    planned_accumulated = models.DecimalField(**MONEY)
    closing_book_value = models.DecimalField(**MONEY)
    planned_units = models.DecimalField(null=True, blank=True, **UNITS)
    eligible_fraction = models.DecimalField(max_digits=12, decimal_places=10)
    formula_snapshot_json = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PLANNED
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("depreciation_profile_id", "sequence_no")
        constraints = [
            models.UniqueConstraint(
                fields=("depreciation_profile", "sequence_no"),
                name="uq_depr_schedule_profile_sequence",
            ),
            models.UniqueConstraint(
                fields=("depreciation_profile", "period_start", "period_end"),
                name="uq_depr_schedule_profile_period",
            ),
            models.CheckConstraint(
                condition=Q(sequence_no__gte=1), name="ck_depr_schedule_sequence"
            ),
            models.CheckConstraint(
                condition=Q(period_end__gt=F("period_start")),
                name="ck_depr_schedule_period",
            ),
            models.CheckConstraint(
                condition=Q(opening_book_value__gte=0)
                & Q(calculated_unrounded__gte=0)
                & Q(planned_amount__gte=0)
                & Q(planned_accumulated__gte=0)
                & Q(closing_book_value__gte=0),
                name="ck_depr_schedule_amounts",
            ),
            models.CheckConstraint(
                condition=Q(planned_units__isnull=True) | Q(planned_units__gte=0),
                name="ck_depr_schedule_units",
            ),
            models.CheckConstraint(
                condition=Q(eligible_fraction__gte=0) & Q(eligible_fraction__lte=1),
                name="ck_depr_schedule_fraction",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("planned", "superseded")),
                name="ck_depr_schedule_status_valid",
            ),
        ]


class DepreciationProfileEvent(models.Model):
    class EventType(models.TextChoices):
        SUSPEND = "suspend", "暂停"
        RESUME = "resume", "恢复"
        STOP = "stop", "停止"
        DISPOSAL_STOP = "disposal_stop", "处置停止"
        DISPOSAL_RESTORE = "disposal_restore", "处置恢复"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="depreciation_profile_events",
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="depreciation_events"
    )
    depreciation_profile = models.ForeignKey(
        AssetDepreciationProfile,
        on_delete=models.PROTECT,
        related_name="events",
    )
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    effective_date = models.DateField()
    reason = models.TextField()
    source_disposal = models.ForeignKey(
        "assets.AssetDisposal",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="depreciation_profile_events",
    )
    previous_profile_status = models.CharField(max_length=16, blank=True)
    reverses_event = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="restored_by_event",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_depreciation_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("depreciation_profile_id", "effective_date", "created_at")
        constraints = [
            models.CheckConstraint(
                condition=~Q(reason=""), name="ck_depr_event_reason"
            ),
            models.CheckConstraint(
                condition=Q(
                    event_type__in=(
                        "suspend",
                        "resume",
                        "stop",
                        "disposal_stop",
                        "disposal_restore",
                    )
                ),
                name="ck_depr_event_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type__in=("suspend", "resume", "stop"),
                        source_disposal__isnull=True,
                        previous_profile_status="",
                        reverses_event__isnull=True,
                    )
                    | Q(
                        event_type="disposal_stop",
                        source_disposal__isnull=False,
                        previous_profile_status__in=("active", "suspended"),
                        reverses_event__isnull=True,
                    )
                    | Q(
                        event_type="disposal_restore",
                        source_disposal__isnull=False,
                        previous_profile_status="",
                        reverses_event__isnull=False,
                    )
                ),
                name="ck_depr_event_disposal_fields",
            ),
            models.UniqueConstraint(
                fields=("source_disposal", "depreciation_profile"),
                condition=Q(event_type="disposal_stop"),
                name="uq_depr_event_disposal_profile_stop",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("折旧事件只允许追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("折旧事件不可删除。")


class AssetWorkUsage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company", on_delete=models.PROTECT, related_name="asset_work_usages"
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="work_usages"
    )
    depreciation_profile = models.ForeignKey(
        AssetDepreciationProfile,
        on_delete=models.PROTECT,
        related_name="work_usages",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    work_unit = models.CharField(max_length=64)
    opening_accumulated_units = models.DecimalField(**UNITS)
    current_units = models.DecimalField(**UNITS)
    closing_accumulated_units = models.DecimalField(**UNITS)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="entered_asset_work_usages",
    )
    entered_at = models.DateTimeField()
    remark = models.TextField(blank=True)

    class Meta:
        ordering = ("depreciation_profile_id", "period_start")
        constraints = [
            models.UniqueConstraint(
                fields=("depreciation_profile", "period_start", "period_end"),
                name="uq_asset_work_usage_profile_period",
            ),
            models.CheckConstraint(
                condition=Q(period_end__gt=F("period_start")),
                name="ck_asset_work_usage_period",
            ),
            models.CheckConstraint(
                condition=Q(opening_accumulated_units__gte=0)
                & Q(current_units__gte=0)
                & Q(closing_accumulated_units__gte=0),
                name="ck_asset_work_usage_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(
                    closing_accumulated_units=F("opening_accumulated_units")
                    + F("current_units")
                ),
                name="ck_asset_work_usage_balance",
            ),
            models.CheckConstraint(
                condition=~Q(work_unit=""), name="ck_asset_work_usage_unit"
            ),
        ]


class DepreciationBatch(models.Model):
    class BatchType(models.TextChoices):
        REGULAR = "regular", "正常计提"
        REVERSAL = "reversal", "冲销"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        REVERSED = "reversed", "已冲销"
        CANCELLED = "cancelled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="depreciation_batches",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    generation_no = models.PositiveIntegerField(default=1)
    batch_type = models.CharField(max_length=16, choices=BatchType.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    idempotency_key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_depreciation_batches",
    )
    generated_at = models.DateTimeField()
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_depreciation_batches",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    reverses_batch = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal_batches",
    )
    supersedes_batch = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseding_batches",
    )
    reversal_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("company_id", "period_start", "generation_no", "batch_type")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "period_start", "generation_no", "batch_type"),
                name="uq_depr_batch_period_generation_type",
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_depr_batch_company_idempotency",
            ),
            models.UniqueConstraint(
                fields=("reverses_batch",),
                condition=Q(batch_type="reversal", status="confirmed"),
                name="uq_depr_batch_confirmed_reversal",
            ),
            models.UniqueConstraint(
                fields=("company", "period_start", "period_end"),
                condition=Q(batch_type="regular", status="confirmed"),
                name="uq_depr_batch_current_regular",
            ),
            models.CheckConstraint(
                condition=Q(period_end__gt=F("period_start")),
                name="ck_depr_batch_period",
            ),
            models.CheckConstraint(
                condition=Q(generation_no__gte=1), name="ck_depr_batch_generation"
            ),
            models.CheckConstraint(
                condition=Q(batch_type__in=("regular", "reversal")),
                name="ck_depr_batch_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("draft", "confirmed", "reversed", "cancelled")),
                name="ck_depr_batch_status_valid",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key="") & ~Q(request_hash=""),
                name="ck_depr_batch_request_values",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status__in=("confirmed", "reversed"),
                        confirmed_at__isnull=False,
                    )
                    | (
                        Q(status__in=("draft", "cancelled"))
                        & Q(
                            confirmed_at__isnull=True,
                            confirmed_by__isnull=True,
                        )
                    )
                ),
                name="ck_depr_batch_confirmation_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        batch_type="reversal",
                        reverses_batch__isnull=False,
                    )
                    & ~Q(reversal_reason="")
                    & Q(supersedes_batch__isnull=True)
                    | Q(
                        batch_type="regular",
                        reverses_batch__isnull=True,
                        reversal_reason="",
                    )
                ),
                name="ck_depr_batch_type_fields",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("reverses_batch_id"))
                & ~Q(id=F("supersedes_batch_id")),
                name="ck_depr_batch_not_self_reference",
            ),
        ]


class DepreciationBatchItem(models.Model):
    class Status(models.TextChoices):
        READY = "ready", "可确认"
        SKIPPED = "skipped", "跳过"
        ERROR = "error", "错误"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="depreciation_batch_items",
    )
    batch = models.ForeignKey(
        DepreciationBatch, on_delete=models.CASCADE, related_name="items"
    )
    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        related_name="depreciation_batch_items",
    )
    depreciation_profile = models.ForeignKey(
        AssetDepreciationProfile,
        on_delete=models.PROTECT,
        related_name="batch_items",
    )
    depreciation_schedule = models.ForeignKey(
        DepreciationSchedule,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="batch_items",
    )
    calculation_method = models.CharField(
        max_length=32, choices=DepreciationMethod.choices
    )
    opening_book_value = models.DecimalField(**MONEY)
    depreciable_floor = models.DecimalField(**MONEY)
    eligible_fraction = models.DecimalField(max_digits=12, decimal_places=10)
    usage_units = models.DecimalField(null=True, blank=True, **UNITS)
    manual_amount = models.DecimalField(null=True, blank=True, **MONEY)
    manual_reason = models.TextField(blank=True)
    manual_entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="entered_manual_depreciation_items",
    )
    manual_entered_at = models.DateTimeField(null=True, blank=True)
    calculated_unrounded = models.DecimalField(null=True, blank=True, **UNROUNDED)
    planned_amount = models.DecimalField(null=True, blank=True, **MONEY)
    closing_book_value = models.DecimalField(null=True, blank=True, **MONEY)
    calculation_snapshot_json = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("batch_id", "asset_id")
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "asset"), name="uq_depr_batch_item_asset"
            ),
            models.CheckConstraint(
                condition=Q(opening_book_value__gte=0)
                & Q(depreciable_floor__gte=0)
                & (Q(planned_amount__isnull=True) | Q(planned_amount__gte=0))
                & (Q(closing_book_value__isnull=True) | Q(closing_book_value__gte=0)),
                name="ck_depr_batch_item_amounts",
            ),
            models.CheckConstraint(
                condition=Q(eligible_fraction__gte=0) & Q(eligible_fraction__lte=1),
                name="ck_depr_batch_item_fraction",
            ),
            models.CheckConstraint(
                condition=Q(usage_units__isnull=True) | Q(usage_units__gte=0),
                name="ck_depr_batch_item_usage",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        calculation_method="manual",
                        status="ready",
                        manual_amount__isnull=False,
                        manual_amount__gte=0,
                        manual_entered_at__isnull=False,
                    )
                    & ~Q(manual_reason="")
                    | (Q(calculation_method="manual", status__in=("error", "skipped"))
                       & Q(
                           manual_amount__isnull=True,
                           manual_reason="",
                           manual_entered_by__isnull=True,
                           manual_entered_at__isnull=True,
                       ))
                    | (~Q(calculation_method="manual")
                       & Q(
                           manual_amount__isnull=True,
                           manual_reason="",
                           manual_entered_by__isnull=True,
                           manual_entered_at__isnull=True,
                       ))
                ),
                name="ck_depr_batch_item_manual_fields",
            ),
            models.CheckConstraint(
                condition=(Q(status="error") & ~Q(error_message=""))
                | (~Q(status="error") & Q(error_message="")),
                name="ck_depr_batch_item_error_fields",
            ),
            models.CheckConstraint(
                condition=Q(calculation_method__in=DepreciationMethod.values),
                name="ck_depr_batch_item_method_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("ready", "skipped", "error")),
                name="ck_depr_batch_item_status_valid",
            ),
        ]


class AssetValueAdjustment(models.Model):
    class AdjustmentType(models.TextChoices):
        OPENING_IMPAIRMENT = "opening_impairment", "期初减值"
        IMPAIRMENT = "impairment", "减值"
        IMPAIRMENT_REVERSAL = "impairment_reversal", "减值转回"
        COST_CORRECTION = "cost_correction", "原值更正"
        DEPRECIATION_ADJUSTMENT = "depreciation_adjustment", "折旧调整"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        CONFIRMED = "confirmed", "已确认"
        REVERSED = "reversed", "已冲销"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="asset_value_adjustments",
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="value_adjustments"
    )
    adjustment_type = models.CharField(max_length=32, choices=AdjustmentType.choices)
    effective_date = models.DateField()
    amount = models.DecimalField(**MONEY)
    old_values_json = models.JSONField(default=dict)
    new_values_json = models.JSONField(default=dict)
    reason = models.TextField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_asset_value_adjustments",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    reversal_of = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_asset_value_adjustments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("asset_id", "effective_date", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(adjustment_type="opening_impairment"),
                name="uq_asset_opening_impairment",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""), name="ck_asset_adjustment_reason"
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        adjustment_type__in=(
                            "opening_impairment",
                            "impairment",
                            "impairment_reversal",
                        ),
                        amount__gt=0,
                    )
                    | Q(
                        adjustment_type__in=(
                            "cost_correction",
                            "depreciation_adjustment",
                        )
                    )
                    & ~Q(amount=0)
                ),
                name="ck_asset_adjustment_amount",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="draft",
                        confirmed_at__isnull=True,
                        confirmed_by__isnull=True,
                    )
                    | Q(status__in=("confirmed", "reversed"), confirmed_at__isnull=False)
                ),
                name="ck_asset_adjustment_confirmation",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("reversal_of_id")),
                name="ck_asset_adjustment_not_self_reversal",
            ),
            models.CheckConstraint(
                condition=Q(
                    adjustment_type__in=(
                        "opening_impairment",
                        "impairment",
                        "impairment_reversal",
                        "cost_correction",
                        "depreciation_adjustment",
                    )
                ),
                name="ck_asset_adjustment_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("draft", "confirmed", "reversed")),
                name="ck_asset_adjustment_status_valid",
            ),
        ]


class DepreciationEntry(models.Model):
    class SourceType(models.TextChoices):
        BATCH = "batch", "批次"
        OPENING = "opening", "期初"
        ADJUSTMENT = "adjustment", "调整"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="depreciation_entries",
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="depreciation_entries"
    )
    depreciation_profile = models.ForeignKey(
        AssetDepreciationProfile,
        on_delete=models.PROTECT,
        related_name="entries",
    )
    entry_date = models.DateField()
    period_start = models.DateField()
    period_end = models.DateField()
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    batch_item = models.ForeignKey(
        DepreciationBatchItem,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="entries",
    )
    opening_profile = models.ForeignKey(
        AssetDepreciationProfile,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="opening_entries",
    )
    value_adjustment = models.ForeignKey(
        AssetValueAdjustment,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="depreciation_entries",
    )
    amount = models.DecimalField(**MONEY)
    accumulated_depreciation_after = models.DecimalField(**MONEY)
    book_value_after = models.DecimalField(**MONEY)
    reversal_of = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal",
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posted_depreciation_entries",
    )
    posted_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ProtectedFinanceQuerySet.as_manager()

    class Meta:
        ordering = ("asset_id", "period_start", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("batch_item",),
                condition=Q(source_type="batch"),
                name="uq_depr_entry_batch_item",
            ),
            models.UniqueConstraint(
                fields=("opening_profile",),
                condition=Q(source_type="opening"),
                name="uq_depr_entry_opening_profile",
            ),
            models.UniqueConstraint(
                fields=("value_adjustment",),
                condition=Q(source_type="adjustment"),
                name="uq_depr_entry_value_adjustment",
            ),
            models.CheckConstraint(
                condition=Q(period_end__gt=F("period_start")),
                name="ck_depr_entry_period",
            ),
            models.CheckConstraint(
                condition=Q(accumulated_depreciation_after__gte=0)
                & Q(book_value_after__gte=0),
                name="ck_depr_entry_balances",
            ),
            models.CheckConstraint(
                condition=Q(amount__gte=0)
                | Q(reversal_of__isnull=False)
                | Q(source_type="adjustment"),
                name="ck_depr_entry_amount_sign",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        source_type="batch",
                        batch_item__isnull=False,
                        opening_profile__isnull=True,
                        value_adjustment__isnull=True,
                    )
                    | Q(
                        source_type="opening",
                        batch_item__isnull=True,
                        opening_profile__isnull=False,
                        value_adjustment__isnull=True,
                    )
                    | Q(
                        source_type="adjustment",
                        batch_item__isnull=True,
                        opening_profile__isnull=True,
                        value_adjustment__isnull=False,
                    )
                ),
                name="ck_depr_entry_source_fields",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("reversal_of_id")),
                name="ck_depr_entry_not_self_reversal",
            ),
            models.CheckConstraint(
                condition=Q(source_type__in=("batch", "opening", "adjustment")),
                name="ck_depr_entry_source_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("已过账折旧分录只允许追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("已过账折旧分录不可删除。")


class TheoreticalDepreciationRun(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        COMPLETED = "completed", "完成"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="theoretical_depreciation_runs",
    )
    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        related_name="theoretical_depreciation_runs",
    )
    as_of_date = models.DateField()
    parameter_snapshot_json = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requested_theoretical_depreciation_runs",
    )
    requested_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=255)

    class Meta:
        ordering = ("-requested_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_theoretical_run_company_idempotency",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""), name="ck_theoretical_run_idempotency"
            ),
            models.CheckConstraint(
                condition=(Q(status="draft", completed_at__isnull=True))
                | (Q(status__in=("completed", "failed"), completed_at__isnull=False)),
                name="ck_theoretical_run_completion",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("draft", "completed", "failed")),
                name="ck_theoretical_run_status_valid",
            ),
        ]


class TheoreticalDepreciationLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        TheoreticalDepreciationRun, on_delete=models.CASCADE, related_name="lines"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    theoretical_amount = models.DecimalField(**MONEY)
    theoretical_accumulated = models.DecimalField(**MONEY)
    theoretical_book_value = models.DecimalField(**MONEY)
    formula_snapshot_json = models.JSONField(default=dict)

    class Meta:
        ordering = ("run_id", "period_start")
        constraints = [
            models.UniqueConstraint(
                fields=("run", "period_start", "period_end"),
                name="uq_theoretical_line_run_period",
            ),
            models.CheckConstraint(
                condition=Q(period_end__gt=F("period_start")),
                name="ck_theoretical_line_period",
            ),
            models.CheckConstraint(
                condition=Q(theoretical_amount__gte=0)
                & Q(theoretical_accumulated__gte=0)
                & Q(theoretical_book_value__gte=0),
                name="ck_theoretical_line_amounts",
            ),
        ]


class FinanceFormalizationRequest(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "completed", "已完成"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="formalization_requests",
    )
    asset = models.OneToOneField(
        "assets.Asset",
        on_delete=models.PROTECT,
        related_name="formalization_request",
    )
    operation = models.CharField(max_length=32, default="finance_formalization")
    idempotency_key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.COMPLETED
    )
    result_issued_code = models.OneToOneField(
        "masterdata.IssuedCode",
        on_delete=models.PROTECT,
        related_name="formalization_request",
    )
    result_finance = models.OneToOneField(
        AssetFinance,
        on_delete=models.PROTECT,
        related_name="formalization_request",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_finance_formalization_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField()

    objects = ProtectedFinanceQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_fin_formalization_company_idempotency",
            ),
            models.CheckConstraint(
                condition=Q(operation="finance_formalization"),
                name="ck_fin_formalization_operation",
            ),
            models.CheckConstraint(
                condition=Q(status="completed"), name="ck_fin_formalization_status"
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key="") & ~Q(request_hash=""),
                name="ck_fin_formalization_request_values",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("正式化幂等结果不可修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("正式化幂等结果不可删除。")
