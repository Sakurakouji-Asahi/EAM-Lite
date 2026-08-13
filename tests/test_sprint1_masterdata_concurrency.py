import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction

from apps.masterdata.models import Company, Department, Employee
from apps.masterdata.services import set_user_roles


pytestmark = pytest.mark.django_db(transaction=True)
PASSWORD = "Concurrency-Only-2026!"


@pytest.fixture(autouse=True)
def require_postgresql(django_db_blocker):
    with django_db_blocker.unblock():
        if connection.vendor != "postgresql":
            pytest.skip("Sprint 1 master-data concurrency requires PostgreSQL")


def _company():
    return Company.objects.create(
        code="CONCURRENCY",
        normalized_code="concurrency",
        name="并发测试公司",
        short_name="并发",
    )


def _raw_update(model, object_id, values, barrier):
    close_old_connections()
    try:
        barrier.wait(timeout=10)
        with transaction.atomic():
            changed = model.objects.filter(pk=object_id).update(**values)
            assert changed == 1
        return "ok"
    except IntegrityError:
        return "integrity"
    except ValidationError:
        return "validation"
    finally:
        close_old_connections()


def test_concurrent_opposite_tree_reparents_cannot_create_cycle():
    company = _company()
    first = Department.objects.create(
        company=company, code="A", normalized_code="a", name="部门 A"
    )
    second = Department.objects.create(
        company=company, code="B", normalized_code="b", name="部门 B"
    )
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(
                _raw_update,
                Department,
                first.pk,
                {"parent_id": second.pk},
                barrier,
            ),
            executor.submit(
                _raw_update,
                Department,
                second.pk,
                {"parent_id": first.pk},
                barrier,
            ),
        ]
        outcomes = [result.result(timeout=30) for result in results]

    assert sorted(outcomes) == ["integrity", "ok"]
    first.refresh_from_db()
    second.refresh_from_db()
    assert not (first.parent_id == second.pk and second.parent_id == first.pk)


def test_concurrent_manager_bind_and_employee_disable_preserve_validity():
    company = _company()
    home = Department.objects.create(
        company=company, code="HOME", normalized_code="home", name="经理所属部门"
    )
    managed = Department.objects.create(
        company=company, code="MANAGED", normalized_code="managed", name="被管理部门"
    )
    employee = Employee.objects.create(
        company=company,
        department=home,
        employee_no="M1",
        normalized_employee_no="m1",
        name="候选经理",
    )
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(
                _raw_update,
                Department,
                managed.pk,
                {"manager_employee_id": employee.pk},
                barrier,
            ),
            executor.submit(
                _raw_update,
                Employee,
                employee.pk,
                {"employment_status": "leaving", "is_active": False},
                barrier,
            ),
        ]
        outcomes = [result.result(timeout=30) for result in results]

    assert sorted(outcomes) == ["ok", "validation"]
    managed.refresh_from_db()
    employee.refresh_from_db()
    assert managed.manager_employee_id is None or (
        employee.employment_status == "active" and employee.is_active
    )


def _remove_own_admin_role(*, company_id, user_id, barrier):
    close_old_connections()
    try:
        user = get_user_model().objects.get(pk=user_id)
        company = Company.objects.get(pk=company_id)
        barrier.wait(timeout=10)
        set_user_roles(
            actor=user,
            company=company,
            user=user,
            roles=(),
            reason="并发末位管理员保护测试",
            current_password=PASSWORD,
        )
        return "ok"
    except ValidationError:
        return "validation"
    finally:
        close_old_connections()


def test_concurrent_admin_removals_leave_one_login_capable_admin():
    company = _company()
    admin_group, _ = Group.objects.get_or_create(name="system_admin")
    admins = []
    for index in range(2):
        admin = get_user_model().objects.create_user(
            username=f"concurrent-admin-{index}",
            password=PASSWORD,
            display_name=f"并发管理员 {index}",
        )
        admin.groups.add(admin_group)
        admins.append(admin)
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _remove_own_admin_role,
                company_id=company.pk,
                user_id=admin.pk,
                barrier=barrier,
            )
            for admin in admins
        ]
        outcomes = [future.result(timeout=30) for future in futures]

    assert sorted(outcomes) == ["ok", "validation"]
    assert (
        get_user_model()
        .objects.filter(is_active=True, groups__name="system_admin")
        .distinct()
        .count()
        == 1
    )
