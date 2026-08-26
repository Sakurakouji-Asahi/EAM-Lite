from __future__ import annotations

import uuid
from decimal import Decimal

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import formset_factory
from django.db.models import Q
from django.utils import timezone

from apps.masterdata.models import Employee, Location
from .domain import quantize_quantity, quantize_unit_cost, validate_zero_cost_reason
from .models import (
    SupplyCategory,
    SupplyDocumentType,
    SupplyItem,
    SupplyItemType,
    SupplyWarehouse,
)
from .permissions import (
    can_manage_supply_item,
    require_create_supply_document,
    require_manage_supply_category,
    require_manage_supply_item,
    require_manage_supply_warehouse,
)


def _bootstrap_widgets(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


class SupplyFormMixin:
    permission = None

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None or company is None:
            raise PermissionDenied("表单必须绑定当前用户和公司。")
        self.actor = actor
        self.company = company
        super().__init__(*args, **kwargs)
        if hasattr(self.instance, "company_id") and self.instance.company_id is None:
            self.instance.company = company
        _bootstrap_widgets(self)


class SupplyCategoryForm(SupplyFormMixin, forms.ModelForm):
    class Meta:
        model = SupplyCategory
        fields = ("code", "name", "parent", "default_item_type", "remark")
        widgets = {"remark": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "code": "同一公司内按 NFKC、去除首尾空格并忽略大小写判重。",
            "default_item_type": "仅作为新增物品时的提示，不自动改变管理模式。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        require_manage_supply_category(self.actor)
        queryset = SupplyCategory.objects.filter(
            company=self.company, is_active=True
        ).order_by("normalized_code")
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = queryset

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        if parent and parent.company_id != self.company.pk:
            raise ValidationError("上级分类不属于当前公司。")
        return parent


class SupplyWarehouseForm(SupplyFormMixin, forms.ModelForm):
    class Meta:
        model = SupplyWarehouse
        fields = ("code", "name", "location", "manager_employee", "remark")
        widgets = {"remark": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "location": "可关联现有启用位置；不复制位置层级。",
            "manager_employee": "只允许当前公司在职、启用且属于启用部门的员工。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        require_manage_supply_warehouse(self.actor)
        self.fields["location"].queryset = Location.objects.filter(
            company=self.company, is_active=True
        ).order_by("level", "normalized_code")
        self.fields["manager_employee"].queryset = Employee.objects.filter(
            company=self.company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        ).select_related("department").order_by("normalized_employee_no")

    def clean_location(self):
        location = self.cleaned_data.get("location")
        if location and location.company_id != self.company.pk:
            raise ValidationError("关联位置不属于当前公司。")
        return location

    def clean_manager_employee(self):
        employee = self.cleaned_data.get("manager_employee")
        if employee is None:
            return None
        if employee.company_id != self.company.pk:
            raise ValidationError("仓库负责人不属于当前公司。")
        if employee.employment_status != "active" or not employee.is_active:
            raise ValidationError("仓库负责人必须是在职且启用的员工。")
        if not employee.department.is_active:
            raise ValidationError("仓库负责人必须属于启用部门。")
        return employee


class SupplyItemForm(SupplyFormMixin, forms.ModelForm):
    class Meta:
        model = SupplyItem
        fields = (
            "item_code",
            "name",
            "category",
            "item_type",
            "unit",
            "specification",
            "model",
            "brand",
            "minimum_stock_quantity",
            "default_warehouse",
            "remark",
        )
        widgets = {
            "minimum_stock_quantity": forms.NumberInput(
                attrs={"step": "0.0001", "min": "0"}
            ),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "item_code": "同一公司内按规范化编码唯一。",
            "item_type": "需要逐件二维码、序列号或单件责任人的物品请使用现有资产模块。",
            "minimum_stock_quantity": "最多 4 位小数，不得为负数。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_type = self.instance.item_type if self.instance.pk else None
        if current_type:
            require_manage_supply_item(self.actor, current_type)
        else:
            require_manage_supply_item(
                self.actor, SupplyItemType.DURABLE_QUANTITY
            )
        self.fields["category"].queryset = SupplyCategory.objects.filter(
            company=self.company, is_active=True
        ).order_by("normalized_code")
        self.fields["default_warehouse"].queryset = SupplyWarehouse.objects.filter(
            company=self.company, is_active=True
        ).order_by("normalized_code")
        if not can_manage_supply_item(
            self.actor, SupplyItemType.CONSUMABLE
        ):
            self.fields["item_type"].choices = (
                (
                    SupplyItemType.DURABLE_QUANTITY,
                    SupplyItemType.DURABLE_QUANTITY.label,
                ),
            )

    def clean_category(self):
        category = self.cleaned_data.get("category")
        if category and category.company_id != self.company.pk:
            raise ValidationError("分类不属于当前公司。")
        return category

    def clean_default_warehouse(self):
        warehouse = self.cleaned_data.get("default_warehouse")
        if warehouse and warehouse.company_id != self.company.pk:
            raise ValidationError("默认仓库不属于当前公司。")
        return warehouse

    def clean_item_type(self):
        item_type = self.cleaned_data.get("item_type")
        require_manage_supply_item(self.actor, item_type)
        return item_type


class SupplyDeactivateForm(forms.Form):
    reason = forms.CharField(
        label="停用原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )


class SupplyDocumentForm(forms.Form):
    business_date = forms.DateField(
        label="业务日期",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    target_warehouse = forms.ModelChoiceField(
        label="目标仓库",
        queryset=SupplyWarehouse.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    external_reference = forms.CharField(
        label="外部参考号",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    counterparty_name = forms.CharField(
        label="来源单位",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    remark = forms.CharField(
        label="单据备注",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    def __init__(
        self,
        *args,
        actor=None,
        company=None,
        document_type=None,
        instance=None,
        **kwargs,
    ):
        if actor is None or company is None:
            raise PermissionDenied("库存单据表单必须绑定当前用户和公司。")
        require_create_supply_document(actor)
        if document_type not in {
            SupplyDocumentType.OPENING,
            SupplyDocumentType.RECEIPT,
        }:
            raise ValidationError("Sprint 14 只允许期初入库和日常入库表单。")
        self.actor = actor
        self.company = company
        self.document_type = document_type
        self.instance = instance
        initial = dict(kwargs.pop("initial", {}) or {})
        if instance is not None:
            initial.update(
                {
                    "business_date": instance.business_date,
                    "target_warehouse": instance.target_warehouse_id,
                    "external_reference": instance.external_reference,
                    "counterparty_name": instance.counterparty_name,
                    "remark": instance.remark,
                    "idempotency_key": instance.idempotency_key,
                }
            )
        else:
            initial.setdefault("business_date", timezone.localdate())
            initial.setdefault("idempotency_key", str(uuid.uuid4()))
        super().__init__(*args, initial=initial, **kwargs)
        warehouses = SupplyWarehouse.objects.filter(company=company, is_active=True)
        if instance is not None and instance.target_warehouse_id:
            warehouses = SupplyWarehouse.objects.filter(company=company).filter(
                Q(is_active=True) | Q(pk=instance.target_warehouse_id)
            )
        self.fields["target_warehouse"].queryset = warehouses.order_by(
            "normalized_code"
        )

    def clean_target_warehouse(self):
        warehouse = self.cleaned_data["target_warehouse"]
        if warehouse.company_id != self.company.pk:
            raise ValidationError("目标仓库不属于当前公司。")
        if not warehouse.is_active:
            raise ValidationError("目标仓库已停用；该草稿只能取消，不能继续编辑或过账。")
        return warehouse

    def clean_idempotency_key(self):
        value = str(self.cleaned_data.get("idempotency_key") or "").strip()
        if not value:
            raise ValidationError("创建幂等键无效，请刷新页面重试。")
        return value


class SupplyDocumentLineEntryForm(forms.Form):
    item = forms.ModelChoiceField(
        label="物品",
        queryset=SupplyItem.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    quantity = forms.DecimalField(
        label="数量",
        max_digits=18,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        widget=forms.NumberInput(
            attrs={"step": "0.0001", "min": "0.0001", "class": "form-control"}
        ),
    )
    entered_unit_cost = forms.DecimalField(
        label="单位成本",
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0"),
        widget=forms.NumberInput(
            attrs={"step": "0.000001", "min": "0", "class": "form-control"}
        ),
    )
    line_remark = forms.CharField(
        label="明细备注 / 0 成本原因",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None or company is None:
            raise PermissionDenied("库存单据明细表单必须绑定当前用户和公司。")
        require_create_supply_document(actor)
        self.company = company
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = SupplyItem.objects.filter(
            company=company, is_active=True
        ).select_related("category").order_by("normalized_item_code")

    def clean(self):
        cleaned = super().clean()
        item = cleaned.get("item")
        if item is not None and item.company_id != self.company.pk:
            self.add_error("item", "物品不属于当前公司。")
        if item is not None and not item.is_active:
            self.add_error("item", "物品已停用，不能用于新增业务单据。")
        quantity = cleaned.get("quantity")
        if quantity is not None:
            cleaned["quantity"] = quantize_quantity(quantity)
        unit_cost = cleaned.get("entered_unit_cost")
        if unit_cost is not None:
            cleaned["entered_unit_cost"] = quantize_unit_cost(unit_cost)
            try:
                cleaned["line_remark"] = validate_zero_cost_reason(
                    cleaned["entered_unit_cost"], cleaned.get("line_remark")
                )
            except ValidationError as exc:
                self.add_error("line_remark", exc)
        return cleaned


SupplyDocumentLineFormSet = formset_factory(
    SupplyDocumentLineEntryForm,
    extra=1,
    can_delete=True,
    max_num=100,
    validate_max=True,
)


class SupplyDocumentCancelForm(forms.Form):
    reason = forms.CharField(
        label="取消原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )


class SupplyDocumentPostForm(forms.Form):
    confirm = forms.BooleanField(
        label="我已核对仓库、物品、数量和成本；确认立即过账且历史不可编辑。",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *args, document=None, **kwargs):
        initial = dict(kwargs.pop("initial", {}) or {})
        if document is not None:
            initial.setdefault("idempotency_key", document.idempotency_key)
        super().__init__(*args, initial=initial, **kwargs)
