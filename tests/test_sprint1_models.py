from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.masterdata.models import (
    AssetCategory,
    Company,
    Department,
    Employee,
    Location,
    UserDepartmentScope,
)


pytestmark = pytest.mark.django_db


def company(code="ACME", *, active=True):
    return Company.objects.create(
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 公司",
        short_name=code,
        is_active=active,
    )


def department(owner, code="D1", parent=None):
    return Department.objects.create(
        company=owner,
        code=code,
        normalized_code=code.casefold(),
        name=code,
        parent=parent,
    )


def employee(owner, dept, number="E1", **kwargs):
    defaults = {
        "name": number,
        "employment_status": "active",
        "is_active": True,
    }
    defaults.update(kwargs)
    return Employee.objects.create(
        company=owner,
        department=dept,
        employee_no=number,
        normalized_employee_no=number.casefold(),
        **defaults,
    )


def test_company_code_nfkc_casefold_unique_and_single_active():
    first = Company(
        code=" Ａbc ", name="第一公司", short_name="第一", is_active=True
    )
    first.full_clean()
    first.save()
    assert first.code == "Abc"
    assert first.normalized_code == "abc"

    duplicate = Company(
        code="abc", name="重复", short_name="重复", is_active=False
    )
    with pytest.raises(ValidationError):
        duplicate.full_clean()

    with pytest.raises(IntegrityError), transaction.atomic():
        Company.objects.create(
            code="OTHER",
            normalized_code="other",
            name="第二公司",
            short_name="第二",
            is_active=True,
        )


def test_tree_validation_rejects_cross_company_and_deep_cycles_in_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("deep database tree guards require PostgreSQL")
    c1 = company("C1")
    c2 = company("C2", active=False)
    root = department(c1, "ROOT")
    child = department(c1, "CHILD", root)
    leaf = department(c1, "LEAF", child)

    leaf.parent = department(c2, "FOREIGN")
    with pytest.raises(ValidationError):
        leaf.full_clean()

    with pytest.raises(IntegrityError), transaction.atomic():
        Department.objects.filter(pk=root.pk).update(parent=leaf)


def test_manager_rule_and_employee_status_constraints():
    c1 = company("C1")
    c2 = company("C2", active=False)
    d1 = department(c1)
    d2 = department(c2)
    manager = employee(c1, d1)
    target = department(c1, "D2")
    target.manager_employee = manager
    target.full_clean()

    manager.employment_status = "leaving"
    manager.is_active = False
    manager.full_clean()
    manager.save()
    target.manager_employee = manager
    with pytest.raises(ValidationError):
        target.full_clean()

    foreign = employee(c2, d2, "FOREIGN")
    target.manager_employee = foreign
    with pytest.raises(ValidationError):
        target.full_clean()

    resigned = Employee(
        company=c1,
        department=d1,
        employee_no="RESIGNED",
        name="离职人员",
        employment_status="resigned",
        termination_date=date(2026, 8, 12),
        is_active=False,
    )
    resigned.full_clean()


def test_employee_user_unique_and_scope_company_database_guard():
    c1 = company("C1")
    c2 = company("C2", active=False)
    d1 = department(c1)
    d2 = department(c2)
    user = get_user_model().objects.create_user(
        username="scope-user", password="Test-Password-2026!", display_name="用户"
    )
    employee(c1, d1, user=user)

    if connection.vendor != "postgresql":
        pytest.skip("cross-company scope database guard requires PostgreSQL")
    with pytest.raises(IntegrityError), transaction.atomic():
        employee(c1, d1, "E2", user=user)

    with pytest.raises(IntegrityError), transaction.atomic():
        UserDepartmentScope.objects.create(
            company=c2,
            user=user,
            department=d2,
            include_descendants=True,
        )

    reverse_user = get_user_model().objects.create_user(
        username="reverse-scope-user",
        password="Test-Password-2026!",
        display_name="反向绑定用户",
    )
    UserDepartmentScope.objects.create(
        company=c1,
        user=reverse_user,
        department=d1,
        include_descendants=True,
    )
    reverse_employee = employee(c2, d2, "REVERSE")
    with pytest.raises(IntegrityError), transaction.atomic():
        Employee.objects.filter(pk=reverse_employee.pk).update(user=reverse_user)


@pytest.mark.parametrize(
    ("model", "kwargs", "level_field"),
    [
        (Location, {"location_type": "site"}, "level"),
        (
            AssetCategory,
            {"category_type": "equipment"},
            "category_level",
        ),
    ],
)
def test_location_and_category_tree_level_and_database_cycle(model, kwargs, level_field):
    if connection.vendor != "postgresql":
        pytest.skip("database tree cycle guards require PostgreSQL")
    owner = company("TREE")
    root = model.objects.create(
        company=owner,
        code="R",
        normalized_code="r",
        name="根",
        **kwargs,
    )
    child = model.objects.create(
        company=owner,
        code="C",
        normalized_code="c",
        name="子",
        parent=root,
        **kwargs,
    )
    child.refresh_from_db()
    assert getattr(child, level_field) == 2

    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.filter(pk=root.pk).update(parent=child)
