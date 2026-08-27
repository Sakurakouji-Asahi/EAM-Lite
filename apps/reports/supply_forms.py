"""Validated filters for the fixed Sprint 18 low-value-goods reports."""

from __future__ import annotations

from django import forms

from apps.masterdata.models import Department, Employee
from apps.masterdata.permissions import resolve_department_ids, role_names_for
from apps.reports.schemas import SUPPLY_REPORT_KEYS
from apps.supplies.models import (
    SupplyCategory,
    SupplyCountDomain,
    SupplyCountStatus,
    SupplyCustodyAction,
    SupplyCustodyStatus,
    SupplyDocumentStatus,
    SupplyDocumentType,
    SupplyItemType,
    SupplyStockMovementType,
    SupplyWarehouse,
)


LOW_STOCK_SCOPE_CHOICES = (
    ("formal", "正式低库存预警"),
    ("unconfigured", "未配置默认仓库"),
)
LOW_STOCK_FILTER_CHOICES = (
    ("", "全部"),
    ("yes", "仅低库存"),
    ("no", "仅非低库存"),
)

COMMON = {"category", "item_code", "management_mode"}
FILTERS_BY_REPORT = {
    "supply_stock_balance": COMMON
    | {"warehouse", "include_zero", "low_stock"},
    "supply_low_stock": COMMON | {"low_stock_scope"},
    "supply_stock_movement": COMMON
    | {"date_from", "date_to", "warehouse"},
    "supply_stock_ledger": COMMON
    | {
        "date_from",
        "date_to",
        "warehouse",
        "document_type",
        "document_status",
        "movement_type",
    },
    "supply_issue_detail": COMMON
    | {"date_from", "date_to", "department", "employee"},
    "supply_department_issue": COMMON
    | {"date_from", "date_to", "department"},
    "supply_employee_issue": COMMON
    | {"date_from", "date_to", "department", "employee"},
    "supply_custody_balance": COMMON
    | {"department", "employee", "custody_status", "clearance_pending"},
    "supply_custody_movement": COMMON
    | {
        "date_from",
        "date_to",
        "department",
        "employee",
        "custody_action",
    },
    "supply_count_difference": COMMON
    | {
        "date_from",
        "date_to",
        "warehouse",
        "department",
        "employee",
        "count_domain",
        "count_status",
        "differences_only",
    },
    "controlled_non_fixed_assets": {"department", "employee"},
    "supply_management_amount": set(),
}


class SupplyReportFilterForm(forms.Form):
    date_from = forms.DateField(
        label="开始日期",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        label="结束日期",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    warehouse = forms.ModelChoiceField(
        label="仓库", required=False, queryset=SupplyWarehouse.objects.none()
    )
    category = forms.ModelChoiceField(
        label="分类", required=False, queryset=SupplyCategory.objects.none()
    )
    item_code = forms.CharField(label="物品编码", required=False, max_length=100)
    management_mode = forms.ChoiceField(
        label="管理模式",
        required=False,
        choices=(("", "全部"), *SupplyItemType.choices),
    )
    department = forms.ModelChoiceField(
        label="部门", required=False, queryset=Department.objects.none()
    )
    employee = forms.ModelChoiceField(
        label="员工", required=False, queryset=Employee.objects.none()
    )
    document_type = forms.ChoiceField(
        label="单据类型",
        required=False,
        choices=(("", "全部"), *SupplyDocumentType.choices),
    )
    document_status = forms.ChoiceField(
        label="单据状态",
        required=False,
        choices=(("", "全部"), *SupplyDocumentStatus.choices),
    )
    movement_type = forms.ChoiceField(
        label="流水类型",
        required=False,
        choices=(("", "全部"), *SupplyStockMovementType.choices),
    )
    custody_status = forms.ChoiceField(
        label="保管状态",
        required=False,
        choices=(("", "全部"), *SupplyCustodyStatus.choices),
    )
    custody_action = forms.ChoiceField(
        label="保管动作",
        required=False,
        choices=(("", "全部"), *SupplyCustodyAction.choices),
    )
    count_domain = forms.ChoiceField(
        label="盘点域",
        required=False,
        choices=(("", "全部"), *SupplyCountDomain.choices),
    )
    count_status = forms.ChoiceField(
        label="盘点状态",
        required=False,
        choices=(("", "全部"), *SupplyCountStatus.choices),
    )
    low_stock_scope = forms.ChoiceField(
        label="预警范围", required=False, choices=LOW_STOCK_SCOPE_CHOICES
    )
    low_stock = forms.ChoiceField(
        label="是否低库存", required=False, choices=LOW_STOCK_FILTER_CHOICES
    )
    include_zero = forms.BooleanField(label="包含零余额", required=False)
    differences_only = forms.BooleanField(label="仅显示差异", required=False)
    clearance_pending = forms.BooleanField(label="仅待处理清退保管", required=False)

    def __init__(self, *args, actor, company, report_key, **kwargs):
        if report_key not in SUPPLY_REPORT_KEYS:
            raise ValueError("未知低值物品报表类型。")
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.company = company
        self.report_key = report_key
        allowed_fields = FILTERS_BY_REPORT[report_key]
        for field_name in tuple(self.fields):
            if field_name not in allowed_fields:
                self.fields.pop(field_name)

        if "warehouse" in self.fields:
            self.fields["warehouse"].queryset = SupplyWarehouse.objects.filter(
                company=company
            ).order_by("normalized_code")
        if "category" in self.fields:
            self.fields["category"].queryset = SupplyCategory.objects.filter(
                company=company
            ).order_by("normalized_code")

        roles = role_names_for(actor)
        global_roles = {
            "system_admin", "finance", "warehouse", "equipment", "management"
        }
        department_qs = Department.objects.filter(company=company)
        employee_qs = Employee.objects.filter(company=company)
        if not roles.intersection(global_roles):
            if "department_manager" in roles:
                department_ids = resolve_department_ids(actor, company)
                department_qs = department_qs.filter(pk__in=department_ids)
                employee_qs = employee_qs.filter(department_id__in=department_ids)
            elif "employee" in roles:
                employee_qs = employee_qs.filter(user=actor)
                department_qs = department_qs.filter(
                    pk__in=employee_qs.values("department_id")
                )
            else:
                department_qs = department_qs.none()
                employee_qs = employee_qs.none()
        if "department" in self.fields:
            self.fields["department"].queryset = department_qs.order_by(
                "normalized_code"
            )
        if "employee" in self.fields:
            self.fields["employee"].queryset = employee_qs.order_by(
                "normalized_employee_no"
            )

        if report_key == "supply_stock_movement":
            self.fields["date_from"].required = True
            self.fields["date_to"].required = True
        if "low_stock_scope" in self.fields and not self.is_bound:
            self.initial.setdefault("low_stock_scope", "formal")
        if "custody_status" in self.fields and not self.is_bound:
            self.initial.setdefault("custody_status", SupplyCustodyStatus.OPEN)

        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            else:
                widget.attrs.setdefault(
                    "class",
                    "form-select" if isinstance(widget, forms.Select) else "form-control",
                )

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("date_from")
        end = cleaned.get("date_to")
        if bool(start) != bool(end):
            raise forms.ValidationError("开始日期和结束日期必须同时填写。")
        if start and end and end < start:
            raise forms.ValidationError("结束日期不得早于开始日期。")
        employee = cleaned.get("employee")
        department = cleaned.get("department")
        if employee and department and employee.department_id != department.pk:
            raise forms.ValidationError("员工不属于所选部门。")
        return cleaned

    def as_filters(self):
        if not self.is_valid():
            raise ValueError("无效报表筛选不能序列化。")
        filters = {}
        for key, value in self.cleaned_data.items():
            if value in (None, "", False):
                continue
            if hasattr(value, "pk"):
                value = str(value.pk)
            filters[key] = value
        return filters


__all__ = ["FILTERS_BY_REPORT", "SupplyReportFilterForm"]
