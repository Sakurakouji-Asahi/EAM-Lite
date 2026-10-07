"""Readable relationship choices within each form's existing allowed options."""
from django import forms


class AllowedHierarchyLabels:
    def __init__(self, field):
        self.field = field
        self.nodes = None
        self.paths = {}

    def __call__(self, obj):
        if self.nodes is None:
            # Do not fetch inactive, foreign or excluded ancestors for labels.
            self.nodes = {
                pk: (parent_id, name)
                for pk, parent_id, name in self.field.queryset.values_list(
                    "pk", "parent_id", "name"
                )
            }
        branch, seen, current = [], set(), obj.pk
        while current in self.nodes and current not in self.paths and current not in seen:
            seen.add(current)
            parent_id, name = self.nodes[current]
            branch.append((current, name))
            current = parent_id
        path = self.paths.get(current, "")
        for pk, name in reversed(branch):
            path = f"{path} / {name}" if path else name
            self.paths[pk] = path
        return f"{obj.code} · {self.paths.get(obj.pk, obj.name)}"


def configure_relationship_choices(form):
    """Change labels and opt in to lookup; never change accepted choices."""
    for name in ("parent", "department", "manager_employee"):
        field = form.fields.get(name)
        if not isinstance(field, forms.ModelChoiceField) or not isinstance(field.widget, forms.Select):
            continue
        field.widget.attrs["data-choice-search"] = ""
        if name == "manager_employee":
            field.label_from_instance = lambda obj: (
                f"{obj.employee_no} · {obj.name} · {obj.department.code} / {obj.department.name}"
            )
        else:
            field.label_from_instance = AllowedHierarchyLabels(field)
