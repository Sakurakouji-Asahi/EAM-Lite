from __future__ import annotations

import uuid
from decimal import Decimal

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import formset_factory
from django.db.models import Q
from django.utils import timezone

from apps.masterdata.models import Department, Employee, Location
from apps.masterdata.permissions import resolve_department_ids, role_names_for
from .domain import quantize_quantity, quantize_unit_cost, validate_zero_cost_reason
from .models import (
    SupplyCategory,
    SupplyCountDomain,
    SupplyCountResolutionType,
    SupplyCustody,
    SupplyCustodyAction,
    SupplyDocumentLine,
    SupplyDocumentStatus,
    SupplyDocumentType,
    SupplyItem,
    SupplyItemType,
    SupplyWarehouse,
)
from .permissions import (
    can_create_supply_count_task,
    can_view_supply_cost,
    can_manage_supply_item,
    require_create_supply_document,
    require_execute_supply_count_task,
    require_manage_supply_category,
    require_manage_supply_custody,
    require_manage_supply_item,
    require_manage_supply_warehouse,
    require_reverse_supply_document,
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
        help_text="默认为今天；补录时请选择实际发生日期。",
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={"type": "date", "class": "form-control"},
        ),
    )
    source_warehouse = forms.ModelChoiceField(
        label="来源仓库",
        help_text="领用或调拨实际出库的仓库。",
        queryset=SupplyWarehouse.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    target_warehouse = forms.ModelChoiceField(
        label="目标仓库",
        help_text="入库、退回或调拨实际进入的仓库。",
        queryset=SupplyWarehouse.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    department = forms.ModelChoiceField(
        label="领用部门",
        help_text="耐用品领用后将以此部门建立保管责任。",
        queryset=Department.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    employee = forms.ModelChoiceField(
        label="领用员工",
        help_text="可留空表示部门领用；选择员工时必须属于领用部门。",
        queryset=Employee.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    external_reference = forms.CharField(
        label="外部参考号",
        help_text="可填写送货单号、采购单号等外部凭据编号。",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    counterparty_name = forms.CharField(
        label="来源单位",
        help_text="可填写供应商或交付单位名称，无需另建供应商档案。",
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
            SupplyDocumentType.ISSUE,
            SupplyDocumentType.RETURN,
            SupplyDocumentType.TRANSFER,
        }:
            raise ValidationError("当前 Sprint 不允许该单据类型表单。")
        self.actor = actor
        self.company = company
        self.document_type = document_type
        self.instance = instance
        initial = dict(kwargs.pop("initial", {}) or {})
        if instance is not None:
            initial.update(
                {
                    "business_date": instance.business_date,
                    "source_warehouse": instance.source_warehouse_id,
                    "target_warehouse": instance.target_warehouse_id,
                    "department": instance.department_id,
                    "employee": instance.employee_id,
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
        if instance is not None and (
            instance.target_warehouse_id or instance.source_warehouse_id
        ):
            warehouses = SupplyWarehouse.objects.filter(company=company).filter(
                Q(is_active=True)
                | Q(pk=instance.target_warehouse_id)
                | Q(pk=instance.source_warehouse_id)
            )
        self.fields["source_warehouse"].queryset = warehouses.order_by(
            "normalized_code"
        )
        self.fields["target_warehouse"].queryset = warehouses.order_by(
            "normalized_code"
        )
        self.fields["department"].queryset = Department.objects.filter(
            company=company, is_active=True
        ).order_by("normalized_code")
        self.fields["employee"].queryset = Employee.objects.filter(
            company=company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        ).select_related("department").order_by("normalized_employee_no")

        for name in ("source_warehouse", "target_warehouse"):
            self.fields[name].empty_label = "请选择仓库"
        self.fields["department"].empty_label = "请选择领用部门"
        self.fields["employee"].empty_label = "部门领用（不指定员工）"

        if instance is None and not self.is_bound and warehouses.count() == 1:
            only_warehouse = warehouses.first()
            if document_type in {
                SupplyDocumentType.OPENING,
                SupplyDocumentType.RECEIPT,
                SupplyDocumentType.RETURN,
            }:
                self.initial.setdefault("target_warehouse", only_warehouse.pk)
            elif document_type == SupplyDocumentType.ISSUE:
                self.initial.setdefault("source_warehouse", only_warehouse.pk)

        if document_type in {
            SupplyDocumentType.OPENING,
            SupplyDocumentType.RECEIPT,
        }:
            self.fields["target_warehouse"].required = True
            for name in ("source_warehouse", "department", "employee"):
                self.fields.pop(name)
        elif document_type == SupplyDocumentType.ISSUE:
            self.fields["source_warehouse"].required = True
            self.fields["department"].required = True
            for name in ("target_warehouse", "external_reference", "counterparty_name"):
                self.fields.pop(name)
        elif document_type == SupplyDocumentType.TRANSFER:
            self.fields["source_warehouse"].required = True
            self.fields["target_warehouse"].required = True
            for name in ("department", "employee", "external_reference", "counterparty_name"):
                self.fields.pop(name)
        else:
            self.fields["target_warehouse"].required = True
            for name in ("source_warehouse", "external_reference", "counterparty_name"):
                self.fields.pop(name)
            self.fields["department"].disabled = True
            self.fields["employee"].disabled = True

    def _clean_warehouse(self, field_name):
        warehouse = self.cleaned_data.get(field_name)
        if warehouse is None:
            return None
        if warehouse.company_id != self.company.pk:
            raise ValidationError("仓库不属于当前公司。")
        if not warehouse.is_active:
            raise ValidationError("仓库已停用；该草稿只能取消，不能继续编辑或过账。")
        return warehouse

    def clean_source_warehouse(self):
        return self._clean_warehouse("source_warehouse")

    def clean_target_warehouse(self):
        return self._clean_warehouse("target_warehouse")

    def clean_department(self):
        department = self.cleaned_data.get("department")
        if department is not None and (
            department.company_id != self.company.pk or not department.is_active
        ):
            raise ValidationError("领用部门不属于当前公司或已经停用。")
        return department

    def clean_employee(self):
        employee = self.cleaned_data.get("employee")
        if employee is not None and (
            employee.company_id != self.company.pk
            or employee.employment_status != "active"
            or not employee.is_active
            or not employee.department.is_active
        ):
            raise ValidationError("领用员工必须是当前公司在职、启用员工。")
        return employee

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("source_warehouse")
        target = cleaned.get("target_warehouse")
        department = cleaned.get("department")
        employee = cleaned.get("employee")
        if self.document_type == SupplyDocumentType.ISSUE:
            if employee is not None and (
                department is None or employee.department_id != department.pk
            ):
                self.add_error("employee", "领用员工必须属于所选领用部门。")
        if self.document_type == SupplyDocumentType.TRANSFER and (
            source is not None and target is not None and source.pk == target.pk
        ):
            self.add_error("target_warehouse", "来源仓库和目标仓库不能相同。")
        return cleaned

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
        help_text="按物品档案中的计量单位填写。",
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
        required=False,
        widget=forms.NumberInput(
            attrs={"step": "0.000001", "min": "0", "class": "form-control"}
        ),
    )
    line_remark = forms.CharField(
        label="明细备注 / 0 成本原因",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def __init__(
        self, *args, actor=None, company=None, document_type=None, **kwargs
    ):
        if actor is None or company is None:
            raise PermissionDenied("库存单据明细表单必须绑定当前用户和公司。")
        require_create_supply_document(actor)
        self.company = company
        self.document_type = document_type
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = SupplyItem.objects.filter(
            company=company, is_active=True
        ).select_related("category").order_by("normalized_item_code")
        if document_type in {
            SupplyDocumentType.OPENING,
            SupplyDocumentType.RECEIPT,
        }:
            self.fields["entered_unit_cost"].required = True
            self.fields["line_remark"].label = "明细备注（0 成本时必填原因）"
        else:
            self.fields.pop("entered_unit_cost")
            self.fields["line_remark"].label = "明细备注"

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
        if "entered_unit_cost" in self.fields and unit_cost is not None:
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


class SupplyConsumableReturnForm(forms.Form):
    target_warehouse = forms.ModelChoiceField(
        label="退回仓库",
        queryset=SupplyWarehouse.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    quantity = forms.DecimalField(
        label="退回数量",
        max_digits=18,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        widget=forms.NumberInput(
            attrs={"step": "0.0001", "min": "0.0001", "class": "form-control"}
        ),
    )
    reason = forms.CharField(
        label="退回原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )
    business_date = forms.DateField(
        label="业务日期",
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={"type": "date", "class": "form-control"},
        ),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    def __init__(
        self,
        *args,
        actor=None,
        company=None,
        source_issue_line=None,
        **kwargs,
    ):
        if actor is None or company is None or source_issue_line is None:
            raise PermissionDenied("退回表单必须绑定当前用户、公司和原领用明细。")
        require_create_supply_document(actor)
        self.company = company
        self.source_issue_line = source_issue_line
        initial = dict(kwargs.pop("initial", {}) or {})
        initial.setdefault("business_date", timezone.localdate())
        initial.setdefault("idempotency_key", str(uuid.uuid4()))
        super().__init__(*args, initial=initial, **kwargs)
        self.fields["target_warehouse"].queryset = SupplyWarehouse.objects.filter(
            company=company, is_active=True
        ).order_by("normalized_code")

    def clean_target_warehouse(self):
        warehouse = self.cleaned_data["target_warehouse"]
        if warehouse.company_id != self.company.pk or not warehouse.is_active:
            raise ValidationError("退回仓库不属于当前公司或已经停用。")
        return warehouse

    def clean_quantity(self):
        return quantize_quantity(self.cleaned_data["quantity"])

    def clean_idempotency_key(self):
        value = str(self.cleaned_data.get("idempotency_key") or "").strip()
        if not value:
            raise ValidationError("创建幂等键无效，请刷新页面重试。")
        return value

    def clean(self):
        cleaned = super().clean()
        source = SupplyDocumentLine.objects.select_related("document", "item").filter(
            pk=self.source_issue_line.pk,
            company=self.company,
            document__document_type=SupplyDocumentType.ISSUE,
            document__status=SupplyDocumentStatus.POSTED,
            item__item_type=SupplyItemType.CONSUMABLE,
        ).first()
        if source is None:
            raise ValidationError("原领用明细已失效，或不是可退回的低值易耗品。")
        return cleaned


class _CustodyActionBaseForm(forms.Form):
    quantity = forms.DecimalField(
        label="处理数量",
        max_digits=18,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        widget=forms.NumberInput(
            attrs={"step": "0.0001", "min": "0.0001", "class": "form-control"}
        ),
    )
    business_date = forms.DateField(
        label="业务日期",
        widget=forms.DateInput(
            format="%Y-%m-%d",
            attrs={"type": "date", "class": "form-control"},
        ),
    )
    reason = forms.CharField(
        label="原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    action = None
    prefill_full_quantity = True

    def __init__(self, *args, actor=None, company=None, custody=None, **kwargs):
        if actor is None or company is None or custody is None:
            raise PermissionDenied("保管动作表单必须绑定当前用户、公司和来源保管。")
        self.actor = actor
        self.company = company
        self.custody = custody
        require_manage_supply_custody(actor, custody, action=self.action)
        initial = dict(kwargs.pop("initial", {}) or {})
        initial.setdefault("business_date", timezone.localdate())
        if self.prefill_full_quantity:
            initial.setdefault("quantity", custody.current_quantity)
        initial.setdefault("idempotency_key", str(uuid.uuid4()))
        super().__init__(*args, initial=initial, **kwargs)
        self.fields["quantity"].help_text = (
            f"当前最多可处理 {custody.current_quantity} {custody.item.unit}。"
        )

    def clean_quantity(self):
        quantity = quantize_quantity(self.cleaned_data["quantity"])
        if quantity > self.custody.current_quantity:
            raise ValidationError(
                f"处理数量超过当前保管数量，当前最多可处理 {self.custody.current_quantity}。"
            )
        return quantity

    def clean_reason(self):
        value = str(self.cleaned_data.get("reason") or "").strip()
        if not value:
            raise ValidationError("原因不能为空。")
        return value

    def clean_idempotency_key(self):
        value = str(self.cleaned_data.get("idempotency_key") or "").strip()
        if not value:
            raise ValidationError("动作幂等键无效，请刷新页面重试。")
        return value


class SupplyDurableReturnForm(_CustodyActionBaseForm):
    action = "return_draft"
    target_warehouse = forms.ModelChoiceField(
        label="归还仓库",
        queryset=SupplyWarehouse.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_warehouse"].queryset = SupplyWarehouse.objects.filter(
            company=self.company, is_active=True
        ).order_by("normalized_code")


class SupplyCustodyTransferForm(_CustodyActionBaseForm):
    action = SupplyCustodyAction.TRANSFER
    target_department = forms.ModelChoiceField(
        label="目标责任部门",
        queryset=Department.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    target_employee = forms.ModelChoiceField(
        label="目标责任员工",
        queryset=Employee.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        departments = Department.objects.filter(
            company=self.company, is_active=True
        )
        roles = role_names_for(self.actor)
        department_ids = None
        if "department_manager" in roles and not roles.intersection(
            {"system_admin", "finance", "warehouse", "equipment"}
        ):
            department_ids = resolve_department_ids(self.actor, self.company)
            departments = departments.filter(
                pk__in=department_ids
            )
        self.fields["target_department"].queryset = departments.order_by(
            "normalized_code"
        )
        employees = Employee.objects.filter(
            company=self.company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )
        if department_ids is not None:
            employees = employees.filter(department_id__in=department_ids)
        self.fields["target_employee"].queryset = employees.select_related(
            "department"
        ).order_by("normalized_employee_no")

    def clean(self):
        cleaned = super().clean()
        department = cleaned.get("target_department")
        employee = cleaned.get("target_employee")
        if employee is not None and (
            department is None or employee.department_id != department.pk
        ):
            self.add_error("target_employee", "目标员工必须属于目标责任部门。")
        if department is not None:
            try:
                require_manage_supply_custody(
                    self.actor,
                    self.custody,
                    action="transfer",
                    target_department=department,
                )
            except PermissionDenied as exc:
                self.add_error("target_department", exc)
        return cleaned


class SupplyCustodyWriteOffForm(_CustodyActionBaseForm):
    prefill_full_quantity = False
    confirm = forms.BooleanField(
        label="我已核对物品、数量和原因，确认本次操作会减少在管数量且不能在本页撤销。",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, action=None, **kwargs):
        if action not in {SupplyCustodyAction.LOSS, SupplyCustodyAction.SCRAP}:
            raise ValidationError("保管核销表单只支持报损或报废。")
        self.action = action
        super().__init__(*args, **kwargs)


class SupplyDocumentReverseForm(forms.Form):
    reason = forms.CharField(
        label="冲销原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        label="我已核对原单及影响，确认生成并立即过账完整冲销。",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, actor=None, document=None, **kwargs):
        if actor is None:
            raise PermissionDenied("冲销表单必须绑定当前用户。")
        require_reverse_supply_document(actor, document=document)
        initial = dict(kwargs.pop("initial", {}) or {})
        initial.setdefault("idempotency_key", str(uuid.uuid4()))
        super().__init__(*args, initial=initial, **kwargs)

    def clean_idempotency_key(self):
        value = str(self.cleaned_data.get("idempotency_key") or "").strip()
        if not value:
            raise ValidationError("冲销幂等键无效，请刷新页面重试。")
        return value


class SupplyDocumentCancelForm(forms.Form):
    reason = forms.CharField(
        label="取消原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )


class SupplyDocumentPostForm(forms.Form):
    confirm = forms.BooleanField(
        label="我已核对仓库、物品和数量；确认立即过账且历史不可编辑。",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *args, document=None, **kwargs):
        initial = dict(kwargs.pop("initial", {}) or {})
        if document is not None:
            initial.setdefault("idempotency_key", document.idempotency_key)
        super().__init__(*args, initial=initial, **kwargs)


class SupplyCountTaskForm(forms.Form):
    name = forms.CharField(label="盘点任务名称", max_length=200)
    count_domain = forms.ChoiceField(
        label="盘点类型", choices=SupplyCountDomain.choices,
        help_text="仓库库存盘点核对在库数量；耐用品保管盘点核对部门或员工名下数量。",
    )
    warehouse = forms.ModelChoiceField(
        label="盘点仓库", queryset=SupplyWarehouse.objects.none(), required=False
    )
    department = forms.ModelChoiceField(
        label="盘点部门", queryset=Department.objects.none(), required=False
    )
    employee = forms.ModelChoiceField(
        label="盘点员工（可选）", queryset=Employee.objects.none(), required=False
    )
    planned_start = forms.DateField(
        label="计划开始日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    planned_end = forms.DateField(
        label="计划结束日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    remark = forms.CharField(
        label="备注", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None or company is None:
            raise PermissionDenied("盘点任务表单必须绑定当前用户和公司。")
        self.actor = actor
        self.company = company
        initial = dict(kwargs.pop("initial", {}) or {})
        initial.setdefault("planned_start", timezone.localdate())
        initial.setdefault("planned_end", timezone.localdate())
        initial.setdefault("idempotency_key", str(uuid.uuid4()))
        super().__init__(*args, initial=initial, **kwargs)
        allowed_domains = [
            choice
            for choice in SupplyCountDomain.choices
            if can_create_supply_count_task(
                actor,
                company=company,
                count_domain=choice[0],
                department=(
                    Department.objects.filter(
                        company=company,
                        pk__in=resolve_department_ids(actor, company),
                    ).first()
                    if choice[0] == SupplyCountDomain.CUSTODY
                    and "department_manager" in role_names_for(actor)
                    else None
                ),
            )
        ]
        self.fields["count_domain"].choices = allowed_domains
        self.fields["warehouse"].queryset = SupplyWarehouse.objects.filter(
            company=company, is_active=True
        ).order_by("normalized_code")
        departments = Department.objects.filter(company=company, is_active=True)
        roles = role_names_for(actor)
        if "department_manager" in roles and not roles.intersection(
            {"system_admin", "finance", "equipment"}
        ):
            departments = departments.filter(
                pk__in=resolve_department_ids(actor, company)
            )
        self.fields["department"].queryset = departments.order_by("normalized_code")
        self.fields["employee"].queryset = Employee.objects.filter(
            company=company
        ).select_related("department").order_by("normalized_employee_no")
        _bootstrap_widgets(self)

    def clean_idempotency_key(self):
        value = str(self.cleaned_data.get("idempotency_key") or "").strip()
        if not value:
            raise ValidationError("创建幂等键无效，请刷新页面重试。")
        return value

    def clean(self):
        cleaned = super().clean()
        domain = cleaned.get("count_domain")
        warehouse = cleaned.get("warehouse")
        department = cleaned.get("department")
        employee = cleaned.get("employee")
        if domain == SupplyCountDomain.WAREHOUSE_STOCK:
            if warehouse is None:
                self.add_error("warehouse", "仓库库存盘点必须选择仓库。")
            if department is not None or employee is not None:
                raise ValidationError("仓库库存盘点不得填写部门或员工。")
        elif domain == SupplyCountDomain.CUSTODY:
            if department is None:
                self.add_error("department", "保管盘点必须选择部门。")
            if warehouse is not None:
                self.add_error("warehouse", "保管盘点不得选择仓库。")
            if employee is not None and (
                department is None or employee.department_id != department.pk
            ):
                self.add_error("employee", "盘点员工必须属于所选部门。")
        if domain and not can_create_supply_count_task(
            self.actor,
            company=self.company,
            count_domain=domain,
            department=department,
        ):
            raise PermissionDenied("您没有在所选范围创建盘点任务的权限。")
        return cleaned


class SupplyCountRecordForm(forms.Form):
    counted_quantity = forms.DecimalField(
        label="实盘数量",
        max_digits=18,
        decimal_places=4,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.0001", "min": "0"}),
    )
    remark = forms.CharField(
        label="差异原因/备注",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    adjustment_unit_cost = forms.DecimalField(
        label="零库存盘盈单位成本",
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0"),
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.000001", "min": "0"}),
    )
    zero_cost_reason = forms.CharField(
        label="0 成本原因",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, actor=None, line=None, **kwargs):
        if actor is None or line is None:
            raise PermissionDenied("实盘录入表单必须绑定当前用户和盘点行。")
        self.actor = actor
        self.line = line
        initial = dict(kwargs.pop("initial", {}) or {})
        initial.setdefault("counted_quantity", line.counted_quantity)
        initial.setdefault("remark", line.remark)
        initial.setdefault("adjustment_unit_cost", line.adjustment_unit_cost)
        initial.setdefault("zero_cost_reason", line.zero_cost_reason)
        super().__init__(*args, initial=initial, **kwargs)
        show_adjustment_cost = bool(
            can_view_supply_cost(actor)
            and line.count_task.count_domain == SupplyCountDomain.WAREHOUSE_STOCK
            and line.expected_quantity == 0
        )
        if not show_adjustment_cost:
            self.fields.pop("adjustment_unit_cost")
            self.fields.pop("zero_cost_reason")
        _bootstrap_widgets(self)

    def clean_counted_quantity(self):
        return quantize_quantity(self.cleaned_data["counted_quantity"])


class SupplyCountAddItemForm(forms.Form):
    item = forms.ModelChoiceField(
        label="新增应盘物品", queryset=SupplyItem.objects.none()
    )

    def __init__(self, *args, actor=None, task=None, **kwargs):
        if actor is None or task is None:
            raise PermissionDenied("新增盘点物品表单必须绑定任务和用户。")
        require_execute_supply_count_task(actor, task)
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = SupplyItem.objects.filter(
            company=task.company, is_active=True
        ).exclude(count_lines__count_task=task).order_by("normalized_item_code")
        _bootstrap_widgets(self)


class SupplyCountCancelForm(forms.Form):
    reason = forms.CharField(
        label="取消原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class SupplyCountAdjustmentCostForm(forms.Form):
    unit_cost = forms.DecimalField(
        label="盘盈单位成本",
        max_digits=18,
        decimal_places=6,
        min_value=Decimal("0"),
        widget=forms.NumberInput(attrs={"step": "0.000001", "min": "0"}),
    )
    zero_cost_reason = forms.CharField(
        label="0 成本原因",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, actor=None, line=None, **kwargs):
        if actor is None or line is None or not can_view_supply_cost(actor):
            raise PermissionDenied("您没有维护盘盈成本的权限。")
        require_execute_supply_count_task(actor, line.count_task)
        initial = dict(kwargs.pop("initial", {}) or {})
        initial.setdefault("unit_cost", line.adjustment_unit_cost)
        initial.setdefault("zero_cost_reason", line.zero_cost_reason)
        super().__init__(*args, initial=initial, **kwargs)
        _bootstrap_widgets(self)


class SupplyCountCustodyResolutionForm(forms.Form):
    resolution_type = forms.ChoiceField(
        label="解决方式", choices=SupplyCountResolutionType.choices
    )
    target_warehouse = forms.ModelChoiceField(
        label="归还仓库", queryset=SupplyWarehouse.objects.none(), required=False
    )
    target_department = forms.ModelChoiceField(
        label="目标责任部门", queryset=Department.objects.none(), required=False
    )
    target_employee = forms.ModelChoiceField(
        label="目标责任员工", queryset=Employee.objects.none(), required=False
    )
    business_date = forms.DateField(
        label="业务日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(
        label="解决原因", max_length=500, widget=forms.Textarea(attrs={"rows": 3})
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *args, actor=None, line=None, **kwargs):
        if actor is None or line is None:
            raise PermissionDenied("差异解决表单必须绑定盘点行和用户。")
        require_execute_supply_count_task(actor, line.count_task)
        self.actor = actor
        self.line = line
        initial = dict(kwargs.pop("initial", {}) or {})
        initial.setdefault("business_date", timezone.localdate())
        initial.setdefault("idempotency_key", str(uuid.uuid4()))
        super().__init__(*args, initial=initial, **kwargs)
        if line.difference_quantity and line.difference_quantity > 0:
            self.fields["resolution_type"].choices = [
                (SupplyCountResolutionType.CORRECTION, "盘点正向更正")
            ]
        self.fields["target_warehouse"].queryset = SupplyWarehouse.objects.filter(
            company=line.company, is_active=True
        ).order_by("normalized_code")
        departments = Department.objects.filter(company=line.company, is_active=True)
        roles = role_names_for(actor)
        department_ids = None
        if "department_manager" in roles and not roles.intersection(
            {"system_admin", "finance", "equipment"}
        ):
            department_ids = resolve_department_ids(actor, line.company)
            departments = departments.filter(
                pk__in=department_ids
            )
        self.fields["target_department"].queryset = departments.order_by(
            "normalized_code"
        )
        employees = Employee.objects.filter(
            company=line.company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )
        if department_ids is not None:
            employees = employees.filter(department_id__in=department_ids)
        self.fields["target_employee"].queryset = employees.select_related(
            "department"
        ).order_by("normalized_employee_no")
        _bootstrap_widgets(self)

    def clean_idempotency_key(self):
        value = str(self.cleaned_data.get("idempotency_key") or "").strip()
        if not value:
            raise ValidationError("动作幂等键无效，请刷新页面重试。")
        return value

    def clean(self):
        cleaned = super().clean()
        resolution_type = cleaned.get("resolution_type")
        if resolution_type == SupplyCountResolutionType.RETURN:
            if cleaned.get("target_warehouse") is None:
                self.add_error("target_warehouse", "归还解决必须选择仓库。")
        elif resolution_type == SupplyCountResolutionType.TRANSFER:
            department = cleaned.get("target_department")
            employee = cleaned.get("target_employee")
            if department is None:
                self.add_error("target_department", "转交解决必须选择目标部门。")
            if employee is not None and (
                department is None or employee.department_id != department.pk
            ):
                self.add_error("target_employee", "目标员工必须属于目标部门。")
        return cleaned
