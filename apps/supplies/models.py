from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from apps.masterdata.normalization import (
    clean_display_identifier,
    normalize_identifier,
)


QUANTITY = {"max_digits": 18, "decimal_places": 4}
UNIT_COST = {"max_digits": 18, "decimal_places": 6}
MONEY = {"max_digits": 18, "decimal_places": 2}
ZERO_QUANTITY = Decimal("0.0000")
ZERO_UNIT_COST = Decimal("0.000000")
ZERO_MONEY = Decimal("0.00")


class SupplyItemType(models.TextChoices):
    CONSUMABLE = "consumable", "低值易耗品"
    DURABLE_QUANTITY = "durable_quantity", "数量型低值耐用品"


class SupplyDocumentType(models.TextChoices):
    OPENING = "opening", "期初入库"
    RECEIPT = "receipt", "日常入库"
    ISSUE = "issue", "领用出库"
    RETURN = "return", "领用退回"
    TRANSFER = "transfer", "仓库调拨"
    COUNT_ADJUSTMENT = "count_adjustment", "盘点调整"
    REVERSAL = "reversal", "冲销"


class SupplyDocumentStatus(models.TextChoices):
    DRAFT = "draft", "草稿"
    POSTED = "posted", "已过账"
    REVERSED = "reversed", "已冲销"
    CANCELLED = "cancelled", "已取消"


class SupplyAdjustmentDirection(models.TextChoices):
    INCREASE = "increase", "盘盈/增加"
    DECREASE = "decrease", "盘亏/减少"


class SupplyStockMovementType(models.TextChoices):
    OPENING_IN = "opening_in", "期初入库"
    RECEIPT_IN = "receipt_in", "日常入库"
    ISSUE_OUT = "issue_out", "领用出库"
    RETURN_IN = "return_in", "领用退回"
    TRANSFER_OUT = "transfer_out", "调拨出库"
    TRANSFER_IN = "transfer_in", "调拨入库"
    COUNT_GAIN = "count_gain", "盘盈"
    COUNT_LOSS = "count_loss", "盘亏"
    REVERSAL = "reversal", "冲销"


class SupplyCustodyStatus(models.TextChoices):
    OPEN = "open", "在管"
    CLOSED = "closed", "已结清"


class SupplyCustodyAction(models.TextChoices):
    ISSUE = "issue", "领用建立"
    OPENING = "opening", "期初建立"
    RETURN = "return", "归还仓库"
    TRANSFER = "transfer", "责任转交"
    LOSS = "loss", "报损"
    SCRAP = "scrap", "报废"
    CORRECTION = "correction", "受控更正"
    REVERSAL = "reversal", "冲销"


class SupplyCountDomain(models.TextChoices):
    WAREHOUSE_STOCK = "warehouse_stock", "仓库库存盘点"
    CUSTODY = "custody", "耐用品保管盘点"


class SupplyCountStatus(models.TextChoices):
    DRAFT = "draft", "草稿"
    IN_PROGRESS = "in_progress", "进行中"
    RECONCILIATION = "reconciliation", "差异处理中"
    CLOSED = "closed", "已关闭"
    CANCELLED = "cancelled", "已取消"


class SupplyCountResolutionType(models.TextChoices):
    RETURN = "return", "归还"
    TRANSFER = "transfer", "转交"
    LOSS = "loss", "报损"
    SCRAP = "scrap", "报废"
    CORRECTION = "correction", "盘点更正"


class EmployeeSupplyClearanceResolution(models.TextChoices):
    PENDING = "pending", "待处理"
    RETURNED = "returned", "已归还"
    TRANSFERRED = "transferred", "已转交"
    LOST = "lost", "已报损"
    SCRAPPED = "scrapped", "已报废"


SUPPLY_SEQUENCE_CHOICES = (
    *SupplyDocumentType.choices,
    ("count_task", "盘点任务"),
)
SUPPLY_SEQUENCE_VALUES = (*SupplyDocumentType.values, "count_task")


def _clean_required_text(value, *, field_name, label):
    cleaned = clean_display_identifier(value)
    if not cleaned:
        raise ValidationError({field_name: f"{label}不能为空。"})
    return cleaned


def _validate_category_tree(instance):
    parent = instance.parent
    if parent is None:
        return
    if instance.pk is not None and parent.pk == instance.pk:
        raise ValidationError({"parent": "不能把分类自身设为上级。"})
    if parent.company_id != instance.company_id:
        raise ValidationError({"parent": "上级分类必须属于同一公司。"})
    seen = {instance.pk} if instance.pk is not None else set()
    current = parent
    while current is not None:
        if current.pk in seen:
            raise ValidationError({"parent": "分类树不能形成循环。"})
        seen.add(current.pk)
        current = current.parent


class SupplyAuditFields(models.Model):
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_%(class)s_records",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="最后修改人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_%(class)s_records",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class SupplyCategory(SupplyAuditFields):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_categories",
    )
    code = models.CharField("分类编码", max_length=100)
    normalized_code = models.CharField(
        "规范化分类编码", max_length=100, editable=False
    )
    name = models.CharField("分类名称", max_length=200)
    parent = models.ForeignKey(
        "self",
        verbose_name="上级分类",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    default_item_type = models.CharField(
        "默认管理模式",
        max_length=32,
        choices=SupplyItemType.choices,
        null=True,
        blank=True,
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.TextField("备注", blank=True)

    class Meta:
        verbose_name = "低值物品分类"
        verbose_name_plural = "低值物品分类"
        ordering = ("company_id", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_supply_category_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""),
                name="ck_supply_category_code_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(name=""),
                name="ck_supply_category_name_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("parent_id")),
                name="ck_supply_category_not_self",
            ),
            models.CheckConstraint(
                condition=(
                    Q(default_item_type__isnull=True)
                    | Q(default_item_type__in=SupplyItemType.values)
                ),
                name="ck_supply_category_type_valid",
            ),
        ]

    def _normalize_fields(self):
        self.code = clean_display_identifier(self.code)
        self.normalized_code = normalize_identifier(self.code)
        self.name = clean_display_identifier(self.name)

    def clean(self):
        super().clean()
        self._normalize_fields()
        if not self.normalized_code:
            raise ValidationError({"code": "分类编码不能为空。"})
        self.name = _clean_required_text(
            self.name, field_name="name", label="分类名称"
        )
        _validate_category_tree(self)

    def save(self, *args, **kwargs):
        self._normalize_fields()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if "code" in fields:
                fields.add("normalized_code")
            kwargs["update_fields"] = fields
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} / {self.name}"


class SupplyWarehouse(SupplyAuditFields):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_warehouses",
    )
    code = models.CharField("仓库编码", max_length=100)
    normalized_code = models.CharField(
        "规范化仓库编码", max_length=100, editable=False
    )
    name = models.CharField("仓库名称", max_length=200)
    location = models.ForeignKey(
        "masterdata.Location",
        verbose_name="关联位置",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_warehouses",
    )
    manager_employee = models.ForeignKey(
        "masterdata.Employee",
        verbose_name="仓库负责人",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="managed_supply_warehouses",
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.TextField("备注", blank=True)

    class Meta:
        verbose_name = "低值物品仓库"
        verbose_name_plural = "低值物品仓库"
        ordering = ("company_id", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_supply_warehouse_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""),
                name="ck_supply_warehouse_code_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(name=""),
                name="ck_supply_warehouse_name_nonempty",
            ),
        ]

    def _normalize_fields(self):
        self.code = clean_display_identifier(self.code)
        self.normalized_code = normalize_identifier(self.code)
        self.name = clean_display_identifier(self.name)

    def clean(self):
        super().clean()
        self._normalize_fields()
        if not self.normalized_code:
            raise ValidationError({"code": "仓库编码不能为空。"})
        self.name = _clean_required_text(
            self.name, field_name="name", label="仓库名称"
        )
        if self.location_id:
            if self.location.company_id != self.company_id:
                raise ValidationError({"location": "关联位置必须属于同一公司。"})
            if not self.location.is_active:
                raise ValidationError({"location": "关联位置必须处于启用状态。"})
        if self.manager_employee_id:
            employee = self.manager_employee
            if employee.company_id != self.company_id:
                raise ValidationError(
                    {"manager_employee": "仓库负责人必须属于同一公司。"}
                )
            if employee.employment_status != "active" or not employee.is_active:
                raise ValidationError(
                    {"manager_employee": "仓库负责人必须是在职且启用的员工。"}
                )
            if not employee.department_id or not employee.department.is_active:
                raise ValidationError(
                    {"manager_employee": "仓库负责人必须属于启用部门。"}
                )

    def save(self, *args, **kwargs):
        self._normalize_fields()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if "code" in fields:
                fields.add("normalized_code")
            kwargs["update_fields"] = fields
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} / {self.name}"


class SupplyItem(SupplyAuditFields):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_items",
    )
    item_code = models.CharField("物品编码", max_length=100)
    normalized_item_code = models.CharField(
        "规范化物品编码", max_length=100, editable=False
    )
    name = models.CharField("物品名称", max_length=200)
    category = models.ForeignKey(
        SupplyCategory,
        verbose_name="分类",
        on_delete=models.PROTECT,
        related_name="items",
    )
    item_type = models.CharField(
        "管理模式", max_length=32, choices=SupplyItemType.choices
    )
    unit = models.CharField("计量单位", max_length=32)
    specification = models.CharField("规格", max_length=200, blank=True)
    model = models.CharField("型号", max_length=100, blank=True)
    brand = models.CharField("品牌", max_length=100, blank=True)
    minimum_stock_quantity = models.DecimalField(
        "最低库存数量", default=ZERO_QUANTITY, **QUANTITY
    )
    default_warehouse = models.ForeignKey(
        SupplyWarehouse,
        verbose_name="默认仓库",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="default_items",
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.TextField("备注", blank=True)

    class Meta:
        verbose_name = "低值物品档案"
        verbose_name_plural = "低值物品档案"
        ordering = ("company_id", "normalized_item_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_item_code"),
                name="uq_supply_item_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_item_code=""),
                name="ck_supply_item_code_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(name=""),
                name="ck_supply_item_name_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(unit=""),
                name="ck_supply_item_unit_nonempty",
            ),
            models.CheckConstraint(
                condition=Q(item_type__in=SupplyItemType.values),
                name="ck_supply_item_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(minimum_stock_quantity__gte=0),
                name="ck_supply_item_minimum_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "item_type", "is_active"),
                name="supply_item_type_active_idx",
            ),
        ]

    def _normalize_fields(self):
        self.item_code = clean_display_identifier(self.item_code)
        self.normalized_item_code = normalize_identifier(self.item_code)
        self.name = clean_display_identifier(self.name)
        self.unit = clean_display_identifier(self.unit)

    def clean(self):
        super().clean()
        self._normalize_fields()
        if not self.normalized_item_code:
            raise ValidationError({"item_code": "物品编码不能为空。"})
        self.name = _clean_required_text(
            self.name, field_name="name", label="物品名称"
        )
        self.unit = _clean_required_text(
            self.unit, field_name="unit", label="计量单位"
        )
        if self.item_type not in SupplyItemType.values:
            raise ValidationError({"item_type": "管理模式不受支持。"})
        try:
            quantity = (
                self.minimum_stock_quantity
                if isinstance(self.minimum_stock_quantity, Decimal)
                else Decimal(str(self.minimum_stock_quantity))
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError(
                {"minimum_stock_quantity": "最低库存数量必须是有效十进制数。"}
            ) from exc
        if not quantity.is_finite():
            raise ValidationError(
                {"minimum_stock_quantity": "最低库存数量必须是有限十进制数。"}
            )
        self.minimum_stock_quantity = quantity
        if quantity < ZERO_QUANTITY:
            raise ValidationError(
                {"minimum_stock_quantity": "最低库存数量不得小于 0。"}
            )
        if self.category_id:
            if self.category.company_id != self.company_id:
                raise ValidationError({"category": "分类必须属于同一公司。"})
            if not self.category.is_active:
                raise ValidationError({"category": "分类必须处于启用状态。"})
        if self.default_warehouse_id:
            if self.default_warehouse.company_id != self.company_id:
                raise ValidationError(
                    {"default_warehouse": "默认仓库必须属于同一公司。"}
                )
            if not self.default_warehouse.is_active:
                raise ValidationError(
                    {"default_warehouse": "默认仓库必须处于启用状态。"}
                )

    def save(self, *args, **kwargs):
        self._normalize_fields()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if "item_code" in fields:
                fields.add("normalized_item_code")
            kwargs["update_fields"] = fields
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.item_code} / {self.name}"


def _only_actor_null_update(queryset, kwargs, *, actor_fields, message):
    if set(kwargs).issubset(actor_fields) and all(
        value is None for value in kwargs.values()
    ):
        return models.QuerySet.update(queryset, **kwargs)
    raise ValidationError(message)


class SupplyDocumentSequence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_document_sequences",
    )
    sequence_type = models.CharField(
        "序号类型", max_length=32, choices=SUPPLY_SEQUENCE_CHOICES
    )
    year = models.PositiveSmallIntegerField("年度")
    current_value = models.PositiveBigIntegerField("当前序号", default=0)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "低值物品单据序号"
        verbose_name_plural = "低值物品单据序号"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "sequence_type", "year"),
                name="uq_supply_doc_sequence_scope",
            ),
            models.CheckConstraint(
                condition=Q(sequence_type__in=SUPPLY_SEQUENCE_VALUES),
                name="ck_supply_doc_sequence_type",
            ),
            models.CheckConstraint(
                condition=Q(year__gte=1900, year__lte=9999),
                name="ck_supply_doc_sequence_year",
            ),
            models.CheckConstraint(
                condition=Q(current_value__gte=0),
                name="ck_supply_doc_sequence_nonnegative",
            ),
        ]

    def __str__(self):
        return f"{self.company_id}:{self.sequence_type}:{self.year}={self.current_value}"


class SupplyDocumentQuerySet(models.QuerySet):
    _ACTOR_FIELDS = {
        "created_by",
        "created_by_id",
        "posted_by",
        "posted_by_id",
        "cancelled_by",
        "cancelled_by_id",
        "reversed_by",
        "reversed_by_id",
    }

    def update(self, **kwargs):
        return _only_actor_null_update(
            self,
            kwargs,
            actor_fields=self._ACTOR_FIELDS,
            message="库存单据只能通过受控库存 Service 修改。",
        )

    def delete(self):
        if self.exclude(status=SupplyDocumentStatus.DRAFT).exists():
            raise ValidationError("非草稿库存单据不得物理删除。")
        return models.QuerySet.delete(self)


class SupplyDocument(models.Model):
    DocumentType = SupplyDocumentType
    Status = SupplyDocumentStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_documents",
    )
    document_no = models.CharField("单据编号", max_length=64)
    document_type = models.CharField(
        "单据类型", max_length=32, choices=SupplyDocumentType.choices
    )
    business_date = models.DateField("业务日期")
    source_warehouse = models.ForeignKey(
        SupplyWarehouse,
        verbose_name="来源仓库",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="source_supply_documents",
    )
    target_warehouse = models.ForeignKey(
        SupplyWarehouse,
        verbose_name="目标仓库",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="target_supply_documents",
    )
    department = models.ForeignKey(
        "masterdata.Department",
        verbose_name="领用/保管部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_documents",
    )
    employee = models.ForeignKey(
        "masterdata.Employee",
        verbose_name="领用/保管员工",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_documents",
    )
    external_reference = models.CharField("外部参考号", max_length=200, blank=True)
    counterparty_name = models.CharField("来源或往来单位", max_length=200, blank=True)
    remark = models.TextField("备注", blank=True)
    status = models.CharField(
        "状态",
        max_length=16,
        choices=SupplyDocumentStatus.choices,
        default=SupplyDocumentStatus.DRAFT,
    )
    idempotency_key = models.CharField("创建幂等键", max_length=128)
    reversal_of = models.OneToOneField(
        "self",
        verbose_name="被冲销原单",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal_document",
    )
    source_count_task = models.OneToOneField(
        "SupplyCountTask",
        verbose_name="来源盘点任务",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="adjustment_document",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_supply_documents",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="过账人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posted_supply_documents",
    )
    posted_at = models.DateTimeField("过账时间", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="取消人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cancelled_supply_documents",
    )
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    cancellation_reason = models.TextField("取消原因", blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="冲销人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reversed_supply_documents",
    )
    reversed_at = models.DateTimeField("冲销时间", null=True, blank=True)

    objects = SupplyDocumentQuerySet.as_manager()

    class Meta:
        verbose_name = "低值物品库存单据"
        verbose_name_plural = "低值物品库存单据"
        ordering = ("-business_date", "-document_no")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "document_no"),
                name="uq_supply_document_company_no",
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_supply_document_company_idem",
            ),
            models.CheckConstraint(
                condition=Q(document_type__in=SupplyDocumentType.values),
                name="ck_supply_document_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=SupplyDocumentStatus.values),
                name="ck_supply_document_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source_warehouse__isnull=True)
                    | Q(target_warehouse__isnull=True)
                    | ~Q(source_warehouse=F("target_warehouse"))
                ),
                name="ck_supply_document_warehouses_differ",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        document_type__in=("opening", "receipt"),
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | Q(
                        document_type="issue",
                        source_warehouse__isnull=False,
                        target_warehouse__isnull=True,
                        department__isnull=False,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | Q(
                        document_type="return",
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=False,
                        department__isnull=False,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | Q(
                        document_type="transfer",
                        source_warehouse__isnull=False,
                        target_warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | Q(
                        document_type="reversal",
                        reversal_of__isnull=False,
                        source_count_task__isnull=True,
                    )
                    | Q(
                        document_type="count_adjustment",
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=True,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                        source_count_task__isnull=False,
                    )
                ),
                name="ck_supply_document_s17_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="draft",
                        posted_at__isnull=True,
                        posted_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                        reversed_at__isnull=True,
                        reversed_by__isnull=True,
                    )
                    | Q(
                        status="posted",
                        posted_at__isnull=False,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                        reversed_at__isnull=True,
                        reversed_by__isnull=True,
                    )
                    | Q(
                        status="cancelled",
                        posted_at__isnull=True,
                        posted_by__isnull=True,
                        cancelled_at__isnull=False,
                        cancellation_reason__gt="",
                        reversed_at__isnull=True,
                        reversed_by__isnull=True,
                    )
                    | Q(
                        status="reversed",
                        posted_at__isnull=False,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                        reversed_at__isnull=False,
                    )
                ),
                name="ck_supply_document_status_fields",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "document_type", "status", "business_date"),
                name="supply_doc_type_status_idx",
            ),
            models.Index(
                fields=("company", "department", "business_date"),
                name="supply_doc_department_idx",
            ),
            models.Index(
                fields=("company", "target_warehouse", "business_date"),
                name="supply_doc_target_idx",
            ),
        ]

    def clean(self):
        super().clean()
        self.document_no = str(self.document_no or "").strip()
        self.idempotency_key = str(self.idempotency_key or "").strip()
        self.external_reference = str(self.external_reference or "").strip()
        self.counterparty_name = str(self.counterparty_name or "").strip()
        self.cancellation_reason = str(self.cancellation_reason or "").strip()
        if not self.document_no:
            raise ValidationError({"document_no": "单据编号不能为空。"})
        if not self.idempotency_key:
            raise ValidationError({"idempotency_key": "创建幂等键不能为空。"})
        if self.document_type in {
            SupplyDocumentType.OPENING,
            SupplyDocumentType.RECEIPT,
        }:
            if self.target_warehouse_id is None:
                raise ValidationError({"target_warehouse": "入库单必须选择目标仓库。"})
            if any(
                (
                    self.source_warehouse_id,
                    self.department_id,
                    self.employee_id,
                    self.reversal_of_id,
                )
            ):
                raise ValidationError("期初和日常入库单不得填写来源仓库、部门、员工或冲销来源。")
        elif self.document_type == SupplyDocumentType.ISSUE:
            if self.source_warehouse_id is None:
                raise ValidationError({"source_warehouse": "领用单必须选择来源仓库。"})
            if self.department_id is None:
                raise ValidationError({"department": "领用单必须选择领用部门。"})
            if self.target_warehouse_id or self.reversal_of_id:
                raise ValidationError("领用单不得填写目标仓库或冲销来源。")
        elif self.document_type == SupplyDocumentType.RETURN:
            if self.target_warehouse_id is None:
                raise ValidationError({"target_warehouse": "退回单必须选择目标仓库。"})
            if self.department_id is None:
                raise ValidationError({"department": "退回单必须保存原领用部门快照。"})
            if self.source_warehouse_id or self.reversal_of_id:
                raise ValidationError("退回单不得填写来源仓库或冲销来源。")
        elif self.document_type == SupplyDocumentType.TRANSFER:
            if self.source_warehouse_id is None or self.target_warehouse_id is None:
                raise ValidationError("调拨单必须同时选择来源仓库和目标仓库。")
            if self.source_warehouse_id == self.target_warehouse_id:
                raise ValidationError("来源仓库和目标仓库不能相同。")
            if self.department_id or self.employee_id or self.reversal_of_id:
                raise ValidationError("调拨单不得填写部门、员工或冲销来源。")
        elif self.document_type == SupplyDocumentType.REVERSAL:
            if self.reversal_of_id is None:
                raise ValidationError({"reversal_of": "冲销单必须关联被冲销原单。"})
            if self.reversal_of_id == self.pk:
                raise ValidationError({"reversal_of": "冲销单不能关联自身。"})
            if self.reversal_of.document_type == SupplyDocumentType.REVERSAL:
                raise ValidationError({"reversal_of": "冲销单不能再次作为被冲销原单。"})
        elif self.document_type == SupplyDocumentType.COUNT_ADJUSTMENT:
            if self.source_count_task_id is None:
                raise ValidationError({"source_count_task": "盘点调整单必须关联来源盘点任务。"})
            if any(
                (
                    self.source_warehouse_id,
                    self.target_warehouse_id,
                    self.department_id,
                    self.employee_id,
                    self.reversal_of_id,
                )
            ):
                raise ValidationError("盘点调整单的仓库只能由来源盘点任务确定。")
            if (
                self.source_count_task.company_id != self.company_id
                or self.source_count_task.count_domain
                != SupplyCountDomain.WAREHOUSE_STOCK
            ):
                raise ValidationError({"source_count_task": "来源必须是同公司的仓库库存盘点任务。"})
        elif self.source_count_task_id:
            raise ValidationError({"source_count_task": "只有盘点调整单可以关联盘点任务。"})
        for field_name in ("source_warehouse", "target_warehouse", "department", "employee"):
            value = getattr(self, field_name, None)
            if value is not None and value.company_id != self.company_id:
                raise ValidationError({field_name: "所选对象必须属于同一公司。"})
        if self.employee_id and self.document_type == SupplyDocumentType.ISSUE:
            if not self.department_id or self.employee.department_id != self.department_id:
                raise ValidationError({"employee": "所选员工不属于目标部门。"})
            if (
                self.employee.employment_status != "active"
                or not self.employee.is_active
                or not self.employee.department.is_active
            ):
                raise ValidationError({"employee": "所选员工必须是在职、启用且属于启用部门。"})

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            persisted = SupplyDocument._base_manager.filter(pk=self.pk).values(
                "status"
            ).first()
            if persisted:
                old_status = persisted["status"]
                if old_status != SupplyDocumentStatus.DRAFT and not getattr(
                    self, "_controlled_transition", False
                ):
                    raise ValidationError("该单据已离开草稿状态，不能普通编辑。")
                if old_status != self.status and not getattr(
                    self, "_controlled_transition", False
                ):
                    raise ValidationError("单据状态只能通过受控库存 Service 修改。")
        elif self.status != SupplyDocumentStatus.DRAFT and not getattr(
            self, "_controlled_transition", False
        ):
            raise ValidationError("新建库存单据只能处于草稿状态。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != SupplyDocumentStatus.DRAFT:
            raise ValidationError("非草稿库存单据不得物理删除。")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.document_no} / {self.get_document_type_display()}"


class SupplyDocumentLineQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("库存单据明细只能通过受控库存 Service 修改。")

    def delete(self):
        if self.exclude(document__status=SupplyDocumentStatus.DRAFT).exists():
            raise ValidationError("已过账或已取消单据的明细不得删除。")
        return models.QuerySet.delete(self)


class SupplyDocumentLine(models.Model):
    AdjustmentDirection = SupplyAdjustmentDirection

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_document_lines",
    )
    document = models.ForeignKey(
        SupplyDocument,
        verbose_name="库存单据",
        on_delete=models.PROTECT,
        related_name="lines",
    )
    line_no = models.PositiveIntegerField("行号")
    item = models.ForeignKey(
        SupplyItem,
        verbose_name="物品",
        on_delete=models.PROTECT,
        related_name="document_lines",
    )
    quantity = models.DecimalField("数量", **QUANTITY)
    entered_unit_cost = models.DecimalField(
        "录入单位成本", null=True, blank=True, **UNIT_COST
    )
    posted_unit_cost = models.DecimalField(
        "过账单位成本", null=True, blank=True, **UNIT_COST
    )
    posted_amount = models.DecimalField("过账金额", null=True, blank=True, **MONEY)
    adjustment_direction = models.CharField(
        "调整方向",
        max_length=16,
        choices=SupplyAdjustmentDirection.choices,
        null=True,
        blank=True,
    )
    source_issue_line = models.ForeignKey(
        "self",
        verbose_name="原领用明细",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="return_lines",
    )
    source_custody = models.ForeignKey(
        "SupplyCustody",
        verbose_name="原保管记录",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="source_document_lines",
    )
    line_remark = models.TextField("明细备注/0 成本原因", blank=True)

    objects = SupplyDocumentLineQuerySet.as_manager()

    class Meta:
        verbose_name = "低值物品库存单据明细"
        verbose_name_plural = "低值物品库存单据明细"
        ordering = ("document_id", "line_no")
        constraints = [
            models.UniqueConstraint(
                fields=("document", "line_no"),
                name="uq_supply_document_line_no",
            ),
            models.CheckConstraint(
                condition=Q(line_no__gte=1),
                name="ck_supply_document_line_positive_no",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="ck_supply_document_line_positive_qty",
            ),
            models.CheckConstraint(
                condition=(Q(entered_unit_cost__isnull=True) | Q(entered_unit_cost__gte=0)),
                name="ck_supply_document_line_entered_cost",
            ),
            models.CheckConstraint(
                condition=(
                    Q(entered_unit_cost__isnull=True)
                    | ~Q(entered_unit_cost=0)
                    | ~Q(line_remark="")
                ),
                name="ck_supply_document_line_zero_reason",
            ),
            models.CheckConstraint(
                condition=(Q(posted_unit_cost__isnull=True) | Q(posted_unit_cost__gte=0)),
                name="ck_supply_document_line_posted_cost",
            ),
            models.CheckConstraint(
                condition=(Q(posted_amount__isnull=True) | Q(posted_amount__gte=0)),
                name="ck_supply_document_line_posted_amount",
            ),
            models.CheckConstraint(
                condition=(
                    Q(posted_unit_cost__isnull=True, posted_amount__isnull=True)
                    | Q(posted_unit_cost__isnull=False, posted_amount__isnull=False)
                ),
                name="ck_supply_document_line_posted_pair",
            ),
            models.CheckConstraint(
                condition=(
                    Q(adjustment_direction__isnull=True)
                    | Q(adjustment_direction__in=SupplyAdjustmentDirection.values)
                ),
                name="ck_supply_document_line_direction",
            ),
        ]

    def clean(self):
        super().clean()
        from .domain import (
            ZERO_COST,
            quantize_quantity,
            quantize_unit_cost,
            validate_zero_cost_reason,
        )

        if self.document_id and self.document.company_id != self.company_id:
            raise ValidationError({"document": "单据明细必须与单据属于同一公司。"})
        if self.item_id and self.item.company_id != self.company_id:
            raise ValidationError({"item": "单据明细物品必须属于同一公司。"})
        if self.source_issue_line_id:
            source = self.source_issue_line
            if source.company_id != self.company_id:
                raise ValidationError({"source_issue_line": "原领用明细必须属于同一公司。"})
            if source.document_id == self.document_id:
                raise ValidationError({"source_issue_line": "原领用明细不能来自当前单据。"})
            if source.document.document_type != SupplyDocumentType.ISSUE:
                raise ValidationError({"source_issue_line": "只能关联领用单明细。"})
            if source.item_id != self.item_id:
                raise ValidationError({"item": "退回物品必须与原领用明细一致。"})
        if self.source_custody_id:
            custody = self.source_custody
            if custody.company_id != self.company_id or custody.item_id != self.item_id:
                raise ValidationError({"source_custody": "原保管记录与当前公司或物品不一致。"})
        self.quantity = quantize_quantity(self.quantity)
        if self.quantity <= ZERO_QUANTITY:
            raise ValidationError({"quantity": "数量必须大于 0。"})
        if self.document_id and self.document.document_type in {
            SupplyDocumentType.OPENING,
            SupplyDocumentType.RECEIPT,
        }:
            if self.entered_unit_cost is None:
                raise ValidationError({"entered_unit_cost": "期初和日常入库必须填写单位成本。"})
            self.entered_unit_cost = quantize_unit_cost(self.entered_unit_cost)
            if self.entered_unit_cost < ZERO_COST:
                raise ValidationError({"entered_unit_cost": "单位成本不得小于 0。"})
            self.line_remark = validate_zero_cost_reason(
                self.entered_unit_cost, self.line_remark
            )
            if self.source_issue_line_id or self.source_custody_id:
                raise ValidationError("入库明细不得关联原领用或原保管记录。")
        elif self.document_id and self.document.document_type in {
            SupplyDocumentType.ISSUE,
            SupplyDocumentType.TRANSFER,
        }:
            if self.entered_unit_cost is not None:
                raise ValidationError({"entered_unit_cost": "领用和调拨成本只能由系统计算。"})
            if self.source_issue_line_id or self.source_custody_id:
                raise ValidationError("领用和调拨明细不得关联原领用或原保管记录。")
        elif self.document_id and self.document.document_type == SupplyDocumentType.RETURN:
            if self.entered_unit_cost is not None:
                raise ValidationError({"entered_unit_cost": "退回成本只能由系统沿用来源成本。"})
            if self.item.item_type == SupplyItemType.CONSUMABLE:
                if self.source_issue_line_id is None:
                    raise ValidationError({"source_issue_line": "易耗品退回必须关联原领用明细。"})
                if self.source_custody_id is not None:
                    raise ValidationError({"source_custody": "易耗品退回不得关联耐用品保管。"})
            elif self.item.item_type == SupplyItemType.DURABLE_QUANTITY:
                if self.source_custody_id is None:
                    raise ValidationError({"source_custody": "耐用品归还必须关联来源保管。"})
                if (
                    self.source_issue_line_id is not None
                    and self.source_custody.origin_issue_line_id
                    != self.source_issue_line_id
                ):
                    raise ValidationError(
                        {"source_issue_line": "原领用明细不是当前来源保管的直接根来源。"}
                    )
            if not str(self.line_remark or "").strip():
                raise ValidationError({"line_remark": "退回原因不能为空。"})
        elif (
            self.document_id
            and self.document.document_type == SupplyDocumentType.COUNT_ADJUSTMENT
        ):
            if self.adjustment_direction not in SupplyAdjustmentDirection.values:
                raise ValidationError({"adjustment_direction": "盘点调整明细必须填写盘盈或盘亏方向。"})
            if self.source_issue_line_id or self.source_custody_id:
                raise ValidationError("盘点调整明细不得关联原领用或原保管记录。")
            if self.adjustment_direction == SupplyAdjustmentDirection.INCREASE:
                if self.entered_unit_cost is None:
                    raise ValidationError({"entered_unit_cost": "盘盈明细必须保存盘点确定的单位成本。"})
                self.entered_unit_cost = quantize_unit_cost(self.entered_unit_cost)
                if self.entered_unit_cost < ZERO_COST:
                    raise ValidationError({"entered_unit_cost": "盘盈单位成本不得小于 0。"})
                self.line_remark = validate_zero_cost_reason(
                    self.entered_unit_cost, self.line_remark
                )
            elif self.entered_unit_cost is not None:
                raise ValidationError({"entered_unit_cost": "盘亏成本只能由冻结余额计算。"})
        if self.document_id and self.document.document_type != SupplyDocumentType.COUNT_ADJUSTMENT:
            if self.adjustment_direction:
                raise ValidationError({"adjustment_direction": "非盘点调整明细不得填写调整方向。"})
        if self.document_id and self.document.status == SupplyDocumentStatus.DRAFT:
            if (
                self.posted_unit_cost is not None or self.posted_amount is not None
            ) and not getattr(self, "_controlled_posting", False):
                raise ValidationError("草稿明细不得预先写入过账成本或金额。")

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            status = (
                SupplyDocumentLine._base_manager.filter(pk=self.pk)
                .values_list("document__status", flat=True)
                .first()
            )
            if status != SupplyDocumentStatus.DRAFT:
                raise ValidationError("已过账或已取消单据的明细不得修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.document.status != SupplyDocumentStatus.DRAFT:
            raise ValidationError("已过账或已取消单据的明细不得删除。")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.document.document_no} #{self.line_no} {self.item}"


class SupplyStockBalanceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("库存余额只能通过受控库存 Service 更新。")

    def delete(self):
        raise ValidationError("库存余额不得通过普通操作删除。")


class SupplyStockBalance(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_stock_balances",
    )
    warehouse = models.ForeignKey(
        SupplyWarehouse,
        verbose_name="仓库",
        on_delete=models.PROTECT,
        related_name="stock_balances",
    )
    item = models.ForeignKey(
        SupplyItem,
        verbose_name="物品",
        on_delete=models.PROTECT,
        related_name="stock_balances",
    )
    quantity_on_hand = models.DecimalField(
        "库存数量", default=ZERO_QUANTITY, **QUANTITY
    )
    amount_on_hand = models.DecimalField("库存金额", default=ZERO_MONEY, **MONEY)
    average_unit_cost = models.DecimalField(
        "移动平均成本", default=ZERO_UNIT_COST, **UNIT_COST
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    objects = SupplyStockBalanceQuerySet.as_manager()

    class Meta:
        verbose_name = "低值物品库存余额"
        verbose_name_plural = "低值物品库存余额"
        ordering = ("warehouse__normalized_code", "item__normalized_item_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "warehouse", "item"),
                name="uq_supply_stock_balance_scope",
            ),
            models.CheckConstraint(
                condition=Q(quantity_on_hand__gte=0),
                name="ck_supply_stock_balance_qty",
            ),
            models.CheckConstraint(
                condition=Q(amount_on_hand__gte=0),
                name="ck_supply_stock_balance_amount",
            ),
            models.CheckConstraint(
                condition=Q(average_unit_cost__gte=0),
                name="ck_supply_stock_balance_average",
            ),
            models.CheckConstraint(
                condition=(
                    Q(quantity_on_hand__gt=0)
                    | Q(
                        quantity_on_hand=0,
                        amount_on_hand=0,
                        average_unit_cost=0,
                    )
                ),
                name="ck_supply_stock_balance_zero",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "item"), name="supply_balance_item_idx"
            ),
        ]

    def clean(self):
        super().clean()
        from .domain import (
            calculate_average_unit_cost,
            quantize_money,
            quantize_quantity,
            quantize_unit_cost,
        )

        if self.warehouse_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "库存仓库必须属于同一公司。"})
        if self.item_id and self.item.company_id != self.company_id:
            raise ValidationError({"item": "库存物品必须属于同一公司。"})
        self.quantity_on_hand = quantize_quantity(self.quantity_on_hand)
        self.amount_on_hand = quantize_money(self.amount_on_hand)
        self.average_unit_cost = quantize_unit_cost(self.average_unit_cost)
        expected = calculate_average_unit_cost(
            self.quantity_on_hand, self.amount_on_hand
        )
        if self.quantity_on_hand == ZERO_QUANTITY and self.average_unit_cost != expected:
            raise ValidationError("库存数量为 0 时金额和平均成本必须同时为 0。")

    def save(self, *args, **kwargs):
        if not getattr(self, "_controlled_mutation", False):
            raise ValidationError("库存余额只能通过受控库存 Service 保存。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("库存余额不得通过普通操作删除。")

    def __str__(self):
        return f"{self.warehouse} / {self.item}: {self.quantity_on_hand}"


class SupplyStockLedgerQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _only_actor_null_update(
            self,
            kwargs,
            actor_fields={"created_by", "created_by_id"},
            message="库存流水只允许追加，不能更新。",
        )

    def delete(self):
        raise ValidationError("库存流水只允许追加，不能删除。")


class SupplyStockLedger(models.Model):
    MovementType = SupplyStockMovementType

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_stock_ledgers",
    )
    warehouse = models.ForeignKey(
        SupplyWarehouse,
        verbose_name="仓库",
        on_delete=models.PROTECT,
        related_name="stock_ledgers",
    )
    item = models.ForeignKey(
        SupplyItem,
        verbose_name="物品",
        on_delete=models.PROTECT,
        related_name="stock_ledgers",
    )
    document = models.ForeignKey(
        SupplyDocument,
        verbose_name="库存单据",
        on_delete=models.PROTECT,
        related_name="stock_ledgers",
    )
    document_line = models.ForeignKey(
        SupplyDocumentLine,
        verbose_name="库存单据明细",
        on_delete=models.PROTECT,
        related_name="stock_ledgers",
    )
    movement_type = models.CharField(
        "流水类型", max_length=32, choices=SupplyStockMovementType.choices
    )
    quantity_delta = models.DecimalField("数量变动", **QUANTITY)
    amount_delta = models.DecimalField("金额变动", **MONEY)
    unit_cost = models.DecimalField("单位成本", **UNIT_COST)
    quantity_before = models.DecimalField("变动前数量", **QUANTITY)
    quantity_after = models.DecimalField("变动后数量", **QUANTITY)
    amount_before = models.DecimalField("变动前金额", **MONEY)
    amount_after = models.DecimalField("变动后金额", **MONEY)
    average_unit_cost_before = models.DecimalField("变动前平均成本", **UNIT_COST)
    average_unit_cost_after = models.DecimalField("变动后平均成本", **UNIT_COST)
    occurred_at = models.DateTimeField("发生时间")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_supply_stock_ledgers",
    )
    reverses_ledger = models.OneToOneField(
        "self",
        verbose_name="被冲销流水",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal_ledger",
    )

    objects = SupplyStockLedgerQuerySet.as_manager()

    class Meta:
        verbose_name = "低值物品库存流水"
        verbose_name_plural = "低值物品库存流水"
        ordering = ("-occurred_at", "document__document_no", "document_line__line_no")
        constraints = [
            models.UniqueConstraint(
                fields=("document_line", "warehouse", "movement_type"),
                name="uq_supply_stock_ledger_posting",
            ),
            models.CheckConstraint(
                condition=Q(movement_type__in=SupplyStockMovementType.values),
                name="ck_supply_stock_ledger_type",
            ),
            models.CheckConstraint(
                condition=~Q(quantity_delta=0),
                name="ck_supply_stock_ledger_delta",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__gte=0),
                name="ck_supply_stock_ledger_cost",
            ),
            models.CheckConstraint(
                condition=Q(
                    quantity_before__gte=0,
                    quantity_after__gte=0,
                    amount_before__gte=0,
                    amount_after__gte=0,
                    average_unit_cost_before__gte=0,
                    average_unit_cost_after__gte=0,
                ),
                name="ck_supply_stock_ledger_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(
                    quantity_after=F("quantity_before") + F("quantity_delta")
                ),
                name="ck_supply_stock_ledger_qty_equation",
            ),
            models.CheckConstraint(
                condition=Q(amount_after=F("amount_before") + F("amount_delta")),
                name="ck_supply_stock_ledger_amount_equation",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "warehouse", "item", "occurred_at"),
                name="supply_ledger_scope_at_idx",
            ),
            models.Index(
                fields=("company", "document"), name="supply_ledger_document_idx"
            ),
            models.Index(
                fields=("company", "-occurred_at", "-id"),
                name="supply_ledger_company_time_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if not self._state.adding:
            raise ValidationError("库存流水只允许追加，不能更新。")
        if self.warehouse_id and self.warehouse.company_id != self.company_id:
            raise ValidationError({"warehouse": "流水仓库必须属于同一公司。"})
        if self.item_id and self.item.company_id != self.company_id:
            raise ValidationError({"item": "流水物品必须属于同一公司。"})
        if self.document_id and self.document.company_id != self.company_id:
            raise ValidationError({"document": "流水单据必须属于同一公司。"})
        if self.document_line_id:
            if self.document_line.company_id != self.company_id:
                raise ValidationError({"document_line": "流水单据行必须属于同一公司。"})
            if self.document_line.document_id != self.document_id:
                raise ValidationError({"document_line": "流水单据行与单据不一致。"})

    def save(self, *args, **kwargs):
        if not self._state.adding or not getattr(self, "_controlled_insert", False):
            raise ValidationError("库存流水只允许由受控过账 Service 追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("库存流水只允许追加，不能删除。")

    def __str__(self):
        return f"{self.document.document_no} #{self.document_line.line_no} {self.movement_type}"


class SupplyCustodyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("耐用品保管余额只能通过受控库存 Service 更新。")

    def delete(self):
        raise ValidationError("耐用品保管记录不得通过普通操作删除。")


class SupplyCustody(models.Model):
    Status = SupplyCustodyStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_custodies",
    )
    item = models.ForeignKey(
        SupplyItem,
        verbose_name="物品",
        on_delete=models.PROTECT,
        related_name="custodies",
    )
    origin_issue_line = models.OneToOneField(
        SupplyDocumentLine,
        verbose_name="来源领用明细",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_custody",
    )
    origin_import_row = models.OneToOneField(
        "masterdata.ImportRow",
        verbose_name="来源期初保管导入行",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_custody",
    )
    parent_custody = models.ForeignKey(
        "self",
        verbose_name="来源父保管",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="child_custodies",
    )
    department = models.ForeignKey(
        "masterdata.Department",
        verbose_name="责任部门",
        on_delete=models.PROTECT,
        related_name="supply_custodies",
    )
    employee = models.ForeignKey(
        "masterdata.Employee",
        verbose_name="责任员工",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_custodies",
    )
    current_quantity = models.DecimalField("当前保管数量", **QUANTITY)
    current_amount = models.DecimalField("当前保管金额", **MONEY)
    unit_cost_snapshot = models.DecimalField("单位成本快照", **UNIT_COST)
    started_on = models.DateField("开始日期")
    status = models.CharField(
        "状态",
        max_length=16,
        choices=SupplyCustodyStatus.choices,
        default=SupplyCustodyStatus.OPEN,
    )
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    objects = SupplyCustodyQuerySet.as_manager()

    class Meta:
        verbose_name = "数量型低值耐用品保管"
        verbose_name_plural = "数量型低值耐用品保管"
        ordering = ("-started_on", "item__normalized_item_code")
        constraints = [
            models.CheckConstraint(
                condition=Q(current_quantity__gte=0),
                name="ck_supply_custody_qty_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(current_amount__gte=0),
                name="ck_supply_custody_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(unit_cost_snapshot__gte=0),
                name="ck_supply_custody_cost_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(status__in=SupplyCustodyStatus.values),
                name="ck_supply_custody_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="open", current_quantity__gt=0)
                    | Q(
                        status="closed",
                        current_quantity=0,
                        current_amount=0,
                    )
                ),
                name="ck_supply_custody_status_balance",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        parent_custody__isnull=True,
                        origin_issue_line__isnull=False,
                        origin_import_row__isnull=True,
                    )
                    | Q(
                        parent_custody__isnull=True,
                        origin_issue_line__isnull=True,
                        origin_import_row__isnull=False,
                    )
                    | Q(
                        parent_custody__isnull=False,
                        origin_issue_line__isnull=True,
                        origin_import_row__isnull=True,
                    )
                ),
                name="ck_supply_custody_source_shape",
            ),
            models.CheckConstraint(
                condition=~Q(id=F("parent_custody")),
                name="ck_supply_custody_parent_not_self",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "employee", "status"),
                name="supply_custody_employee_idx",
            ),
            models.Index(
                fields=("company", "department", "item", "status"),
                name="supply_custody_dept_item_idx",
            ),
        ]

    def clean(self):
        super().clean()
        from .domain import quantize_money, quantize_quantity, quantize_unit_cost

        if self.item_id:
            if self.item.company_id != self.company_id:
                raise ValidationError({"item": "保管物品必须属于同一公司。"})
            if self.item.item_type != SupplyItemType.DURABLE_QUANTITY:
                raise ValidationError({"item": "只有数量型低值耐用品可以建立保管。"})
        if self.origin_issue_line_id:
            line = self.origin_issue_line
            if line.company_id != self.company_id:
                raise ValidationError({"origin_issue_line": "来源领用明细必须属于同一公司。"})
            if line.item_id != self.item_id or line.document.document_type != SupplyDocumentType.ISSUE:
                raise ValidationError({"origin_issue_line": "来源必须是同一物品的领用单明细。"})
        if self.origin_import_row_id:
            row = self.origin_import_row
            if row.batch.company_id != self.company_id:
                raise ValidationError({"origin_import_row": "期初导入行必须属于同一公司。"})
            if row.batch.import_type != "opening_custody":
                raise ValidationError({"origin_import_row": "来源必须是耐用品期初保管导入行。"})
            if row.validation_status not in {"valid", "created"}:
                raise ValidationError({"origin_import_row": "期初导入行尚未通过校验。"})
        if self.parent_custody_id:
            parent = self.parent_custody
            if parent.pk == self.pk:
                raise ValidationError({"parent_custody": "父保管不能指向自身。"})
            if parent.company_id != self.company_id or parent.item_id != self.item_id:
                raise ValidationError({"parent_custody": "父保管必须属于同一公司和物品。"})
            if self.origin_issue_line_id or self.origin_import_row_id:
                raise ValidationError("转交子保管不得重复占用根来源。")
        else:
            if bool(self.origin_issue_line_id) == bool(self.origin_import_row_id):
                raise ValidationError("根保管必须且只能关联领用行或期初导入行之一。")
        if self.department_id and self.department.company_id != self.company_id:
            raise ValidationError({"department": "责任部门必须属于同一公司。"})
        if self.employee_id:
            if self.employee.company_id != self.company_id:
                raise ValidationError({"employee": "责任员工必须属于同一公司。"})
            if self.employee.department_id != self.department_id:
                raise ValidationError({"employee": "责任员工必须属于责任部门。"})
            if (
                self.employee.employment_status != "active"
                or not self.employee.is_active
                or not self.employee.department.is_active
            ):
                raise ValidationError({"employee": "责任员工必须是在职、启用且属于启用部门。"})
        self.current_quantity = quantize_quantity(self.current_quantity)
        self.current_amount = quantize_money(self.current_amount)
        self.unit_cost_snapshot = quantize_unit_cost(self.unit_cost_snapshot)
        if self.current_quantity < ZERO_QUANTITY or self.current_amount < ZERO_MONEY:
            raise ValidationError("保管数量和金额不得小于 0。")
        if self.status == SupplyCustodyStatus.OPEN and self.current_quantity <= ZERO_QUANTITY:
            raise ValidationError("在管保管记录的当前数量必须大于 0。")
        if self.status == SupplyCustodyStatus.CLOSED and (
            self.current_quantity != ZERO_QUANTITY or self.current_amount != ZERO_MONEY
        ):
            raise ValidationError("已结清保管记录的数量和金额必须同时为 0。")

    def save(self, *args, **kwargs):
        controlled = getattr(self, "_controlled_mutation", False)
        if not controlled:
            raise ValidationError("耐用品保管记录只能通过受控库存 Service 保存。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("耐用品保管记录不得通过普通操作删除。")

    def __str__(self):
        employee = self.employee or "部门保管"
        return f"{self.item} / {self.department} / {employee}: {self.current_quantity}"


class SupplyCustodyMovementQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _only_actor_null_update(
            self,
            kwargs,
            actor_fields={"created_by", "created_by_id"},
            message="保管流水只允许追加，不能更新。",
        )

    def delete(self):
        raise ValidationError("保管流水只允许追加，不能删除。")


class SupplyCustodyMovement(models.Model):
    Action = SupplyCustodyAction

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_custody_movements",
    )
    item = models.ForeignKey(
        SupplyItem,
        verbose_name="物品",
        on_delete=models.PROTECT,
        related_name="custody_movements",
    )
    from_custody = models.ForeignKey(
        SupplyCustody,
        verbose_name="转出保管记录",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="outgoing_movements",
    )
    to_custody = models.ForeignKey(
        SupplyCustody,
        verbose_name="转入保管记录",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="incoming_movements",
    )
    action = models.CharField(
        "动作", max_length=16, choices=SupplyCustodyAction.choices
    )
    quantity = models.DecimalField("数量", **QUANTITY)
    amount = models.DecimalField("金额", **MONEY)
    unit_cost = models.DecimalField("单位成本", **UNIT_COST)
    business_date = models.DateField("业务日期")
    source_document_line = models.ForeignKey(
        SupplyDocumentLine,
        verbose_name="来源单据明细",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="custody_movements",
    )
    reason = models.TextField("原因", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_supply_custody_movements",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    reverses_movement = models.OneToOneField(
        "self",
        verbose_name="被冲销保管流水",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal_movement",
    )
    idempotency_key = models.CharField(
        "动作幂等键", max_length=128, null=True, blank=True
    )

    objects = SupplyCustodyMovementQuerySet.as_manager()

    class Meta:
        verbose_name = "数量型低值耐用品保管流水"
        verbose_name_plural = "数量型低值耐用品保管流水"
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(action__in=SupplyCustodyAction.values),
                name="ck_supply_custody_movement_action",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="ck_supply_custody_movement_qty",
            ),
            models.CheckConstraint(
                condition=Q(amount__gte=0, unit_cost__gte=0),
                name="ck_supply_custody_movement_amount",
            ),
            models.CheckConstraint(
                condition=~Q(action=SupplyCustodyAction.CORRECTION) | ~Q(reason=""),
                name="ck_supply_custody_correction_reason",
            ),
            models.CheckConstraint(
                condition=(
                    Q(action__in=("issue", "opening"), from_custody__isnull=True, to_custody__isnull=False)
                    | Q(action__in=("return", "loss", "scrap"), from_custody__isnull=False, to_custody__isnull=True)
                    | (
                        Q(action="transfer", from_custody__isnull=False, to_custody__isnull=False)
                        & ~Q(from_custody=F("to_custody"))
                    )
                    | (
                        Q(action="correction")
                        & (
                            Q(from_custody__isnull=True, to_custody__isnull=False)
                            | Q(from_custody__isnull=False, to_custody__isnull=True)
                        )
                    )
                    | (
                        Q(action="reversal")
                        & (
                            Q(from_custody__isnull=True, to_custody__isnull=False)
                            | Q(from_custody__isnull=False, to_custody__isnull=True)
                            | (
                                Q(from_custody__isnull=False, to_custody__isnull=False)
                                & ~Q(from_custody=F("to_custody"))
                            )
                        )
                    )
                ),
                name="ck_supply_custody_movement_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(action="reversal", reverses_movement__isnull=False)
                    | (~Q(action="reversal") & Q(reverses_movement__isnull=True))
                ),
                name="ck_supply_custody_movement_reversal",
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                condition=Q(idempotency_key__isnull=False),
                name="uq_supply_custody_move_company_idem",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "item", "created_at"),
                name="supply_custody_move_item_idx",
            ),
        ]

    def clean(self):
        super().clean()
        from .domain import quantize_money, quantize_quantity, quantize_unit_cost

        if self.item_id:
            if self.item.company_id != self.company_id:
                raise ValidationError({"item": "保管流水物品必须属于同一公司。"})
            if self.item.item_type != SupplyItemType.DURABLE_QUANTITY:
                raise ValidationError({"item": "保管流水仅适用于数量型低值耐用品。"})
        for field_name in ("from_custody", "to_custody"):
            custody = getattr(self, field_name, None)
            if custody is not None and (
                custody.company_id != self.company_id or custody.item_id != self.item_id
            ):
                raise ValidationError({field_name: "保管流水引用的保管记录不属于同一公司或物品。"})
        if self.source_document_line_id:
            line = self.source_document_line
            if line.company_id != self.company_id or line.item_id != self.item_id:
                raise ValidationError({"source_document_line": "来源单据明细不属于同一公司或物品。"})
        if self.reverses_movement_id:
            original = self.reverses_movement
            if original.company_id != self.company_id or original.item_id != self.item_id:
                raise ValidationError({"reverses_movement": "被冲销保管流水不属于同一公司或物品。"})
            if self.action != SupplyCustodyAction.REVERSAL:
                raise ValidationError({"reverses_movement": "只有冲销动作可以关联原保管流水。"})
            if (
                self.from_custody_id != original.to_custody_id
                or self.to_custody_id != original.from_custody_id
                or self.quantity != original.quantity
                or self.amount != original.amount
                or self.unit_cost != original.unit_cost
            ):
                raise ValidationError("保管冲销流水必须精确反转原动作方向、数量和金额。")
        elif self.action == SupplyCustodyAction.REVERSAL:
            raise ValidationError({"reverses_movement": "冲销动作必须关联原保管流水。"})
        if self.action == SupplyCustodyAction.CORRECTION:
            if (self.from_custody_id is None) == (self.to_custody_id is None):
                raise ValidationError("盘点更正流水必须且只能有一个保管方向。")
            if self.source_document_line_id or self.reverses_movement_id:
                raise ValidationError("盘点更正不得伪造库存单据或冲销来源。")
            if not str(self.reason or "").strip():
                raise ValidationError({"reason": "盘点更正原因不能为空。"})
        self.idempotency_key = str(self.idempotency_key or "").strip() or None
        self.quantity = quantize_quantity(self.quantity)
        self.amount = quantize_money(self.amount)
        self.unit_cost = quantize_unit_cost(self.unit_cost)
        if self.quantity <= ZERO_QUANTITY:
            raise ValidationError({"quantity": "保管流水数量必须大于 0。"})
        if self.amount < ZERO_MONEY or self.unit_cost < ZERO_UNIT_COST:
            raise ValidationError("保管流水金额和单位成本不得小于 0。")

    def save(self, *args, **kwargs):
        if not self._state.adding or not getattr(self, "_controlled_insert", False):
            raise ValidationError("保管流水只允许由受控库存 Service 追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("保管流水只允许追加，不能删除。")

    def __str__(self):
        return f"{self.get_action_display()} / {self.item}: {self.quantity}"


class SupplyCountTaskQuerySet(models.QuerySet):
    _ACTOR_FIELDS = {
        "created_by",
        "created_by_id",
        "published_by",
        "published_by_id",
        "stopped_by",
        "stopped_by_id",
        "closed_by",
        "closed_by_id",
        "cancelled_by",
        "cancelled_by_id",
    }

    def update(self, **kwargs):
        return _only_actor_null_update(
            self,
            kwargs,
            actor_fields=self._ACTOR_FIELDS,
            message="盘点任务只能通过受控盘点 Service 修改。",
        )

    def delete(self):
        if self.exclude(status=SupplyCountStatus.DRAFT).exists():
            raise ValidationError("已发布盘点任务不得物理删除。")
        return models.QuerySet.delete(self)


class SupplyCountTask(models.Model):
    CountDomain = SupplyCountDomain
    Status = SupplyCountStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_count_tasks",
    )
    task_no = models.CharField("盘点任务编号", max_length=64)
    name = models.CharField("盘点任务名称", max_length=200)
    count_domain = models.CharField(
        "盘点域", max_length=32, choices=SupplyCountDomain.choices
    )
    warehouse = models.ForeignKey(
        SupplyWarehouse,
        verbose_name="盘点仓库",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="count_tasks",
    )
    department = models.ForeignKey(
        "masterdata.Department",
        verbose_name="盘点部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_count_tasks",
    )
    employee = models.ForeignKey(
        "masterdata.Employee",
        verbose_name="盘点员工",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supply_count_tasks",
    )
    planned_start = models.DateField("计划开始日期")
    planned_end = models.DateField("计划结束日期")
    snapshot_at = models.DateTimeField("快照时间", null=True, blank=True)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=SupplyCountStatus.choices,
        default=SupplyCountStatus.DRAFT,
    )
    idempotency_key = models.CharField("创建幂等键", max_length=128)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_supply_count_tasks",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="发布人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="published_supply_count_tasks",
    )
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    stopped_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="停止录入人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stopped_supply_count_tasks",
    )
    stopped_at = models.DateTimeField("停止录入时间", null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="关闭人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="closed_supply_count_tasks",
    )
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="取消人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cancelled_supply_count_tasks",
    )
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    cancellation_reason = models.TextField("取消原因", blank=True)
    remark = models.TextField("备注", blank=True)

    objects = SupplyCountTaskQuerySet.as_manager()

    class Meta:
        verbose_name = "低值物品盘点任务"
        verbose_name_plural = "低值物品盘点任务"
        ordering = ("-created_at", "-task_no")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "task_no"),
                name="uq_supply_count_task_company_no",
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_supply_count_task_company_idem",
            ),
            models.UniqueConstraint(
                fields=("company", "warehouse"),
                condition=Q(
                    count_domain="warehouse_stock",
                    status__in=("in_progress", "reconciliation"),
                ),
                name="uq_supply_count_active_warehouse",
            ),
            models.UniqueConstraint(
                fields=("company", "employee"),
                condition=Q(
                    count_domain="custody",
                    employee__isnull=False,
                    status__in=("in_progress", "reconciliation"),
                ),
                name="uq_supply_count_active_employee",
            ),
            models.CheckConstraint(
                condition=Q(count_domain__in=SupplyCountDomain.values),
                name="ck_supply_count_task_domain",
            ),
            models.CheckConstraint(
                condition=Q(status__in=SupplyCountStatus.values),
                name="ck_supply_count_task_status",
            ),
            models.CheckConstraint(
                condition=Q(planned_end__gte=F("planned_start")),
                name="ck_supply_count_task_dates",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        count_domain="warehouse_stock",
                        warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                    )
                    | Q(
                        count_domain="custody",
                        warehouse__isnull=True,
                        department__isnull=False,
                    )
                ),
                name="ck_supply_count_task_scope",
            ),
            models.CheckConstraint(
                condition=~Q(task_no="") & ~Q(name="") & ~Q(idempotency_key=""),
                name="ck_supply_count_task_required_text",
            ),
            models.CheckConstraint(
                condition=(
                    Q(cancelled_at__isnull=True, cancellation_reason="")
                    | (Q(cancelled_at__isnull=False) & ~Q(cancellation_reason=""))
                ),
                name="ck_supply_count_task_cancel_fields",
            ),
        ]
        indexes = [
            models.Index(
                fields=("company", "status", "count_domain"),
                name="supply_count_status_idx",
            ),
            models.Index(
                fields=("company", "department", "employee", "status"),
                name="supply_count_custody_scope_idx",
            ),
        ]

    def clean(self):
        super().clean()
        self.task_no = str(self.task_no or "").strip()
        self.name = str(self.name or "").strip()
        self.idempotency_key = str(self.idempotency_key or "").strip()
        self.cancellation_reason = str(self.cancellation_reason or "").strip()
        self.remark = str(self.remark or "").strip()
        if not self.task_no:
            raise ValidationError({"task_no": "盘点任务编号不能为空。"})
        if not self.name:
            raise ValidationError({"name": "盘点任务名称不能为空。"})
        if not self.idempotency_key:
            raise ValidationError({"idempotency_key": "盘点任务幂等键不能为空。"})
        if self.planned_start and self.planned_end and self.planned_end < self.planned_start:
            raise ValidationError({"planned_end": "计划结束日期不得早于计划开始日期。"})
        if self.count_domain == SupplyCountDomain.WAREHOUSE_STOCK:
            if self.warehouse_id is None or self.department_id or self.employee_id:
                raise ValidationError("仓库库存盘点必须且只能指定仓库。")
        elif self.count_domain == SupplyCountDomain.CUSTODY:
            if self.department_id is None or self.warehouse_id:
                raise ValidationError("保管盘点必须指定部门且不得指定仓库。")
        for field_name in ("warehouse", "department", "employee"):
            value = getattr(self, field_name, None)
            if value is not None and value.company_id != self.company_id:
                raise ValidationError({field_name: "盘点范围对象必须属于同一公司。"})
        if self.employee_id and self.employee.department_id != self.department_id:
            raise ValidationError({"employee": "盘点员工必须属于所选部门。"})
        if self.status == SupplyCountStatus.DRAFT:
            if any((self.snapshot_at, self.published_at, self.stopped_at, self.closed_at, self.cancelled_at)):
                raise ValidationError("草稿盘点不得包含发布、停止、关闭或取消时间。")
        elif self.status == SupplyCountStatus.IN_PROGRESS:
            if not self.snapshot_at or not self.published_at or self.stopped_at or self.closed_at or self.cancelled_at:
                raise ValidationError("进行中盘点的发布快照字段不完整。")
        elif self.status == SupplyCountStatus.RECONCILIATION:
            if not self.snapshot_at or not self.published_at or not self.stopped_at or self.closed_at or self.cancelled_at:
                raise ValidationError("差异处理中盘点的停止录入字段不完整。")
        elif self.status == SupplyCountStatus.CLOSED:
            if not self.snapshot_at or not self.published_at or not self.stopped_at or not self.closed_at or self.cancelled_at:
                raise ValidationError("已关闭盘点的状态时间字段不完整。")
        elif self.status == SupplyCountStatus.CANCELLED:
            if not self.cancelled_at or not self.cancellation_reason or self.closed_at:
                raise ValidationError("取消盘点必须保存取消时间和原因。")

    def save(self, *args, **kwargs):
        if self._state.adding:
            if not getattr(self, "_controlled_insert", False):
                raise ValidationError("盘点任务只能通过受控盘点 Service 创建。")
        elif not getattr(self, "_controlled_transition", False):
            raise ValidationError("盘点任务只能通过受控盘点 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != SupplyCountStatus.DRAFT:
            raise ValidationError("已发布盘点任务不得物理删除。")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.task_no} / {self.name}"


class SupplyCountLineQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("盘点行只能通过受控盘点 Service 修改。")

    def delete(self):
        raise ValidationError("盘点快照行不得物理删除。")


class SupplyCountLine(models.Model):
    ResolutionType = SupplyCountResolutionType

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="supply_count_lines",
    )
    count_task = models.ForeignKey(
        SupplyCountTask,
        verbose_name="盘点任务",
        on_delete=models.PROTECT,
        related_name="lines",
    )
    item = models.ForeignKey(
        SupplyItem,
        verbose_name="物品",
        on_delete=models.PROTECT,
        related_name="count_lines",
    )
    stock_balance = models.ForeignKey(
        SupplyStockBalance,
        verbose_name="发布时库存余额",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="count_lines",
    )
    custody = models.ForeignKey(
        SupplyCustody,
        verbose_name="发布时保管记录",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="count_lines",
    )
    item_code_snapshot = models.CharField("物品编码快照", max_length=100)
    item_name_snapshot = models.CharField("物品名称快照", max_length=200)
    department_snapshot = models.CharField("责任部门快照", max_length=200, blank=True)
    employee_snapshot = models.CharField("责任员工快照", max_length=200, blank=True)
    expected_quantity = models.DecimalField("应盘数量", **QUANTITY)
    expected_amount = models.DecimalField("应盘金额", **MONEY)
    expected_unit_cost = models.DecimalField("发布时单位成本", **UNIT_COST)
    counted_quantity = models.DecimalField(
        "实盘数量", null=True, blank=True, **QUANTITY
    )
    difference_quantity = models.DecimalField(
        "差异数量", null=True, blank=True, **QUANTITY
    )
    adjustment_unit_cost = models.DecimalField(
        "盘盈调整单位成本", null=True, blank=True, **UNIT_COST
    )
    zero_cost_reason = models.TextField("零成本原因", blank=True)
    resolution_type = models.CharField(
        "解决方式",
        max_length=16,
        choices=SupplyCountResolutionType.choices,
        null=True,
        blank=True,
    )
    adjustment_document_line = models.OneToOneField(
        SupplyDocumentLine,
        verbose_name="盘点调整单明细",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="source_count_line",
    )
    resolution_custody_movement = models.OneToOneField(
        SupplyCustodyMovement,
        verbose_name="保管差异解决流水",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="source_count_line",
    )
    remark = models.TextField("差异原因/备注", blank=True)
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="盘点录入人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recorded_supply_count_lines",
    )
    counted_at = models.DateTimeField("盘点录入时间", null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="差异处理人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_supply_count_lines",
    )
    resolved_at = models.DateTimeField("差异处理时间", null=True, blank=True)

    objects = SupplyCountLineQuerySet.as_manager()

    class Meta:
        verbose_name = "低值物品盘点行"
        verbose_name_plural = "低值物品盘点行"
        ordering = ("item_code_snapshot", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("count_task", "item"),
                condition=Q(custody__isnull=True),
                name="uq_supply_count_warehouse_item",
            ),
            models.UniqueConstraint(
                fields=("count_task", "custody"),
                condition=Q(custody__isnull=False),
                name="uq_supply_count_custody",
            ),
            models.CheckConstraint(
                condition=Q(stock_balance__isnull=True) | Q(custody__isnull=True),
                name="ck_supply_count_line_one_source",
            ),
            models.CheckConstraint(
                condition=Q(
                    expected_quantity__gte=0,
                    expected_amount__gte=0,
                    expected_unit_cost__gte=0,
                ),
                name="ck_supply_count_line_expected",
            ),
            models.CheckConstraint(
                condition=Q(counted_quantity__isnull=True) | Q(counted_quantity__gte=0),
                name="ck_supply_count_line_counted",
            ),
            models.CheckConstraint(
                condition=Q(adjustment_unit_cost__isnull=True)
                | Q(adjustment_unit_cost__gte=0),
                name="ck_supply_count_line_adjust_cost",
            ),
            models.CheckConstraint(
                condition=(
                    Q(counted_quantity__isnull=True, difference_quantity__isnull=True)
                    | Q(counted_quantity__isnull=False, difference_quantity__isnull=False)
                ),
                name="ck_supply_count_line_count_pair",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        adjustment_document_line__isnull=True,
                        resolution_custody_movement__isnull=True,
                        resolution_type__isnull=True,
                        resolved_by__isnull=True,
                        resolved_at__isnull=True,
                    )
                    | Q(
                        adjustment_document_line__isnull=False,
                        resolution_custody_movement__isnull=True,
                        resolution_type__isnull=True,
                        resolved_by__isnull=False,
                        resolved_at__isnull=False,
                    )
                    | Q(
                        adjustment_document_line__isnull=True,
                        resolution_custody_movement__isnull=False,
                        resolution_type__isnull=False,
                        resolved_by__isnull=False,
                        resolved_at__isnull=False,
                    )
                ),
                name="ck_supply_count_line_evidence",
            ),
            models.CheckConstraint(
                condition=~Q(item_code_snapshot="") & ~Q(item_name_snapshot=""),
                name="ck_supply_count_line_snapshots",
            ),
        ]

    def clean(self):
        super().clean()
        from .domain import (
            quantize_money,
            quantize_quantity,
            quantize_unit_cost,
        )

        if self.count_task_id and self.count_task.company_id != self.company_id:
            raise ValidationError({"count_task": "盘点任务必须属于同一公司。"})
        if self.item_id and self.item.company_id != self.company_id:
            raise ValidationError({"item": "盘点物品必须属于同一公司。"})
        self.expected_quantity = quantize_quantity(self.expected_quantity)
        self.expected_amount = quantize_money(self.expected_amount)
        self.expected_unit_cost = quantize_unit_cost(self.expected_unit_cost)
        if self.counted_quantity is not None:
            self.counted_quantity = quantize_quantity(self.counted_quantity)
            expected_difference = quantize_quantity(
                self.counted_quantity - self.expected_quantity
            )
            if self.difference_quantity is None:
                self.difference_quantity = expected_difference
            else:
                self.difference_quantity = quantize_quantity(self.difference_quantity)
                if self.difference_quantity != expected_difference:
                    raise ValidationError({"difference_quantity": "差异数量必须等于实盘数量减应盘数量。"})
            if self.counted_quantity < ZERO_QUANTITY:
                raise ValidationError({"counted_quantity": "实盘数量不得小于 0。"})
            if self.difference_quantity != ZERO_QUANTITY and not str(self.remark or "").strip():
                raise ValidationError({"remark": "存在盘点差异时必须填写原因。"})
        elif self.difference_quantity is not None:
            raise ValidationError({"difference_quantity": "尚未录入实盘数量时不得保存差异。"})
        if self.adjustment_unit_cost is not None:
            self.adjustment_unit_cost = quantize_unit_cost(self.adjustment_unit_cost)
            if self.adjustment_unit_cost < ZERO_UNIT_COST:
                raise ValidationError({"adjustment_unit_cost": "盘盈单位成本不得小于 0。"})
        if self.count_task_id and self.count_task.count_domain == SupplyCountDomain.WAREHOUSE_STOCK:
            if self.custody_id is not None:
                raise ValidationError({"custody": "仓库盘点行不得关联保管记录。"})
            if self.stock_balance_id and (
                self.stock_balance.company_id != self.company_id
                or self.stock_balance.item_id != self.item_id
                or self.stock_balance.warehouse_id != self.count_task.warehouse_id
            ):
                raise ValidationError({"stock_balance": "库存余额不属于盘点仓库、公司或物品。"})
        elif self.count_task_id:
            if self.stock_balance_id is not None or self.custody_id is None:
                raise ValidationError("保管盘点行必须且只能关联一条保管记录。")
            if (
                self.custody.company_id != self.company_id
                or self.custody.item_id != self.item_id
                or self.custody.department_id != self.count_task.department_id
                or (
                    self.count_task.employee_id is not None
                    and self.custody.employee_id != self.count_task.employee_id
                )
            ):
                raise ValidationError({"custody": "保管记录不属于盘点范围。"})
        if self.adjustment_document_line_id and self.resolution_custody_movement_id:
            raise ValidationError("一条盘点差异不能同时关联库存调整和保管流水。")
        if self.adjustment_document_line_id:
            line = self.adjustment_document_line
            if (
                line.company_id != self.company_id
                or line.item_id != self.item_id
                or line.document.source_count_task_id != self.count_task_id
            ):
                raise ValidationError({"adjustment_document_line": "调整单明细不属于本盘点任务和物品。"})
        if self.resolution_custody_movement_id:
            movement = self.resolution_custody_movement
            if movement.company_id != self.company_id or movement.item_id != self.item_id:
                raise ValidationError({"resolution_custody_movement": "解决流水不属于本盘点公司或物品。"})
            if self.custody_id not in {movement.from_custody_id, movement.to_custody_id}:
                raise ValidationError({"resolution_custody_movement": "解决流水未关联本盘点保管记录。"})

    def save(self, *args, **kwargs):
        if not self._state.adding or not getattr(self, "_controlled_insert", False):
            raise ValidationError("盘点行只能通过受控盘点 Service 创建。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("盘点快照行不得物理删除。")

    def __str__(self):
        return f"{self.count_task.task_no} / {self.item_code_snapshot}"


class EmployeeSupplyClearanceItemQuerySet(models.QuerySet):
    def update(self, **kwargs):
        return _only_actor_null_update(
            self,
            kwargs,
            actor_fields={"resolved_by", "resolved_by_id"},
            message="耐用品清退项目只能通过受控清退 Service 解决。",
        )

    def delete(self):
        raise ValidationError("耐用品清退项目不得物理删除。")


class EmployeeSupplyClearanceItem(models.Model):
    Resolution = EmployeeSupplyClearanceResolution

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clearance = models.ForeignKey(
        "offboarding.EmployeeAssetClearance",
        verbose_name="离职清退单",
        on_delete=models.PROTECT,
        related_name="supply_items",
    )
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="employee_supply_clearance_items",
    )
    custody = models.ForeignKey(
        SupplyCustody,
        verbose_name="耐用品保管记录",
        on_delete=models.PROTECT,
        related_name="clearance_items",
    )
    item_code_snapshot = models.CharField("物品编码快照", max_length=100)
    item_name_snapshot = models.CharField("物品名称快照", max_length=200)
    quantity_snapshot = models.DecimalField("数量快照", **QUANTITY)
    amount_snapshot = models.DecimalField("金额快照", **MONEY)
    department_snapshot = models.CharField("责任部门快照", max_length=200)
    employee_snapshot = models.CharField("责任员工快照", max_length=200)
    resolution = models.CharField(
        "解决方式",
        max_length=16,
        choices=EmployeeSupplyClearanceResolution.choices,
        default=EmployeeSupplyClearanceResolution.PENDING,
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="处理人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_employee_supply_clearance_items",
    )
    resolved_at = models.DateTimeField("处理时间", null=True, blank=True)
    custody_movement = models.ForeignKey(
        SupplyCustodyMovement,
        verbose_name="解决保管流水",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="clearance_items",
    )
    remark = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = EmployeeSupplyClearanceItemQuerySet.as_manager()

    class Meta:
        verbose_name = "员工离职耐用品清退项目"
        verbose_name_plural = "员工离职耐用品清退项目"
        ordering = ("clearance_id", "item_code_snapshot", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("clearance", "custody"),
                name="uq_employee_supply_clearance_custody",
            ),
            models.CheckConstraint(
                condition=Q(quantity_snapshot__gt=0, amount_snapshot__gte=0),
                name="ck_employee_supply_clearance_amounts",
            ),
            models.CheckConstraint(
                condition=Q(resolution__in=EmployeeSupplyClearanceResolution.values),
                name="ck_employee_supply_clearance_resolution",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        resolution="pending",
                        resolved_by__isnull=True,
                        resolved_at__isnull=True,
                        custody_movement__isnull=True,
                    )
                    | Q(
                        resolution__in=("returned", "transferred", "lost", "scrapped"),
                        resolved_by__isnull=False,
                        resolved_at__isnull=False,
                        custody_movement__isnull=False,
                    )
                ),
                name="ck_employee_supply_clearance_evidence",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(item_code_snapshot="")
                    & ~Q(item_name_snapshot="")
                    & ~Q(department_snapshot="")
                    & ~Q(employee_snapshot="")
                ),
                name="ck_employee_supply_clearance_snapshots",
            ),
        ]

    def clean(self):
        super().clean()
        from .domain import quantize_money, quantize_quantity

        self.quantity_snapshot = quantize_quantity(self.quantity_snapshot)
        self.amount_snapshot = quantize_money(self.amount_snapshot)
        if self.clearance_id and self.clearance.company_id != self.company_id:
            raise ValidationError({"clearance": "清退单必须属于同一公司。"})
        if self.custody_id:
            if self.custody.company_id != self.company_id:
                raise ValidationError({"custody": "保管记录必须属于同一公司。"})
            if self.custody.item.item_type != SupplyItemType.DURABLE_QUANTITY:
                raise ValidationError({"custody": "清退项目只允许数量型低值耐用品保管。"})
        if self.quantity_snapshot <= ZERO_QUANTITY or self.amount_snapshot < ZERO_MONEY:
            raise ValidationError("耐用品清退数量必须大于 0，金额不得小于 0。")
        if self.resolution == EmployeeSupplyClearanceResolution.PENDING:
            if self.resolved_by_id or self.resolved_at or self.custody_movement_id:
                raise ValidationError("待处理耐用品清退项不得伪造解决证据。")
        else:
            if not self.resolved_by_id or not self.resolved_at or not self.custody_movement_id:
                raise ValidationError("已解决耐用品清退项必须关联处理人、时间和真实保管流水。")
            movement = self.custody_movement
            if movement.company_id != self.company_id or movement.from_custody_id != self.custody_id:
                raise ValidationError({"custody_movement": "解决流水必须从本清退保管记录转出。"})
            expected = {
                SupplyCustodyAction.RETURN: EmployeeSupplyClearanceResolution.RETURNED,
                SupplyCustodyAction.TRANSFER: EmployeeSupplyClearanceResolution.TRANSFERRED,
                SupplyCustodyAction.LOSS: EmployeeSupplyClearanceResolution.LOST,
                SupplyCustodyAction.SCRAP: EmployeeSupplyClearanceResolution.SCRAPPED,
            }.get(movement.action)
            if expected != self.resolution:
                raise ValidationError({"custody_movement": "保管流水动作与清退解决方式不一致。"})

    def save(self, *args, **kwargs):
        if not self._state.adding or not getattr(self, "_controlled_insert", False):
            raise ValidationError("耐用品清退项目只能通过受控清退 Service 创建。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("耐用品清退项目不得物理删除。")

    def __str__(self):
        return f"{self.clearance_id} / {self.item_code_snapshot}"
