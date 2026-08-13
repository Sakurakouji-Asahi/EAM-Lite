import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.assets.models import Asset, AssetMovement
from apps.masterdata.models import (
    AssetCategory,
    Company,
    Department,
    Employee,
    Location,
)


def _only_actor_null_update(queryset, kwargs, *, actor_fields, message):
    if set(kwargs).issubset(actor_fields) and all(
        value is None for value in kwargs.values()
    ):
        return models.QuerySet.update(queryset, **kwargs)
    raise ValidationError(message)


class InventoryTaskQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _only_actor_null_update(
            self,
            kwargs,
            actor_fields={
                "created_by",
                "created_by_id",
                "scanning_stopped_by",
                "scanning_stopped_by_id",
                "closed_by",
                "closed_by_id",
                "cancelled_by",
                "cancelled_by_id",
            },
            message="盘点任务只能通过受控盘点 Service 修改。",
        )

    def delete(self):
        raise ValidationError("盘点任务不得物理删除。")


class InventoryTask(models.Model):
    class InventoryType(models.TextChoices):
        DEPARTMENT = "department", "部门盘点"
        FULL = "full", "财务全盘"
        SPECIAL = "special", "专项盘点"

    class ScopeType(models.TextChoices):
        COMPANY = "company", "全公司"
        DEPARTMENT = "department", "部门"
        CATEGORY = "category", "实物分类"
        LOCATION = "location", "位置"
        SELECTED_ASSETS = "selected_assets", "勾选资产"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        IN_PROGRESS = "in_progress", "进行中"
        RECONCILIATION = "reconciliation", "差异处理中"
        CLOSED = "closed", "已关闭"
        CANCELLED = "cancelled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="inventory_tasks",
        verbose_name="公司",
    )
    task_code = models.CharField("任务编号", max_length=64)
    name = models.CharField("任务名称", max_length=200)
    inventory_type = models.CharField(
        "盘点类型", max_length=16, choices=InventoryType.choices
    )
    scope_type = models.CharField(
        "范围类型", max_length=32, choices=ScopeType.choices
    )
    scope_department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_tasks",
        verbose_name="范围部门",
    )
    scope_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_tasks",
        verbose_name="范围位置",
    )
    scope_category = models.ForeignKey(
        AssetCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_tasks",
        verbose_name="范围实物分类",
    )
    scope_definition_json = models.JSONField("范围定义快照", default=dict, blank=True)
    planned_start = models.DateField("计划开始日期")
    planned_end = models.DateField("计划结束日期")
    remark = models.TextField("备注", blank=True)
    snapshot_at = models.DateTimeField("快照基准时间", null=True, blank=True)
    expected_asset_count = models.PositiveIntegerField(
        "应盘数量", null=True, blank=True
    )
    status = models.CharField(
        "状态", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    idempotency_key = models.CharField("创建幂等键", max_length=128)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_inventory_tasks",
        verbose_name="创建人",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    scanning_stopped_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stopped_inventory_tasks",
        verbose_name="停止扫码人",
    )
    scanning_stopped_at = models.DateTimeField("停止扫码时间", null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_inventory_tasks",
        verbose_name="关闭人",
    )
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_inventory_tasks",
        verbose_name="取消人",
    )
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    cancellation_reason = models.TextField("取消原因", blank=True)

    objects = InventoryTaskQuerySet.as_manager()

    class Meta:
        verbose_name = "盘点任务"
        verbose_name_plural = "盘点任务"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "task_code"), name="uq_inv_task_company_code"
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_inv_task_company_idem",
            ),
            models.CheckConstraint(
                condition=~Q(task_code="") & ~Q(name="") & ~Q(idempotency_key=""),
                name="ck_inv_task_required_text",
            ),
            models.CheckConstraint(
                condition=Q(inventory_type__in=("department", "full", "special")),
                name="ck_inv_task_type",
            ),
            models.CheckConstraint(
                condition=Q(
                    scope_type__in=(
                        "company",
                        "department",
                        "category",
                        "location",
                        "selected_assets",
                    )
                ),
                name="ck_inv_task_scope_type",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        scope_type="company",
                        scope_department__isnull=True,
                        scope_location__isnull=True,
                        scope_category__isnull=True,
                    )
                    | Q(
                        scope_type="department",
                        scope_department__isnull=False,
                        scope_location__isnull=True,
                        scope_category__isnull=True,
                    )
                    | Q(
                        scope_type="category",
                        scope_department__isnull=True,
                        scope_location__isnull=True,
                        scope_category__isnull=False,
                    )
                    | Q(
                        scope_type="location",
                        scope_department__isnull=True,
                        scope_location__isnull=False,
                        scope_category__isnull=True,
                    )
                    | Q(
                        scope_type="selected_assets",
                        scope_department__isnull=True,
                        scope_location__isnull=True,
                        scope_category__isnull=True,
                    )
                ),
                name="ck_inv_task_scope_fields",
            ),
            models.CheckConstraint(
                condition=Q(planned_end__gte=models.F("planned_start")),
                name="ck_inv_task_planned_dates",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=(
                        "draft",
                        "in_progress",
                        "reconciliation",
                        "closed",
                        "cancelled",
                    )
                ),
                name="ck_inv_task_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="draft",
                        snapshot_at__isnull=True,
                        expected_asset_count__isnull=True,
                        scanning_stopped_at__isnull=True,
                        scanning_stopped_by__isnull=True,
                        closed_at__isnull=True,
                        closed_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                    | Q(
                        status="in_progress",
                        snapshot_at__isnull=False,
                        expected_asset_count__isnull=False,
                        scanning_stopped_at__isnull=True,
                        scanning_stopped_by__isnull=True,
                        closed_at__isnull=True,
                        closed_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                    | Q(
                        status="reconciliation",
                        snapshot_at__isnull=False,
                        expected_asset_count__isnull=False,
                        scanning_stopped_at__isnull=False,
                        closed_at__isnull=True,
                        closed_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                    | Q(
                        status="closed",
                        snapshot_at__isnull=False,
                        expected_asset_count__isnull=False,
                        scanning_stopped_at__isnull=False,
                        closed_at__isnull=False,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                    | (
                        Q(
                            status="cancelled",
                            cancelled_at__isnull=False,
                            closed_at__isnull=True,
                            closed_by__isnull=True,
                        )
                        & ~Q(cancellation_reason="")
                    )
                ),
                name="ck_inv_task_status_fields",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.scope_department_id and self.scope_department.company_id != self.company_id:
            errors["scope_department"] = "范围部门必须属于同一公司。"
        if self.scope_location_id and self.scope_location.company_id != self.company_id:
            errors["scope_location"] = "范围位置必须属于同一公司。"
        if self.scope_category_id and self.scope_category.company_id != self.company_id:
            errors["scope_category"] = "范围实物分类必须属于同一公司。"
        if self.status != self.Status.DRAFT and self.expected_asset_count is None:
            errors["expected_asset_count"] = "已发布任务必须保存应盘数量。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("盘点任务只能通过受控盘点 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("盘点任务不得物理删除。")

    def __str__(self):
        return f"{self.task_code} {self.name}"


class InventoryTaskAssigneeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _only_actor_null_update(
            self,
            kwargs,
            actor_fields={"assigned_by", "assigned_by_id"},
            message="盘点执行人关联不可修改。",
        )

    def delete(self):
        if self.exclude(inventory_task__status=InventoryTask.Status.DRAFT).exists():
            raise ValidationError("盘点发布后执行人历史不可删除或替换。")
        return super().delete()


class InventoryTaskAssignee(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="inventory_task_assignees",
        verbose_name="公司",
    )
    inventory_task = models.ForeignKey(
        InventoryTask,
        on_delete=models.PROTECT,
        related_name="assignees",
        verbose_name="盘点任务",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inventory_task_assignments",
        verbose_name="执行用户",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_inventory_tasks",
        verbose_name="分配人",
    )
    assigned_at = models.DateTimeField("分配时间", auto_now_add=True)

    objects = InventoryTaskAssigneeQuerySet.as_manager()

    class Meta:
        verbose_name = "盘点任务执行人"
        verbose_name_plural = "盘点任务执行人"
        constraints = [
            models.UniqueConstraint(
                fields=("inventory_task", "user"), name="uq_inv_assignee_task_user"
            )
        ]

    def clean(self):
        super().clean()
        if (
            self.inventory_task_id
            and self.inventory_task.company_id != self.company_id
        ):
            raise ValidationError({"inventory_task": "任务必须属于同一公司。"})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("盘点执行人关联不可修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.inventory_task.status != InventoryTask.Status.DRAFT:
            raise ValidationError("盘点发布后执行人历史不可删除或替换。")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.inventory_task} / {self.user}"


class InventoryTaskAssetQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("应盘快照只能通过受控盘点 Service 更新状态缓存。")

    def delete(self):
        raise ValidationError("应盘快照不可删除。")


class InventoryTaskAsset(models.Model):
    class InventoryStatus(models.TextChoices):
        PENDING = "pending", "未盘"
        NORMAL = "normal", "正常"
        EXCEPTION = "exception", "异常"
        MISSING = "missing", "盘亏候选"
        RESOLVED = "resolved", "已处理"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="inventory_task_assets",
        verbose_name="公司",
    )
    inventory_task = models.ForeignKey(
        InventoryTask,
        on_delete=models.PROTECT,
        related_name="task_assets",
        verbose_name="盘点任务",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="inventory_snapshots",
        verbose_name="资产",
    )
    expected_department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="inventory_expected_department_rows",
        verbose_name="应在部门",
    )
    expected_employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="inventory_expected_employee_rows",
        verbose_name="应由责任人",
    )
    expected_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name="inventory_expected_location_rows",
        verbose_name="应在位置",
    )
    expected_asset_status = models.CharField("应为资产状态", max_length=32)
    expected_code_snapshot = models.CharField("资产编号快照", max_length=64)
    expected_name_snapshot = models.CharField("资产名称快照", max_length=200)
    expected_category_snapshot = models.CharField("实物分类快照", max_length=200)
    expected_department_snapshot = models.CharField("部门快照", max_length=200)
    expected_employee_snapshot = models.CharField("责任人快照", max_length=200)
    expected_location_path_snapshot = models.CharField("完整位置路径快照", max_length=500)
    inventory_status = models.CharField(
        "盘点状态",
        max_length=16,
        choices=InventoryStatus.choices,
        default=InventoryStatus.PENDING,
    )

    objects = InventoryTaskAssetQuerySet.as_manager()

    class Meta:
        verbose_name = "盘点应盘快照"
        verbose_name_plural = "盘点应盘快照"
        ordering = ("expected_code_snapshot", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("inventory_task", "asset"), name="uq_inv_task_asset"
            ),
            models.CheckConstraint(
                condition=Q(
                    inventory_status__in=(
                        "pending",
                        "normal",
                        "exception",
                        "missing",
                        "resolved",
                    )
                ),
                name="ck_inv_task_asset_status",
            ),
            models.CheckConstraint(
                condition=Q(expected_asset_status__in=tuple(Asset.AssetStatus.values)),
                name="ck_inv_task_asset_expected_status",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(expected_code_snapshot="")
                    & ~Q(expected_name_snapshot="")
                    & ~Q(expected_category_snapshot="")
                    & ~Q(expected_department_snapshot="")
                    & ~Q(expected_employee_snapshot="")
                    & ~Q(expected_location_path_snapshot="")
                ),
                name="ck_inv_task_asset_snapshots",
            ),
        ]

    def clean(self):
        super().clean()
        refs = {
            "inventory_task": self.inventory_task if self.inventory_task_id else None,
            "asset": self.asset if self.asset_id else None,
            "expected_department": (
                self.expected_department if self.expected_department_id else None
            ),
            "expected_employee": self.expected_employee if self.expected_employee_id else None,
            "expected_location": self.expected_location if self.expected_location_id else None,
        }
        errors = {
            field: "应盘快照引用必须属于同一公司。"
            for field, obj in refs.items()
            if obj is not None and obj.company_id != self.company_id
        }
        if self.inventory_task_id and self.inventory_task.status == InventoryTask.Status.DRAFT:
            errors["inventory_task"] = "草稿任务不得生成应盘快照。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("应盘快照不可编辑。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("应盘快照不可删除。")

    def __str__(self):
        return f"{self.inventory_task} / {self.expected_code_snapshot}"


class InventoryScanQuerySet(models.QuerySet):
    def update(self, **kwargs):
        actor_fields = {"scanned_by", "scanned_by_id"}
        if set(kwargs).issubset(actor_fields) and all(
            value is None for value in kwargs.values()
        ):
            return super().update(**kwargs)
        raise ValidationError("盘点扫描只允许受控追加和失效旧结果。")

    def delete(self):
        raise ValidationError("盘点扫描历史不可删除。")


class InventoryScan(models.Model):
    class ScanMode(models.TextChoices):
        NORMAL = "normal", "普通扫描"
        SUPPLEMENTAL = "supplemental", "受控补盘"

    class Result(models.TextChoices):
        NORMAL = "normal", "正常"
        LOCATION_MISMATCH = "location_mismatch", "位置异常"
        RESPONSIBLE_MISMATCH = "responsible_mismatch", "责任人异常"
        STATUS_MISMATCH = "status_mismatch", "状态异常"
        MULTIPLE_MISMATCH = "multiple_mismatch", "多项异常"
        OTHER_MISMATCH = "other_mismatch", "其他异常"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="inventory_scans",
        verbose_name="公司",
    )
    inventory_task = models.ForeignKey(
        InventoryTask,
        on_delete=models.PROTECT,
        related_name="scans",
        verbose_name="盘点任务",
    )
    task_asset = models.ForeignKey(
        InventoryTaskAsset,
        on_delete=models.PROTECT,
        related_name="scans",
        verbose_name="应盘快照",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="inventory_scans",
        verbose_name="资产",
    )
    scan_mode = models.CharField("扫描模式", max_length=16, choices=ScanMode.choices)
    supplement_reason = models.TextField("补盘原因", blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_scans",
        verbose_name="扫描人",
    )
    scanned_at = models.DateTimeField("扫描时间")
    actual_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_actual_location_scans",
        verbose_name="实际位置",
    )
    actual_employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_actual_employee_scans",
        verbose_name="实际责任人",
    )
    actual_status = models.CharField("实际状态", max_length=32)
    result = models.CharField("扫描结果", max_length=32, choices=Result.choices)
    note = models.TextField("说明", blank=True)
    is_effective = models.BooleanField("当前有效结果", default=True)
    supersedes_scan = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="superseded_by_scans",
        verbose_name="替代的扫描",
    )
    idempotency_key = models.CharField("幂等键", max_length=128)

    objects = InventoryScanQuerySet.as_manager()

    class Meta:
        verbose_name = "盘点扫描"
        verbose_name_plural = "盘点扫描"
        ordering = ("task_asset_id", "scanned_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_inv_scan_company_idem",
            ),
            models.UniqueConstraint(
                fields=("task_asset",),
                condition=Q(is_effective=True),
                name="uq_inv_scan_effective_task_asset",
            ),
            models.CheckConstraint(
                condition=Q(scan_mode__in=("normal", "supplemental")),
                name="ck_inv_scan_mode",
            ),
            models.CheckConstraint(
                condition=(
                    Q(scan_mode="normal", supplement_reason="")
                    | (Q(scan_mode="supplemental") & ~Q(supplement_reason=""))
                ),
                name="ck_inv_scan_mode_reason",
            ),
            models.CheckConstraint(
                condition=Q(
                    result__in=(
                        "normal",
                        "location_mismatch",
                        "responsible_mismatch",
                        "status_mismatch",
                        "multiple_mismatch",
                        "other_mismatch",
                    )
                ),
                name="ck_inv_scan_result",
            ),
            models.CheckConstraint(
                condition=(~Q(result="other_mismatch") | ~Q(note="")),
                name="ck_inv_scan_other_note",
            ),
            models.CheckConstraint(
                condition=Q(actual_status__in=tuple(Asset.AssetStatus.values)),
                name="ck_inv_scan_actual_status",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""), name="ck_inv_scan_idem_nonempty"
            ),
            models.CheckConstraint(
                condition=(
                    Q(supersedes_scan__isnull=True)
                    | ~Q(id=models.F("supersedes_scan"))
                ),
                name="ck_inv_scan_not_self_supersede",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        refs = {
            "inventory_task": self.inventory_task if self.inventory_task_id else None,
            "task_asset": self.task_asset if self.task_asset_id else None,
            "asset": self.asset if self.asset_id else None,
            "actual_location": self.actual_location if self.actual_location_id else None,
            "actual_employee": self.actual_employee if self.actual_employee_id else None,
        }
        for field, obj in refs.items():
            if obj is not None and obj.company_id != self.company_id:
                errors[field] = "扫描引用必须属于同一公司。"
        if self.task_asset_id:
            if self.task_asset.inventory_task_id != self.inventory_task_id:
                errors["task_asset"] = "扫描快照不属于该任务。"
            if self.task_asset.asset_id != self.asset_id:
                errors["asset"] = "扫描资产与应盘快照不一致。"
        if self.supersedes_scan_id:
            old = self.supersedes_scan
            if old.company_id != self.company_id or old.task_asset_id != self.task_asset_id:
                errors["supersedes_scan"] = "只能替代同公司同快照的扫描。"
        if self._state.adding and self.scanned_by_id is None:
            errors["scanned_by"] = "扫描必须记录操作人。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("盘点扫描历史不可编辑。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("盘点扫描历史不可删除。")

    def __str__(self):
        return f"{self.task_asset} / {self.get_result_display()}"


class InventoryResolutionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        actor_fields = {"resolved_by", "resolved_by_id"}
        if set(kwargs).issubset(actor_fields) and all(
            value is None for value in kwargs.values()
        ):
            return super().update(**kwargs)
        raise ValidationError("盘点处理结论只允许受控追加和标记被更正。")

    def delete(self):
        raise ValidationError("盘点处理结论不可删除。")


class InventoryResolution(models.Model):
    class ResolutionType(models.TextChoices):
        MASTER_UPDATED = "master_updated", "已执行主档变动"
        MASTER_CONFIRMED = "master_confirmed", "确认主档无误"
        LOSS_CONFIRMED = "loss_confirmed", "确认盘亏"
        OTHER = "other", "其他结论"

    class Status(models.TextChoices):
        ACTIVE = "active", "当前结论"
        SUPERSEDED = "superseded", "已被更正"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="inventory_resolutions",
        verbose_name="公司",
    )
    inventory_task_asset = models.ForeignKey(
        InventoryTaskAsset,
        on_delete=models.PROTECT,
        related_name="resolutions",
        verbose_name="应盘快照",
    )
    resolution_type = models.CharField(
        "结论类型", max_length=32, choices=ResolutionType.choices
    )
    conclusion = models.TextField("处理结论")
    movement = models.ForeignKey(
        AssetMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_resolutions",
        verbose_name="主档变动",
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    supersedes_resolution = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="correction_resolutions",
        verbose_name="更正的原结论",
    )
    correction_reason = models.TextField("更正原因", blank=True)
    idempotency_key = models.CharField("幂等键", max_length=128)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_resolutions",
        verbose_name="处理人",
    )
    resolved_at = models.DateTimeField("处理时间")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = InventoryResolutionQuerySet.as_manager()

    class Meta:
        verbose_name = "盘点处理结论"
        verbose_name_plural = "盘点处理结论"
        ordering = ("inventory_task_asset_id", "resolved_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_inv_resolution_company_idem",
            ),
            models.UniqueConstraint(
                fields=("inventory_task_asset",),
                condition=Q(status="active"),
                name="uq_inv_resolution_active_task_asset",
            ),
            models.CheckConstraint(
                condition=Q(
                    resolution_type__in=(
                        "master_updated",
                        "master_confirmed",
                        "loss_confirmed",
                        "other",
                    )
                ),
                name="ck_inv_resolution_type",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("active", "superseded")),
                name="ck_inv_resolution_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(resolution_type="master_updated", movement__isnull=False)
                    | (~Q(resolution_type="master_updated") & Q(movement__isnull=True))
                ),
                name="ck_inv_resolution_movement",
            ),
            models.CheckConstraint(
                condition=(
                    Q(supersedes_resolution__isnull=True, correction_reason="")
                    | (
                        Q(supersedes_resolution__isnull=False)
                        & ~Q(correction_reason="")
                    )
                ),
                name="ck_inv_resolution_correction",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(conclusion="") & ~Q(idempotency_key="")
                ),
                name="ck_inv_resolution_required_text",
            ),
            models.CheckConstraint(
                condition=(
                    Q(supersedes_resolution__isnull=True)
                    | ~Q(id=models.F("supersedes_resolution"))
                ),
                name="ck_inv_resolution_not_self",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.inventory_task_asset_id
            and self.inventory_task_asset.company_id != self.company_id
        ):
            errors["inventory_task_asset"] = "盘点快照必须属于同一公司。"
        if self.movement_id:
            if self.movement.company_id != self.company_id:
                errors["movement"] = "主档变动必须属于同一公司。"
            elif (
                self.inventory_task_asset_id
                and self.movement.asset_id != self.inventory_task_asset.asset_id
            ):
                errors["movement"] = "主档变动必须属于该快照资产。"
        if self.supersedes_resolution_id:
            old = self.supersedes_resolution
            if (
                old.company_id != self.company_id
                or old.inventory_task_asset_id != self.inventory_task_asset_id
            ):
                errors["supersedes_resolution"] = "只能更正同一快照的原结论。"
        if self._state.adding and self.resolved_by_id is None:
            errors["resolved_by"] = "处理结论必须记录处理人。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("盘点处理结论不可编辑。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("盘点处理结论不可删除。")

    def __str__(self):
        return f"{self.inventory_task_asset} / {self.get_resolution_type_display()}"


class InventorySurplusQuerySet(models.QuerySet):
    def update(self, **kwargs):
        actor_fields = {
            "found_by",
            "found_by_id",
            "resolved_by",
            "resolved_by_id",
        }
        if set(kwargs).issubset(actor_fields) and all(
            value is None for value in kwargs.values()
        ):
            return super().update(**kwargs)
        raise ValidationError("盘盈记录只能通过受控盘盈 Service 修改。")

    def delete(self):
        raise ValidationError("盘盈记录不可删除。")


class InventorySurplus(models.Model):
    class ResolutionStatus(models.TextChoices):
        PENDING = "pending", "待确认"
        CONVERTED_TO_DRAFT = "converted_to_draft", "已转资产草稿"
        NOT_COMPANY = "not_company", "非公司资产"
        DUPLICATE = "duplicate", "重复记录"
        OTHER = "other", "其他处理"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="inventory_surpluses",
        verbose_name="公司",
    )
    inventory_task = models.ForeignKey(
        InventoryTask,
        on_delete=models.PROTECT,
        related_name="surpluses",
        verbose_name="盘点任务",
    )
    temporary_name = models.CharField("临时名称", max_length=200)
    temporary_category_text = models.CharField("实物分类描述", max_length=200, blank=True)
    temporary_location_text = models.CharField("发现位置", max_length=500)
    found_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="found_inventory_surpluses",
        verbose_name="发现人",
    )
    found_at = models.DateTimeField("发现时间")
    resolution_status = models.CharField(
        "处理状态",
        max_length=32,
        choices=ResolutionStatus.choices,
        default=ResolutionStatus.PENDING,
    )
    linked_asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="source_inventory_surpluses",
        verbose_name="转建资产草稿",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_inventory_surpluses",
        verbose_name="处理人",
    )
    resolved_at = models.DateTimeField("处理时间", null=True, blank=True)
    remark = models.TextField("说明", blank=True)
    idempotency_key = models.CharField("幂等键", max_length=128)

    objects = InventorySurplusQuerySet.as_manager()

    class Meta:
        verbose_name = "盘盈记录"
        verbose_name_plural = "盘盈记录"
        ordering = ("inventory_task_id", "found_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_inv_surplus_company_idem",
            ),
            models.CheckConstraint(
                condition=Q(
                    resolution_status__in=(
                        "pending",
                        "converted_to_draft",
                        "not_company",
                        "duplicate",
                        "other",
                    )
                ),
                name="ck_inv_surplus_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        resolution_status="pending",
                        linked_asset__isnull=True,
                        resolved_by__isnull=True,
                        resolved_at__isnull=True,
                        remark="",
                    )
                    | (
                        Q(
                            resolution_status="converted_to_draft",
                            linked_asset__isnull=False,
                            resolved_at__isnull=False,
                        )
                        & ~Q(remark="")
                    )
                    | (
                        Q(
                            resolution_status__in=("not_company", "duplicate", "other"),
                            linked_asset__isnull=True,
                            resolved_at__isnull=False,
                        )
                        & ~Q(remark="")
                    )
                ),
                name="ck_inv_surplus_resolution_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(temporary_name="")
                    & ~Q(temporary_location_text="")
                    & ~Q(idempotency_key="")
                ),
                name="ck_inv_surplus_required_text",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.inventory_task_id
            and self.inventory_task.company_id != self.company_id
        ):
            errors["inventory_task"] = "盘盈任务必须属于同一公司。"
        if self.linked_asset_id and self.linked_asset.company_id != self.company_id:
            errors["linked_asset"] = "转建草稿必须属于同一公司。"
        if self.linked_asset_id and self.linked_asset.asset_status != Asset.AssetStatus.DRAFT:
            errors["linked_asset"] = "盘盈只能转为资产草稿。"
        if self._state.adding and self.found_by_id is None:
            errors["found_by"] = "盘盈必须记录发现人。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("盘盈记录只能通过受控盘盈 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("盘盈记录不可删除。")

    def __str__(self):
        return f"{self.inventory_task} / {self.temporary_name}"
