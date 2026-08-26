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
        "序号类型", max_length=32, choices=SupplyDocumentType.choices
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
                condition=Q(sequence_type__in=SupplyDocumentType.values),
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
                    ~Q(document_type__in=("opening", "receipt"))
                    | Q(
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                    )
                ),
                name="ck_supply_document_receipt_shape",
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
        for field_name in ("source_warehouse", "target_warehouse", "department", "employee"):
            value = getattr(self, field_name, None)
            if value is not None and value.company_id != self.company_id:
                raise ValidationError({field_name: "所选对象必须属于同一公司。"})
        if self.employee_id and self.department_id:
            if self.employee.department_id != self.department_id:
                raise ValidationError({"employee": "所选员工不属于目标部门。"})

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
