import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.assets.models import Asset, AssetDisposal
from apps.masterdata.models import Company, Employee


def _actor_null_only(queryset, kwargs, actor_fields, message):
    if kwargs and set(kwargs).issubset(actor_fields) and all(
        value is None for value in kwargs.values()
    ):
        return models.QuerySet.update(queryset, **kwargs)
    raise ValidationError(message)


class MaintenancePlanQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("保养计划只能通过受控保养 Service 修改。")

    def delete(self):
        raise ValidationError("保养计划不得物理删除。")


class MaintenancePlan(models.Model):
    class CycleUnit(models.TextChoices):
        DAY = "day", "日"
        WEEK = "week", "周"
        MONTH = "month", "月"
        YEAR = "year", "年"

    class Status(models.TextChoices):
        ACTIVE = "active", "启用"
        SUSPENDED = "suspended", "暂停"
        ENDED = "ended", "已终止"

    class EndedReason(models.TextChoices):
        MANUAL = "manual", "手工终止"
        ASSET_DISPOSAL = "asset_disposal", "资产处置"
        OTHER = "other", "其他"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="maintenance_plans"
    )
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="maintenance_plans"
    )
    name = models.CharField("计划名称", max_length=200)
    cycle_value = models.PositiveIntegerField("周期数值")
    cycle_unit = models.CharField("周期单位", max_length=8, choices=CycleUnit.choices)
    advance_notice_days = models.PositiveIntegerField("提前提醒天数", default=0)
    responsible_employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="responsible_maintenance_plans",
    )
    standard_content = models.TextField("标准内容")
    first_due_date = models.DateField("首次到期日")
    last_maintenance_date = models.DateField("上次完成日", null=True, blank=True)
    next_maintenance_date = models.DateField("下次到期日")
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    ended_reason = models.CharField(
        "终止原因", max_length=24, choices=EndedReason.choices, null=True, blank=True
    )
    ended_by_disposal = models.ForeignKey(
        AssetDisposal,
        on_delete=models.PROTECT,
        related_name="ended_maintenance_plans",
        null=True,
        blank=True,
    )
    status_before_disposal = models.CharField(
        "处置前状态",
        max_length=16,
        choices=((Status.ACTIVE, "启用"), (Status.SUSPENDED, "暂停")),
        null=True,
        blank=True,
    )
    ended_at = models.DateTimeField("终止时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    objects = MaintenancePlanQuerySet.as_manager()

    class Meta:
        ordering = ("next_maintenance_date", "id")
        constraints = [
            models.CheckConstraint(condition=Q(cycle_value__gt=0), name="ck_maint_plan_cycle_positive"),
            models.CheckConstraint(condition=Q(advance_notice_days__gte=0), name="ck_maint_plan_notice_nonnegative"),
            models.CheckConstraint(condition=~Q(name="") & ~Q(standard_content=""), name="ck_maint_plan_required_text"),
            models.CheckConstraint(condition=Q(cycle_unit__in=("day", "week", "month", "year")), name="ck_maint_plan_cycle_unit"),
            models.CheckConstraint(condition=Q(status__in=("active", "suspended", "ended")), name="ck_maint_plan_status"),
            models.CheckConstraint(
                condition=(
                    Q(status__in=("active", "suspended"), ended_reason__isnull=True, ended_by_disposal__isnull=True, status_before_disposal__isnull=True, ended_at__isnull=True)
                    | Q(status="ended", ended_reason__in=("manual", "other"), ended_by_disposal__isnull=True, status_before_disposal__isnull=True, ended_at__isnull=False)
                    | Q(status="ended", ended_reason="asset_disposal", ended_by_disposal__isnull=False, status_before_disposal__in=("active", "suspended"), ended_at__isnull=False)
                ),
                name="ck_maint_plan_ended_fields",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        if self.responsible_employee_id:
            employee = self.responsible_employee
            if employee.company_id != self.company_id:
                errors["responsible_employee"] = "责任人必须属于同一公司。"
            elif employee.employment_status != "active" or not employee.is_active:
                errors["responsible_employee"] = "责任人必须为启用的在职员工。"
        if self.ended_by_disposal_id:
            disposal = self.ended_by_disposal
            if disposal.company_id != self.company_id or disposal.asset_id != self.asset_id:
                errors["ended_by_disposal"] = "处置必须属于同公司的同一资产。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("保养计划只能通过受控保养 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("保养计划不得物理删除。")


class MaintenanceRecordQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _actor_null_only(
            self,
            kwargs,
            {"completed_by", "completed_by_id", "voided_by", "voided_by_id"},
            "保养记录只能通过受控保养 Service 作废。",
        )

    def delete(self):
        raise ValidationError("保养记录不得物理删除。")


class MaintenanceRecord(models.Model):
    class Result(models.TextChoices):
        NORMAL = "normal", "正常"
        PROBLEM_FOUND = "problem_found", "发现问题"

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "已确认"
        VOIDED = "voided", "已作废"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="maintenance_records")
    maintenance_plan = models.ForeignKey(MaintenancePlan, on_delete=models.PROTECT, related_name="records")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="maintenance_records")
    scheduled_date = models.DateField("计划日期")
    completed_date = models.DateField("实际完成日期")
    completed_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="completed_maintenance_records")
    content_snapshot = models.TextField("实际内容")
    result = models.CharField("结果", max_length=16, choices=Result.choices)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.CONFIRMED)
    void_reason = models.TextField("作废原因", blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="voided_maintenance_records")
    voided_at = models.DateTimeField("作废时间", null=True, blank=True)
    remark = models.TextField("备注", blank=True)
    idempotency_key = models.CharField("幂等键", max_length=128)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = MaintenanceRecordQuerySet.as_manager()

    class Meta:
        ordering = ("-completed_date", "-created_at")
        constraints = [
            models.UniqueConstraint(fields=("company", "idempotency_key"), name="uq_maint_record_company_idem"),
            models.UniqueConstraint(fields=("maintenance_plan", "scheduled_date"), condition=Q(status="confirmed"), name="uq_maint_record_confirmed_due"),
            models.CheckConstraint(condition=Q(result__in=("normal", "problem_found")), name="ck_maint_record_result"),
            models.CheckConstraint(condition=Q(status__in=("confirmed", "voided")), name="ck_maint_record_status"),
            models.CheckConstraint(condition=~Q(content_snapshot="") & ~Q(idempotency_key=""), name="ck_maint_record_required_text"),
            models.CheckConstraint(
                condition=(
                    Q(status="confirmed", void_reason="", voided_by__isnull=True, voided_at__isnull=True)
                    | (Q(status="voided", voided_at__isnull=False) & ~Q(void_reason=""))
                ),
                name="ck_maint_record_void_fields",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.maintenance_plan_id and self.maintenance_plan.company_id != self.company_id:
            errors["maintenance_plan"] = "计划必须属于同一公司。"
        if self.maintenance_plan_id and self.maintenance_plan.asset_id != self.asset_id:
            errors["asset"] = "保养记录资产必须与计划资产一致。"
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        if self.completed_by_id and self.completed_by.company_id != self.company_id:
            errors["completed_by"] = "完成人必须属于同一公司。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("保养记录只能通过受控保养 Service 作废。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("保养记录不得物理删除。")


class MaintenanceProblemQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _actor_null_only(
            self,
            kwargs,
            {"closed_by", "closed_by_id", "owner_employee", "owner_employee_id"},
            "保养问题只能通过受控保养 Service 关闭。",
        )

    def delete(self):
        raise ValidationError("保养问题不得物理删除。")


class MaintenanceProblem(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待跟进"
        CLOSED = "closed", "已关闭"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="maintenance_problems")
    maintenance_record = models.OneToOneField(MaintenanceRecord, on_delete=models.PROTECT, related_name="problem")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="maintenance_problems")
    description = models.TextField("问题说明")
    status = models.CharField("状态", max_length=8, choices=Status.choices, default=Status.OPEN)
    owner_employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name="owned_maintenance_problems")
    target_date = models.DateField("目标日期", null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="closed_maintenance_problems")
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    closure_note = models.TextField("处理说明", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = MaintenanceProblemQuerySet.as_manager()

    class Meta:
        ordering = ("status", "target_date", "created_at")
        constraints = [
            models.CheckConstraint(condition=~Q(description=""), name="ck_maint_problem_description"),
            models.CheckConstraint(condition=Q(status__in=("open", "closed")), name="ck_maint_problem_status"),
            models.CheckConstraint(
                condition=(
                    Q(status="open", closed_by__isnull=True, closed_at__isnull=True, closure_note="")
                    | (Q(status="closed", closed_at__isnull=False) & ~Q(closure_note=""))
                ),
                name="ck_maint_problem_closed_fields",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.maintenance_record_id:
            record = self.maintenance_record
            if record.company_id != self.company_id or record.asset_id != self.asset_id:
                errors["maintenance_record"] = "保养记录必须属于同公司的同一资产。"
            if record.result != MaintenanceRecord.Result.PROBLEM_FOUND:
                errors["maintenance_record"] = "只有发现问题的保养记录才能建立跟进项。"
            if self.target_date and self.target_date < record.completed_date:
                errors["target_date"] = "目标日期不得早于实际完成日期。"
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        if self.owner_employee_id and self.owner_employee.company_id != self.company_id:
            errors["owner_employee"] = "跟进责任人必须属于同一公司。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("保养问题只能通过受控保养 Service 关闭。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("保养问题不得物理删除。")
