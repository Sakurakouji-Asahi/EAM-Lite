"""Relationship lookup keeps original options, authorization and error input."""
import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.forms import AssetCategoryForm, DepartmentForm, EmployeeForm, LocationForm
from apps.masterdata.models import Department
from tests.test_sprint3_support import (
    make_category, make_company, make_department, make_employee, make_location, make_user,
)

pytestmark = pytest.mark.django_db


def test_relationship_labels_distinguish_branches_without_fetching_excluded_names(client):
    company = make_company("CHOICES")
    foreign = make_company("CHOICES-FOREIGN", active=False)
    actor = make_user("choices-admin", "system_admin", "hr", "equipment")
    client.force_login(actor)
    for slug, factory, form_class, field_name in (
        ("department", make_department, DepartmentForm, "parent"),
        ("employee", make_department, EmployeeForm, "department"),
        ("location", make_location, LocationForm, "parent"),
        ("category", make_category, AssetCategoryForm, "parent"),
    ):
        first = factory(company, f"{slug}-FIRST")
        second = factory(company, f"{slug}-SECOND")
        children = [factory(company, f"{slug}-CHILD-{index}", parent=parent)
                    for index, parent in enumerate((first, second))]
        for child in children:
            child.name = "同名资料"
            child.save(update_fields=["name"])
        hidden = factory(company, f"{slug}-HIDDEN", active=False)
        hidden.name = "不可选上级名称"
        hidden.save(update_fields=["name"])
        orphan = factory(company, f"{slug}-VISIBLE", parent=hidden)
        foreign_node = factory(foreign, f"{slug}-FOREIGN")
        expected = form_class(actor=actor, company=company).fields[field_name].queryset
        before = AuditLog.objects.count()
        page = client.get(reverse(f"masterdata:{slug}-create"))
        assert page.status_code == 200
        field = page.context["form"].fields[field_name]
        assert set(field.queryset.values_list("pk", flat=True)) == set(expected.values_list("pk", flat=True))
        labels = {str(value): label for value, label in field.choices}
        for child, parent in zip(children, (first, second)):
            assert labels[str(child.pk)] == f"{child.code} · {parent.name} / 同名资料"
        assert labels[str(orphan.pk)] == f"{orphan.code} · {orphan.name}"
        assert str(hidden.pk) not in labels and str(foreign_node.pk) not in labels
        assert "不可选上级名称" not in page.content.decode()
        assert "data-choice-search" in field.widget.attrs
        assert AuditLog.objects.count() == before
        if slug != "employee":
            editing = client.get(reverse(f"masterdata:{slug}-edit", args=[children[0].pk]))
            options = editing.context["form"].fields["parent"].queryset
            assert not options.filter(pk=children[0].pk).exists()


def test_manager_lookup_and_role_specific_fields_keep_existing_authorization(client):
    company = make_company("MANAGER-CHOICES")
    department = make_department(company, "ACTIVE-DEPARTMENT")
    inactive_department = make_department(company, "INACTIVE-DEPARTMENT", active=False)
    active = make_employee(company, department, "ELIGIBLE")
    make_employee(company, department, "LEAVING", status="leaving", active=False)
    make_employee(company, department, "DISABLED", active=False)
    make_employee(company, inactive_department, "INACTIVE-ORG")
    hr = make_user("choices-hr", "system_admin", "hr")
    client.force_login(hr)
    page = client.get(reverse("masterdata:department-create"))
    manager = page.context["form"].fields["manager_employee"]
    assert list(manager.queryset.values_list("pk", flat=True)) == [active.pk]
    assert manager.label_from_instance(active) == (
        f"ELIGIBLE · {active.name} · {department.code} / {department.name}"
    )
    editing = client.get(reverse("masterdata:employee-edit", args=[active.pk]))
    assert editing.context["form"].fields["employment_status"].disabled
    assert editing.context["form"].fields["termination_date"].disabled
    equipment = make_user("choices-equipment", "equipment")
    client.force_login(equipment)
    category = client.get(reverse("masterdata:category-create"))
    assert category.status_code == 200
    assert "default_coding_scheme" not in category.context["form"].fields
    assert client.get(reverse("masterdata:department-create")).status_code == 403
    assert client.get(reverse("masterdata:employee-create")).status_code == 403


def test_error_form_keeps_selected_relationship_and_rejects_foreign_submission(client):
    company = make_company("BOUND-CHOICES")
    parent = make_department(company, "BOUND-PARENT")
    foreign = make_company("BOUND-FOREIGN", active=False)
    other = make_department(foreign, "FOREIGN-PARENT")
    actor = make_user("choices-bound-hr", "system_admin")
    client.force_login(actor)
    url = reverse("masterdata:department-create")
    source = reverse("masterdata:department-list") + "?q=保留&page=2"
    before = Department.objects.count()
    error = client.post(url, {"name": "", "code": "UNSAVED", "parent": parent.pk, "return_to": source})
    form = error.context["form"]
    assert error.status_code == 200 and "name" in form.errors
    assert form["parent"].value() == str(parent.pk)
    assert form["code"].value() == "UNSAVED"
    assert error.context["masterdata_return_to"] == source
    assert "data-choice-search" in form.fields["parent"].widget.attrs
    invalid = client.post(url, {"name": "不可越界", "code": "UNSAVED", "parent": other.pk})
    assert invalid.status_code == 200 and "parent" in invalid.context["form"].errors
    assert Department.objects.count() == before
