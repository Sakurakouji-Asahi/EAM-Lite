"""Assignment inputs for a bounded selection of asset drafts."""
from django import forms

from apps.assets.form_options import link_assignment_options
from apps.assets.permissions import assignable_asset_departments
from apps.masterdata.models import Location
from apps.masterdata.directory import employee_department_tree
from apps.masterdata.permissions import scoped_departments, scoped_employees


class BulkRegistrationFilterForm(forms.Form):
    q = forms.CharField(label="搜索待建档资产", max_length=200, required=False)
    department = forms.ModelChoiceField(label="部门（含下属班组）", queryset=None, required=False)
    page_size = forms.TypedChoiceField(
        label="每页显示", coerce=int, required=False,
        choices=((25, "25 条"), (50, "50 条"), (100, "100 条"), (200, "200 条")),
    )
    import_batch = forms.IntegerField(label="导入批次", min_value=1, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, actor, company, **kwargs):
        kwargs.setdefault("auto_id", "id_bulk_filter_%s")
        super().__init__(*args, **kwargs)
        departments = scoped_departments(actor, company).order_by("normalized_code")
        _, _, labels = employee_department_tree(departments, [])
        self.fields["department"].queryset = departments
        self.fields["department"].label_from_instance = lambda item: labels.get(item.pk, item.name)
        self.fields["department"].empty_label = "全部部门"
        self.fields["q"].widget.attrs["placeholder"] = "草稿号、设备编号、名称、型号或责任人"
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )


class BulkDraftAssignmentForm(forms.Form):
    department = forms.ModelChoiceField(label="统一部门", queryset=None, required=False)
    responsible_employee = forms.ModelChoiceField(label="统一责任人", queryset=None, required=False)
    location = forms.ModelChoiceField(label="统一位置", queryset=None, required=False)
    reason = forms.CharField(label="补充说明", max_length=500, widget=forms.TextInput)

    def __init__(self, *args, actor, company, **kwargs):
        kwargs.setdefault("use_required_attribute", False)
        super().__init__(*args, **kwargs)
        departments = assignable_asset_departments(actor, company)
        self.fields["department"].queryset = departments.order_by("normalized_code")
        self.fields["responsible_employee"].queryset = scoped_employees(actor, company).filter(
            is_active=True, employment_status="active", department__in=departments,
        ).order_by("normalized_employee_no")
        self.fields["location"].queryset = Location.objects.filter(
            company=company, is_active=True, children__isnull=True,
        ).order_by("level", "normalized_code")
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            if isinstance(field, forms.ModelChoiceField):
                field.empty_label = "保持原值"
        link_assignment_options(self, department="department", employee="responsible_employee", location="location")

    def clean(self):
        data = super().clean()
        if not any(data.get(name) for name in ("department", "responsible_employee", "location")):
            raise forms.ValidationError("请至少选择一个要补充的字段。")
        return data
