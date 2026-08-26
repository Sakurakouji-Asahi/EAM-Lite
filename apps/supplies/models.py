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
ZERO_QUANTITY = Decimal("0.0000")


class SupplyItemType(models.TextChoices):
    CONSUMABLE = "consumable", "低值易耗品"
    DURABLE_QUANTITY = "durable_quantity", "数量型低值耐用品"


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
