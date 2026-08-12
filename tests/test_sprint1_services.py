import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError

from apps.audit.models import AuditLog
from apps.masterdata.models import Company, Department, Employee, UserDepartmentScope
from apps.masterdata.permissions import resolve_department_ids
from apps.masterdata.services import (
    assign_department_scope,
    create_department,
    create_employee,
    revoke_department_scope,
    link_employee_user,
    set_system_setting,
    set_user_roles,
    update_employee,
)


pytestmark = pytest.mark.django_db
PASSWORD = "Valid-Password-2026!"


def make_user(username, *roles):
    user = get_user_model().objects.create_user(
        username=username,
        password=PASSWORD,
        display_name=username,
    )
    user.groups.set(Group.objects.filter(name__in=roles))
    return user


def make_company():
    return Company.objects.create(
        code="C1",
        normalized_code="c1",
        name="测试公司",
        short_name="测试",
    )


def test_employee_status_service_clears_manager_and_audits_with_company():
    owner = make_company()
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    base = create_department(
        actor=admin,
        company=owner,
        data={"code": "BASE", "name": "基础部"},
    )
    manager = create_employee(
        actor=hr,
        company=owner,
        data={
            "employee_no": "E1",
            "name": "经理",
            "department": base,
            "employment_status": "active",
            "is_active": True,
        },
    )
    managed = create_department(
        actor=admin,
        company=owner,
        data={"code": "MANAGED", "name": "被管理部", "manager_employee": manager},
    )

    update_employee(
        actor=hr,
        employee=manager,
        data={"employment_status": "leaving"},
    )
    manager.refresh_from_db()
    managed.refresh_from_db()

    assert not manager.is_active
    assert managed.manager_employee is None
    audit = AuditLog.objects.get(action="manager_cleared", object_id=str(managed.pk))
    assert audit.company == owner


def test_scope_union_descendants_revoke_and_role_does_not_come_from_scope():
    owner = make_company()
    admin = make_user("admin", "system_admin")
    manager = make_user("manager")
    root = create_department(
        actor=admin, company=owner, data={"code": "R", "name": "根"}
    )
    child = create_department(
        actor=admin,
        company=owner,
        data={"code": "C", "name": "子", "parent": root},
    )
    other = create_department(
        actor=admin, company=owner, data={"code": "O", "name": "其他"}
    )
    scope1 = assign_department_scope(
        actor=admin,
        company=owner,
        user=manager,
        department=root,
        reason="授权根部门及下级",
    )
    assign_department_scope(
        actor=admin,
        company=owner,
        user=manager,
        department=other,
        include_descendants=False,
        reason="授权单一部门",
    )

    assert resolve_department_ids(manager, owner) == set()
    manager.groups.add(Group.objects.get(name="department_manager"))
    assert resolve_department_ids(manager, owner) == {root.pk, child.pk, other.pk}
    revoke_department_scope(actor=admin, scope=scope1, reason="调整范围")
    assert resolve_department_ids(manager, owner) == {other.pk}
    assert UserDepartmentScope.objects.filter(pk=scope1.pk, is_active=False).exists()


def test_existing_scope_blocks_reverse_cross_company_employee_link():
    c1 = make_company()
    c2 = Company.objects.create(
        code="C2", normalized_code="c2", name="第二公司", short_name="第二", is_active=False
    )
    admin = make_user("admin", "system_admin")
    user = make_user("scoped-user")
    root = create_department(
        actor=admin, company=c1, data={"code": "C1-D", "name": "第一公司部门"}
    )
    assign_department_scope(
        actor=admin,
        company=c1,
        user=user,
        department=root,
        reason="第一公司授权",
    )
    c2_department = Department.objects.create(
        company=c2, code="C2-D", normalized_code="c2-d", name="第二公司部门"
    )
    target = Employee.objects.create(
        company=c2,
        department=c2_department,
        employee_no="C2-E",
        normalized_employee_no="c2-e",
        name="第二公司员工",
    )

    with pytest.raises(PermissionDenied, match="当前公司"):
        link_employee_user(actor=admin, employee=target, user=user)


def test_department_manager_role_requires_an_active_scope():
    owner = make_company()
    admin = make_user("admin", "system_admin")
    target = make_user("target")

    with pytest.raises(ValidationError, match="必须先.*部门范围"):
        set_user_roles(
            actor=admin,
            company=owner,
            user=target,
            roles=["department_manager"],
            reason="授予部门经理",
        )

    department = create_department(
        actor=admin, company=owner, data={"code": "D1", "name": "部门"}
    )
    assign_department_scope(
        actor=admin,
        company=owner,
        user=target,
        department=department,
        reason="先配置范围",
    )
    set_user_roles(
        actor=admin,
        company=owner,
        user=target,
        roles=["department_manager"],
        reason="授予部门经理",
    )
    assert target.groups.filter(name="department_manager").exists()


def test_last_login_capable_admin_and_finance_are_protected():
    owner = make_company()
    admin = make_user("admin", "system_admin")
    finance = make_user("finance", "finance")

    with pytest.raises(ValidationError, match="最后一名可登录"):
        set_user_roles(
            actor=admin,
            company=owner,
            user=admin,
            roles=[],
            reason="错误移除",
            current_password=PASSWORD,
        )
    with pytest.raises(ValidationError, match="最后一名可登录"):
        set_user_roles(
            actor=admin,
            company=owner,
            user=finance,
            roles=[],
            reason="错误移除",
            current_password=PASSWORD,
        )


def test_system_setting_registry_and_sprint_boundary():
    owner = make_company()
    admin = make_user("admin", "system_admin")
    finance = make_user("finance-settings", "finance")

    setting = set_system_setting(
        actor=admin,
        company=owner,
        key="attachment_max_size_bytes",
        value="1024",
    )
    assert setting.value == "1024"
    with pytest.raises(ValidationError, match="未知"):
        set_system_setting(
            actor=admin, company=owner, key="secret_key", value="do-not-store"
        )
    # Since Sprint 4 the key is live, but it remains a finance-owned business
    # setting: system_admin cannot write it on finance's behalf.
    with pytest.raises(PermissionDenied):
        set_system_setting(
            actor=admin,
            company=owner,
            key="fixed_asset_warning_amount",
            value="5000.00",
        )
    warning = set_system_setting(
        actor=finance,
        company=owner,
        key="fixed_asset_warning_amount",
        value="5000.00",
    )
    assert warning.value == "5000.00"
