import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.assets.models import Asset, AssetDisposal, AssetLoan, AssetMovement
from apps.masterdata.models import Company, Department, Employee, Location


def _actor_null_only(queryset, kwargs, actor_fields, message):
    if kwargs and set(kwargs).issubset(actor_fields) and all(
        value is None for value in kwargs.values()
    ):
        return models.QuerySet.update(queryset, **kwargs)
    raise ValidationError(message)


class EmployeeAssetClearanceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _actor_null_only(
            self,
            kwargs,
            {"initiated_by", "initiated_by_id", "completed_by", "completed_by_id"},
            "离职清退只能通过受控清退 Service 修改。",
        )

    def delete(self):
        raise ValidationError("离职清退记录不得物理删除。")


class EmployeeAssetClearance(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "处理中"
        BLOCKED = "blocked", "存在未清资产"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="employee_asset_clearances",
        verbose_name="公司",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="asset_clearances",
        verbose_name="离职员工",
    )
    supplements_clearance = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supplementary_clearances",
        verbose_name="被补充的原清退单",
    )
    supplement_reason = models.TextField("补充清退原因", blank=True)
    initiated_at = models.DateTimeField("发起时间")
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="initiated_employee_asset_clearances",
        verbose_name="发起人",
    )
    total_assets_snapshot = models.PositiveIntegerField("资产总数", default=0)
    unresolved_assets = models.PositiveIntegerField("未解决资产数", default=0)
    total_supply_custodies_snapshot = models.PositiveIntegerField(
        "数量型耐用品保管总数", default=0
    )
    unresolved_supply_custodies = models.PositiveIntegerField(
        "未解决数量型耐用品保管数", default=0
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.OPEN
    )
    completed_at = models.DateTimeField("完成时间", null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_employee_asset_clearances",
        verbose_name="完成人",
    )
    remark = models.TextField("备注", blank=True)
    idempotency_key = models.CharField("幂等键", max_length=128)

    objects = EmployeeAssetClearanceQuerySet.as_manager()

    class Meta:
        verbose_name = "员工离职资产清退"
        verbose_name_plural = "员工离职资产清退"
        ordering = ("-initiated_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_clearance_company_idem",
            ),
            models.UniqueConstraint(
                fields=("company", "employee"),
                condition=Q(status__in=("open", "blocked")),
                name="uq_clearance_employee_active",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("open", "blocked", "completed", "cancelled")),
                name="ck_clearance_status",
            ),
            models.CheckConstraint(
                condition=Q(unresolved_assets__lte=models.F("total_assets_snapshot")),
                name="ck_clearance_counts",
            ),
            models.CheckConstraint(
                condition=Q(
                    unresolved_supply_custodies__lte=models.F(
                        "total_supply_custodies_snapshot"
                    )
                ),
                name="ck_clearance_supply_counts",
            ),
            models.CheckConstraint(
                condition=(
                    Q(supplements_clearance__isnull=True, supplement_reason="")
                    | (
                        Q(supplements_clearance__isnull=False)
                        & ~Q(supplement_reason="")
                    )
                ),
                name="ck_clearance_supplement_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status__in=("open", "blocked", "cancelled"),
                        completed_at__isnull=True,
                        completed_by__isnull=True,
                    )
                    | Q(status="completed", completed_at__isnull=False)
                ),
                name="ck_clearance_completion_fields",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""),
                name="ck_clearance_idem_nonempty",
            ),
        ]

    @property
    def is_supplement(self):
        return self.supplements_clearance_id is not None

    def clean(self):
        super().clean()
        errors = {}
        if self.employee_id and self.employee.company_id != self.company_id:
            errors["employee"] = "离职员工必须属于同一公司。"
        if self.supplements_clearance_id:
            original = self.supplements_clearance
            if original.pk == self.pk:
                errors["supplements_clearance"] = "补充清退不能指向自身。"
            elif (
                original.company_id != self.company_id
                or original.employee_id != self.employee_id
            ):
                errors["supplements_clearance"] = "原清退单必须属于同公司同一员工。"
            elif original.status != self.Status.COMPLETED:
                errors["supplements_clearance"] = "只能补充已完成的清退单。"
            elif original.supplements_clearance_id is not None:
                errors["supplements_clearance"] = "补充清退必须直接指向首次清退单。"
            if not str(self.supplement_reason or "").strip():
                errors["supplement_reason"] = "补充清退原因必填。"
        elif self.supplement_reason:
            errors["supplement_reason"] = "首次清退不得填写补充原因。"
        if self.unresolved_assets > self.total_assets_snapshot:
            errors["unresolved_assets"] = "未解决数量不得大于资产总数。"
        if self.unresolved_supply_custodies > self.total_supply_custodies_snapshot:
            errors["unresolved_supply_custodies"] = "未解决耐用品数量不得大于耐用品总数。"
        if not str(self.idempotency_key or "").strip():
            errors["idempotency_key"] = "幂等键不能为空。"
        if self._state.adding and self.initiated_by_id is None:
            errors["initiated_by"] = "清退发起人必填。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("离职清退只能通过受控清退 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("离职清退记录不得物理删除。")

    def __str__(self):
        kind = "补充清退" if self.is_supplement else "首次清退"
        return f"{self.employee} / {kind}"


class EmployeeAssetClearanceItemQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _actor_null_only(
            self,
            kwargs,
            {"resolved_by", "resolved_by_id"},
            "清退项目只能通过受控清退 Service 解决。",
        )

    def delete(self):
        raise ValidationError("离职清退项目不得物理删除。")


class EmployeeAssetClearanceItem(models.Model):
    class SourceType(models.TextChoices):
        RESPONSIBILITY = "responsibility", "当前责任"
        INTERNAL_LOAN = "internal_loan", "内部借用"
        BOTH = "both", "当前责任及内部借用"

    class Resolution(models.TextChoices):
        PENDING = "pending", "待处理"
        DISPOSAL_IN_PROGRESS = "disposal_in_progress", "处置中"
        RETURNED = "returned", "已归还"
        TRANSFERRED = "transferred", "已转交"
        DISPOSED = "disposed", "已处置"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="employee_asset_clearance_items",
        verbose_name="公司",
    )
    clearance = models.ForeignKey(
        EmployeeAssetClearance,
        on_delete=models.PROTECT,
        related_name="items",
        verbose_name="清退单",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="clearance_items",
        verbose_name="资产",
    )
    source_type = models.CharField(
        "来源类型", max_length=24, choices=SourceType.choices
    )
    source_loan = models.ForeignKey(
        AssetLoan,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="clearance_items",
        verbose_name="来源内部借用",
    )
    association_effective_at = models.DateTimeField("关联生效时间")
    discovered_at = models.DateTimeField("发现时间")
    addition_reason = models.TextField("后补原因", blank=True)
    asset_code_snapshot = models.CharField("资产编号快照", max_length=64)
    asset_name_snapshot = models.CharField("资产名称快照", max_length=200)
    original_department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="clearance_item_snapshots",
        verbose_name="原部门",
    )
    original_employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="clearance_item_snapshots",
        verbose_name="原责任人",
    )
    original_location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name="clearance_item_snapshots",
        verbose_name="原位置",
    )
    original_department_snapshot = models.CharField("原部门快照", max_length=200)
    original_employee_snapshot = models.CharField("原责任人快照", max_length=200)
    original_location_path_snapshot = models.CharField("原位置路径快照", max_length=500)
    original_status = models.CharField("原资产状态", max_length=32)
    added_during_clearance = models.BooleanField("清退期间后补发现", default=False)
    resolution = models.CharField(
        "解决方式",
        max_length=24,
        choices=Resolution.choices,
        default=Resolution.PENDING,
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_employee_asset_clearance_items",
        verbose_name="处理人",
    )
    resolved_at = models.DateTimeField("处理时间", null=True, blank=True)
    movement = models.ForeignKey(
        AssetMovement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="clearance_items",
        verbose_name="解决变动证据",
    )
    disposal = models.ForeignKey(
        AssetDisposal,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="clearance_items",
        verbose_name="解决处置证据",
    )
    remark = models.TextField("备注", blank=True)

    objects = EmployeeAssetClearanceItemQuerySet.as_manager()

    class Meta:
        verbose_name = "员工离职资产清退项目"
        verbose_name_plural = "员工离职资产清退项目"
        ordering = ("clearance_id", "asset_code_snapshot", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("clearance", "asset"),
                name="uq_clearance_item_asset",
            ),
            models.CheckConstraint(
                condition=Q(source_type__in=("responsibility", "internal_loan", "both")),
                name="ck_clearance_item_source",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source_type="responsibility", source_loan__isnull=True)
                    | Q(
                        source_type__in=("internal_loan", "both"),
                        source_loan__isnull=False,
                    )
                ),
                name="ck_clearance_item_source_loan",
            ),
            models.CheckConstraint(
                condition=(
                    Q(added_during_clearance=False, addition_reason="")
                    | (Q(added_during_clearance=True) & ~Q(addition_reason=""))
                ),
                name="ck_clearance_item_addition",
            ),
            models.CheckConstraint(
                condition=Q(
                    resolution__in=(
                        "pending",
                        "disposal_in_progress",
                        "returned",
                        "transferred",
                        "disposed",
                    )
                ),
                name="ck_clearance_item_resolution",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        resolution="pending",
                        resolved_by__isnull=True,
                        resolved_at__isnull=True,
                        movement__isnull=True,
                        disposal__isnull=True,
                    )
                    | Q(
                        resolution="disposal_in_progress",
                        resolved_by__isnull=True,
                        resolved_at__isnull=True,
                        movement__isnull=True,
                        disposal__isnull=False,
                    )
                    | Q(
                        resolution__in=("returned", "transferred"),
                        resolved_at__isnull=False,
                        movement__isnull=False,
                        disposal__isnull=True,
                    )
                    | Q(
                        resolution="disposed",
                        resolved_at__isnull=False,
                        movement__isnull=True,
                        disposal__isnull=False,
                    )
                ),
                name="ck_clearance_item_evidence",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(asset_code_snapshot="")
                    & ~Q(asset_name_snapshot="")
                    & ~Q(original_department_snapshot="")
                    & ~Q(original_employee_snapshot="")
                    & ~Q(original_location_path_snapshot="")
                ),
                name="ck_clearance_item_snapshots",
            ),
            models.CheckConstraint(
                condition=Q(
                    original_status__in=(
                        "pending_label",
                        "in_use",
                        "idle",
                        "loaned",
                        "under_repair",
                        "pending_disposal",
                    )
                ),
                name="ck_clearance_item_original_status",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.clearance_id and self.clearance.company_id != self.company_id:
            errors["clearance"] = "清退单必须属于同一公司。"
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "清退资产必须属于同一公司。"
        for field_name in ("original_department", "original_employee", "original_location"):
            value = getattr(self, field_name)
            if value is not None and value.company_id != self.company_id:
                errors[field_name] = "原始快照对象必须属于同一公司。"
        if self.source_type in {self.SourceType.INTERNAL_LOAN, self.SourceType.BOTH}:
            loan = self.source_loan
            if loan is None:
                errors["source_loan"] = "内部借用来源必须关联结构化借用记录。"
            elif loan.company_id != self.company_id or loan.asset_id != self.asset_id:
                errors["source_loan"] = "来源借用必须属于同公司的同一资产。"
            elif (
                self.clearance_id
                and loan.borrower_employee_id != self.clearance.employee_id
            ):
                errors["source_loan"] = "来源借用人必须是本清退员工。"
        elif self.source_loan_id is not None:
            errors["source_loan"] = "纯责任来源不得关联借用记录。"
        if self.added_during_clearance and not str(self.addition_reason or "").strip():
            errors["addition_reason"] = "后补发现必须填写原因。"
        if not self.added_during_clearance and self.addition_reason:
            errors["addition_reason"] = "初始快照不得填写后补原因。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("清退项目只能通过受控清退 Service 解决。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("离职清退项目不得物理删除。")

    def __str__(self):
        return f"{self.clearance} / {self.asset_code_snapshot}"
