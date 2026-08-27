import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from apps.masterdata.models import Attachment, Company
from apps.reports.schemas import (
    REPORT_SCHEMA_VERSION,
    TPLUS_SCHEMA_VERSION,
    TPLUS_TOTAL_METRICS,
)


HEX64_VALIDATOR = RegexValidator(
    regex=r"^[0-9a-f]{64}$",
    message="摘要必须是 64 位小写十六进制字符串。",
)

REPORT_TOTALS_SCHEMA_VERSION = REPORT_SCHEMA_VERSION
TPLUS_TOTALS_SCHEMA_VERSION = TPLUS_SCHEMA_VERSION
TPLUS_TOTAL_METRIC_KEYS = TPLUS_TOTAL_METRICS
EXPORT_TOTAL_SCHEMAS = {
    REPORT_TOTALS_SCHEMA_VERSION: frozenset(),
    TPLUS_TOTALS_SCHEMA_VERSION: frozenset(TPLUS_TOTAL_METRIC_KEYS),
}


class ExportLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        actor_fields = {"requested_by", "requested_by_id"}
        if set(kwargs).issubset(actor_fields) and all(
            value is None for value in kwargs.values()
        ):
            return super().update(**kwargs)
        raise ValidationError("导出记录只能通过受控发布 Service 修改。")

    def delete(self):
        raise ValidationError("导出记录不可删除。")


class ExportLog(models.Model):
    class ExportType(models.TextChoices):
        ASSET_LEDGER = "asset_ledger", "公司资产总账"
        FIXED_ASSET_DETAIL = "fixed_asset_detail", "固定资产明细"
        DEPRECIATION_SCHEDULE = "depreciation_schedule", "折旧计划"
        DEPRECIATION_DETAIL = "depreciation_detail", "折旧明细"
        MONTHLY_DEPRECIATION = "monthly_depreciation", "月度折旧"
        DEPARTMENT_ASSETS = "department_assets", "部门资产"
        EMPLOYEE_ASSETS = "employee_assets", "人员资产"
        EQUIPMENT_LIST = "equipment_list", "设备清单"
        MOLD_TOOL_INSPECTION_LIST = (
            "mold_tool_inspection_list",
            "模具工具检具清单",
        )
        INVENTORY_RESULTS = "inventory_results", "盘点结果"
        INVENTORY_DIFFERENCES = "inventory_differences", "盘点差异"
        MAINTENANCE_PLANS = "maintenance_plans", "保养计划"
        MAINTENANCE_DUE = "maintenance_due", "到期保养"
        MAINTENANCE_RECORDS = "maintenance_records", "保养记录"
        OFFBOARDING_UNRESOLVED = "offboarding_unresolved", "离职资产未清"
        DISPOSAL_LIST = "disposal_list", "处置清单"
        SUPPLY_STOCK_BALANCE = "supply_stock_balance", "当前库存余额表"
        SUPPLY_LOW_STOCK = "supply_low_stock", "低库存预警表"
        SUPPLY_STOCK_MOVEMENT = "supply_stock_movement", "库存收发存表"
        SUPPLY_STOCK_LEDGER = "supply_stock_ledger", "库存流水明细表"
        SUPPLY_ISSUE_DETAIL = "supply_issue_detail", "领用明细表"
        SUPPLY_DEPARTMENT_ISSUE = "supply_department_issue", "部门领用汇总表"
        SUPPLY_EMPLOYEE_ISSUE = "supply_employee_issue", "员工领用汇总表"
        SUPPLY_CUSTODY_BALANCE = "supply_custody_balance", "耐用品保管余额表"
        SUPPLY_CUSTODY_MOVEMENT = "supply_custody_movement", "保管动作明细表"
        SUPPLY_COUNT_DIFFERENCE = "supply_count_difference", "盘点差异处理表"
        CONTROLLED_NON_FIXED_ASSETS = (
            "controlled_non_fixed_assets",
            "逐件受控非固定资产清单",
        )
        SUPPLY_MANAGEMENT_AMOUNT = "supply_management_amount", "综合管理金额表"
        TPLUS_RECONCILIATION = "tplus_reconciliation", "T+ 人工对账"

    class Status(models.TextChoices):
        PENDING = "pending", "生成中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"
        EXPIRED = "expired", "已过期"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="export_logs",
    )
    export_type = models.CharField(
        "导出类型", max_length=48, choices=ExportType.choices
    )
    filters_json = models.JSONField(
        "筛选条件", default=dict, encoder=DjangoJSONEncoder
    )
    data_snapshot_at = models.DateTimeField("数据截止时间", null=True, blank=True)
    row_count = models.PositiveIntegerField("行数", null=True, blank=True)
    output_attachment = models.OneToOneField(
        Attachment,
        verbose_name="输出附件",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="export_log",
    )
    output_sha256 = models.CharField(
        "输出 SHA-256",
        max_length=64,
        blank=True,
        validators=[HEX64_VALIDATOR],
    )
    totals_schema_version = models.CharField("合计 Schema 版本", max_length=32, blank=True)
    request_hash = models.CharField(
        "请求摘要", max_length=64, validators=[HEX64_VALIDATOR], db_index=True
    )
    idempotency_key = models.CharField("幂等键", max_length=128)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="导出人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requested_export_logs",
    )
    requested_at = models.DateTimeField("请求时间", auto_now_add=True)
    completed_at = models.DateTimeField("完成时间", null=True, blank=True)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    error_summary = models.TextField("错误摘要", blank=True)

    objects = ExportLogQuerySet.as_manager()

    class Meta:
        verbose_name = "导出记录"
        verbose_name_plural = "导出记录"
        ordering = ("-requested_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_export_log_company_idempotency",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "completed", "failed", "expired")),
                name="ck_export_log_status",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status="pending")
                    | Q(
                        output_attachment__isnull=True,
                        output_sha256="",
                        totals_schema_version="",
                        row_count__isnull=True,
                        data_snapshot_at__isnull=True,
                        completed_at__isnull=True,
                        error_summary="",
                    )
                ),
                name="ck_export_log_pending_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status="completed")
                    | Q(
                        output_attachment__isnull=False,
                        data_snapshot_at__isnull=False,
                        row_count__isnull=False,
                        completed_at__isnull=False,
                    )
                    & ~Q(output_sha256="")
                    & ~Q(totals_schema_version="")
                    & Q(error_summary="")
                ),
                name="ck_export_log_completed_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status="failed")
                    | Q(
                        output_attachment__isnull=True,
                        output_sha256="",
                        completed_at__isnull=True,
                    )
                    & ~Q(error_summary="")
                ),
                name="ck_export_log_failed_fields",
            ),
            models.CheckConstraint(
                condition=~Q(request_hash="") & ~Q(idempotency_key=""),
                name="ck_export_log_request_fields",
            ),
        ]

    @property
    def generated_at(self):
        return self.completed_at

    def clean(self):
        super().clean()
        self.output_sha256 = (self.output_sha256 or "").strip().lower()
        self.request_hash = (self.request_hash or "").strip().lower()
        errors = {}
        if not isinstance(self.filters_json, dict):
            errors["filters_json"] = "筛选条件必须是 JSON 对象。"
        if self.output_attachment_id:
            if self.output_attachment.company_id != self.company_id:
                errors["output_attachment"] = "输出附件必须属于同一公司。"
            elif self.output_attachment.sha256 != self.output_sha256:
                errors["output_sha256"] = "输出摘要必须与附件摘要一致。"
        expected_schema = (
            TPLUS_TOTALS_SCHEMA_VERSION
            if self.export_type == self.ExportType.TPLUS_RECONCILIATION
            else REPORT_TOTALS_SCHEMA_VERSION
        )
        if self.status == self.Status.COMPLETED and self.totals_schema_version != expected_schema:
            errors["totals_schema_version"] = "合计 Schema 版本与导出类型不匹配。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.output_sha256 = (self.output_sha256 or "").strip().lower()
        self.request_hash = (self.request_hash or "").strip().lower()
        if not self._state.adding:
            raise ValidationError("导出记录只能通过受控发布 Service 修改。")
        if self.status != self.Status.PENDING or any(
            (
                self.output_attachment_id is not None,
                bool(self.output_sha256),
                bool(self.totals_schema_version),
                self.row_count is not None,
                self.data_snapshot_at is not None,
                self.completed_at is not None,
                bool(self.error_summary),
            )
        ):
            raise ValidationError("导出记录必须以无发布数据的 pending 状态创建。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("导出记录不可删除。")

    def __str__(self):
        return f"{self.get_export_type_display()} / {self.pk}"


class ExportLogTotalQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("导出合计只能在受控发布事务中写入。")

    def delete(self):
        raise ValidationError("导出合计不可删除。")


class ExportLogTotal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="export_log_totals",
    )
    export_log = models.ForeignKey(
        ExportLog,
        verbose_name="导出记录",
        on_delete=models.CASCADE,
        related_name="totals",
    )
    metric_key = models.CharField("合计指标", max_length=64)
    amount = models.DecimalField("金额", max_digits=18, decimal_places=2)
    currency = models.CharField("币种", max_length=3, default="CNY")

    objects = ExportLogTotalQuerySet.as_manager()

    class Meta:
        verbose_name = "导出金额合计"
        verbose_name_plural = "导出金额合计"
        ordering = ("export_log_id", "metric_key")
        constraints = [
            models.UniqueConstraint(
                fields=("export_log", "metric_key"),
                name="uq_export_log_total_metric",
            ),
            models.CheckConstraint(
                condition=Q(currency="CNY"), name="ck_export_log_total_currency"
            ),
            models.CheckConstraint(
                condition=~Q(metric_key=""), name="ck_export_log_total_metric_nonempty"
            ),
            models.CheckConstraint(
                condition=Q(metric_key__in=TPLUS_TOTAL_METRIC_KEYS),
                name="ck_export_log_total_metric_key",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.export_log_id and self.export_log.company_id != self.company_id:
            errors["export_log"] = "导出记录与合计必须属于同一公司。"
        elif self.export_log_id and self.export_log.export_type != ExportLog.ExportType.TPLUS_RECONCILIATION:
            errors["export_log"] = "只有 T+ 对账导出可以保存金额合计。"
        if self.metric_key not in TPLUS_TOTAL_METRIC_KEYS:
            errors["metric_key"] = "未知的导出合计指标。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("导出合计只能在受控发布事务中写入。")
        if self.export_log_id and self.export_log.status != ExportLog.Status.PENDING:
            raise ValidationError("只能向 pending 导出记录写入合计。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("导出合计不可删除。")

    def __str__(self):
        return f"{self.export_log_id} / {self.metric_key}"
