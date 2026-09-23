"""Assignment inputs for a bounded selection of asset drafts."""
from django import forms

from apps.assets.form_options import link_assignment_options
from apps.assets.permissions import assignable_asset_departments
from apps.masterdata.models import Location
from apps.masterdata.permissions import scoped_employees


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
