"""Permission-filtered options with browser-side assignment hints."""
from django import forms


def location_path(location):
    parts, seen = [], set()
    while location is not None and location.pk not in seen:
        seen.add(location.pk)
        parts.append(location.name)
        location = location.parent
    return " / ".join(reversed(parts))


class HierarchyOptionLabels:
    """Load names and parent IDs once when this form first renders a tree choice."""

    def __init__(self, model, company):
        self.model = model
        self.company_id = company.pk
        self.nodes = None
        self.paths = {}

    def __call__(self, node):
        if self.nodes is None:
            self.nodes = {
                pk: (parent_id, name) for pk, parent_id, name in
                self.model.objects.filter(company_id=self.company_id).values_list("pk", "parent_id", "name")
            }
        current, branch, seen = node.pk, [], set()
        while current in self.nodes and current not in self.paths and current not in seen:
            seen.add(current)
            branch.append(current)
            current = self.nodes[current][0]
        path = self.paths.get(current, "")
        for pk in reversed(branch):
            name = self.nodes[pk][1]
            path = f"{path} / {name}" if path else name
            self.paths[pk] = path
        return f"{node.code} / {self.paths.get(node.pk, node.name)}"


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
