"""Readable receiving parties within the existing transfer form choices."""
from apps.assets.form_options import DepartmentEmployeeSelect


def configure_custody_transfer_choices(form):
    department = form.fields["target_department"]
    employee = form.fields["target_employee"]
    department.widget.attrs["data-choice-search"] = ""
    employee.widget = DepartmentEmployeeSelect(attrs={**employee.widget.attrs,
        "data-choice-search": "", "data-department-field": form["target_department"].html_name})
    employee.label_from_instance = lambda person: (
        f"{person.employee_no} · {person.name} · {person.department.code} / {person.department.name}"
    )
    employee.widget.choices = employee.choices
