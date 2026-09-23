"""Permission-filtered options with browser-side assignment hints."""
from django import forms


def location_path(location):
    parts, seen = [], set()
    while location is not None and location.pk not in seen:
        seen.add(location.pk)
        parts.append(location.name)
        location = location.parent
    return " / ".join(reversed(parts))


class DepartmentEmployeeSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if hasattr(value, "instance"):
            option["attrs"]["data-department"] = str(value.instance.department_id)
        return option


def link_assignment_options(form, *, department, employee, location):
    field = form.fields[employee]
    field.widget = DepartmentEmployeeSelect(attrs={
        **field.widget.attrs, "data-department-field": form[department].html_name,
    })
    field.queryset = field.queryset.select_related("department")
    form.fields[location].label_from_instance = location_path
