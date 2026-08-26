from __future__ import annotations

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError

from apps.masterdata.models import Employee, Location
from .models import SupplyCategory, SupplyItem, SupplyItemType, SupplyWarehouse
from .permissions import (
    can_manage_supply_item,
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
