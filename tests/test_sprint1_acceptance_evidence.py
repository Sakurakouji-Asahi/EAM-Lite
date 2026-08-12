import json
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.models import (
    AssetCategory,
    Company,
    Department,
    Employee,
    InitializationSetting,
    Location,
    SystemSetting,
    UserDepartmentScope,
)
from apps.masterdata.services import (
    SYSTEM_SETTING_REGISTRY,
    _serialize_setting,
    assign_department_scope,
    create_asset_category,
    create_department,
    create_employee,
    create_location,
    get_system_setting,
    link_employee_user,
    refresh_initialization_progress,
    revoke_department_scope,
    set_employee_active,
    set_system_setting,
    set_user_roles,
    update_department,
    update_employee,
)


pytestmark = pytest.mark.django_db
PASSWORD = "Valid-Password-2026!"


@pytest.fixture(autouse=True)
def require_postgresql_18_4():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 1 acceptance evidence requires PostgreSQL 18.4")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        assert cursor.fetchone()[0].startswith("18.4")


def make_user(username, *roles, password=PASSWORD):
    user = get_user_model().objects.create_user(
        username=username,
        password=password,
        display_name=username,
    )
    user.groups.set(Group.objects.filter(name__in=roles))
    return user


def make_company(code="C1", *, active=True):
    return Company.objects.create(
        code=code,
        name=f"{code} 公司",
        short_name=code,
        is_active=active,
    )


def make_department(company, code, *, parent=None):
    return Department.objects.create(
        company=company,
        code=code,
        name=code,
        parent=parent,
    )


def make_employee(company, department, number, **overrides):
    values = {
        "name": number,
        "employment_status": "active",
        "is_active": True,
    }
    values.update(overrides)
    return Employee.objects.create(
        company=company,
        department=department,
        employee_no=number,
        **values,
    )


def test_company_scoped_identifiers_and_department_tree_guards_are_database_backed():
    company = make_company("C1")
    other_company = make_company("C2", active=False)
    root = make_department(company, "D1")
    foreign_root = make_department(other_company, " d1 ")

    assert root.normalized_code == foreign_root.normalized_code == "d1"
    with pytest.raises(IntegrityError), transaction.atomic():
        make_department(company, " d1 ")

    first_employee = make_employee(company, root, "E1")
    foreign_employee = make_employee(other_company, foreign_root, " e1 ")
    assert (
        first_employee.normalized_employee_no
        == foreign_employee.normalized_employee_no
        == "e1"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        make_employee(company, root, " e1 ")

    child = make_department(company, "CHILD", parent=root)
    leaf = make_department(company, "LEAF", parent=child)
    with pytest.raises(IntegrityError), transaction.atomic():
        Department.objects.filter(pk=root.pk).update(parent_id=root.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        Department.objects.filter(pk=child.pk).update(parent_id=foreign_root.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        Department.objects.filter(pk=root.pk).update(parent_id=leaf.pk)

    root.refresh_from_db()
    child.refresh_from_db()
    assert root.parent_id is None
    assert child.parent_id == root.pk


def test_employee_status_candidate_and_user_activation_are_independent():
    company = make_company()
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    department = create_department(
        actor=admin,
        company=company,
        data={"code": "D1", "name": "部门"},
    )
    employee = create_employee(
        actor=hr,
        company=company,
        data={
            "employee_no": "E1",
            "name": "员工",
            "department": department,
            "employment_status": "active",
            "is_active": True,
        },
    )
    assert employee.user is None
    assert employee.can_receive_new_responsibility

    account = make_user("linked-user", "employee")
    link_employee_user(actor=admin, employee=employee, user=account)
    account.is_active = False
    account.save(update_fields=["is_active"])
    employee.refresh_from_db()
    assert employee.is_active
    assert employee.employment_status == "active"
    assert employee.can_receive_new_responsibility

    account.is_active = True
    account.save(update_fields=["is_active"])
    set_employee_active(actor=hr, employee=employee, is_active=False)
    employee.refresh_from_db()
    account.refresh_from_db()
    assert employee.employment_status == "active"
    assert not employee.is_active
    assert not employee.can_receive_new_responsibility
    assert account.is_active

    set_employee_active(actor=hr, employee=employee, is_active=True)
    update_employee(
        actor=hr,
        employee=employee,
        data={"employment_status": "leaving"},
    )
    employee.refresh_from_db()
    account.refresh_from_db()
    assert employee.employment_status == "leaving"
    assert not employee.is_active
    assert employee.termination_date is None
    assert not employee.can_receive_new_responsibility
    assert account.is_active
    with pytest.raises(ValidationError, match="不能重新启用"):
        set_employee_active(actor=hr, employee=employee, is_active=True)

    termination_date = date(2026, 8, 12)
    update_employee(
        actor=hr,
        employee=employee,
        data={
            "employment_status": "resigned",
            "termination_date": termination_date,
        },
    )
    employee.refresh_from_db()
    assert employee.termination_date == termination_date
    with pytest.raises(ValidationError, match="不允许.*恢复为在职"):
        update_employee(
            actor=hr,
            employee=employee,
            data={"employment_status": "active", "termination_date": None},
        )

    active_with_termination = Employee(
        company=company,
        department=department,
        employee_no="BAD-ACTIVE",
        name="错误在职",
        employment_status="active",
        termination_date=termination_date,
    )
    with pytest.raises(ValidationError):
        active_with_termination.full_clean()
    resigned_without_termination = Employee(
        company=company,
        department=department,
        employee_no="BAD-RESIGNED",
        name="错误离职",
        employment_status="resigned",
        is_active=False,
    )
    with pytest.raises(ValidationError):
        resigned_without_termination.full_clean()


def test_masterdata_update_deactivation_and_protect_are_audited_without_deletion():
    company = make_company()
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    root = create_department(
        actor=admin,
        company=company,
        data={"code": "ROOT", "name": "根部门"},
    )
    department = create_department(
        actor=admin,
        company=company,
        data={"code": "D1", "name": "原名称", "parent": root},
    )
    employee = create_employee(
        actor=hr,
        company=company,
        data={"employee_no": "E1", "name": "员工", "department": department},
    )

    update_department(
        actor=admin,
        department=department,
        data={"name": "新名称"},
    )
    update_department(
        actor=admin,
        department=department,
        data={"is_active": False},
    )
    department.refresh_from_db()
    assert not department.is_active
    assert Department.objects.filter(pk=department.pk).exists()
    assert Employee.objects.filter(pk=employee.pk).exists()

    with pytest.raises(ProtectedError):
        department.delete()
    with pytest.raises(ProtectedError):
        root.delete()
    with pytest.raises(ProtectedError):
        company.delete()

    audits = list(
        AuditLog.objects.filter(
            company=company,
            object_type="Department",
            object_id=str(department.pk),
        ).order_by("created_at")
    )
    assert [audit.action for audit in audits] == ["create", "update", "update"]
    assert audits[1].old_data_json["name"] == "原名称"
    assert audits[1].new_data_json["name"] == "新名称"
    assert audits[2].old_data_json["is_active"] is True
    assert audits[2].new_data_json["is_active"] is False
    assert all(audit.user_id == admin.pk for audit in audits)


def test_user_department_scope_active_uniqueness_and_revoked_history():
    company = make_company()
    admin = make_user("admin", "system_admin")
    target = make_user("target")
    department = make_department(company, "D1")
    scope = assign_department_scope(
        actor=admin,
        company=company,
        user=target,
        department=department,
        reason="首次授权",
    )

    with pytest.raises(ValidationError, match="已有此部门"):
        assign_department_scope(
            actor=admin,
            company=company,
            user=target,
            department=department,
            reason="重复授权",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        UserDepartmentScope.objects.create(
            company=company,
            user=target,
            department=department,
            assigned_by=admin,
        )

    revoke_department_scope(actor=admin, scope=scope, reason="调整授权")
    scope.refresh_from_db()
    assert not scope.is_active
    assert scope.revoked_by_id == admin.pk
    assert scope.revoked_at is not None

    replacement = assign_department_scope(
        actor=admin,
        company=company,
        user=target,
        department=department,
        include_descendants=False,
        reason="重新授权",
    )
    assert replacement.pk != scope.pk
    assert UserDepartmentScope.objects.filter(
        company=company,
        user=target,
        department=department,
    ).count() == 2
    assert UserDepartmentScope.objects.filter(
        company=company,
        user=target,
        department=department,
        is_active=True,
    ).count() == 1

    actions = list(
        AuditLog.objects.filter(
            company=company,
            object_type="UserDepartmentScope",
            object_id=str(scope.pk),
        ).values_list("action", flat=True)
    )
    assert set(actions) == {"scope_assign", "scope_revoke"}


def test_high_risk_role_changes_require_reason_password_and_safe_audit():
    company = make_company()
    admin = make_user("admin", "system_admin")
    target = make_user("target", "employee")

    with pytest.raises(ValidationError, match="原因不能为空"):
        set_user_roles(
            actor=admin,
            company=company,
            user=target,
            roles=["finance"],
            reason=" ",
            current_password=PASSWORD,
        )
    with pytest.raises(ValidationError, match="当前密码验证失败"):
        set_user_roles(
            actor=admin,
            company=company,
            user=target,
            roles=["finance"],
            reason="分配财务职责",
            current_password="Wrong-Password-2026!",
        )
    assert target.groups.filter(name="employee").exists()
    assert not target.groups.filter(name="finance").exists()

    set_user_roles(
        actor=admin,
        company=company,
        user=target,
        roles=["finance"],
        reason="分配财务职责",
        current_password=PASSWORD,
    )
    assert set(target.groups.values_list("name", flat=True)) == {"finance"}
    audit = AuditLog.objects.get(
        company=company,
        action="roles_update",
        object_type="User",
        object_id=str(target.pk),
    )
    assert audit.old_data_json == {"roles": ["employee"]}
    assert audit.new_data_json == {
        "roles": ["finance"],
        "reason": "分配财务职责",
    }
    serialized_audit = json.dumps(
        [audit.old_data_json, audit.new_data_json], ensure_ascii=False
    )
    assert "current_password" not in serialized_audit
    assert PASSWORD not in serialized_audit


def test_last_login_capable_system_admin_and_finance_are_protected():
    company = make_company()
    admin = make_user("admin", "system_admin")
    finance = make_user("finance", "finance")

    with pytest.raises(ValidationError, match="最后一名可登录.*system_admin"):
        set_user_roles(
            actor=admin,
            company=company,
            user=admin,
            roles=[],
            reason="移除系统管理员",
            current_password=PASSWORD,
        )
    with pytest.raises(ValidationError, match="最后一名可登录.*finance"):
        set_user_roles(
            actor=admin,
            company=company,
            user=finance,
            roles=[],
            reason="移除财务角色",
            current_password=PASSWORD,
        )

    assert admin.groups.filter(name="system_admin").exists()
    assert finance.groups.filter(name="finance").exists()
    assert not AuditLog.objects.filter(company=company, action="roles_update").exists()


def test_scope_without_role_is_denied_and_manager_http_revocation_is_immediate(
    client,
):
    company = make_company()
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    scoped_without_role = make_user("scope-only")
    manager = make_user("manager", "department_manager")
    root = create_department(
        actor=admin,
        company=company,
        data={"code": "ROOT", "name": "授权根"},
    )
    child = create_department(
        actor=admin,
        company=company,
        data={"code": "CHILD", "name": "授权下级", "parent": root},
    )
    outside = create_department(
        actor=admin,
        company=company,
        data={"code": "OUT", "name": "范围外"},
    )
    inside_employee = create_employee(
        actor=hr,
        company=company,
        data={"employee_no": "IN", "name": "范围内员工", "department": child},
    )
    outside_employee = create_employee(
        actor=hr,
        company=company,
        data={"employee_no": "OUT", "name": "范围外员工", "department": outside},
    )
    assign_department_scope(
        actor=admin,
        company=company,
        user=scoped_without_role,
        department=root,
        reason="仅授予数据范围",
    )
    manager_scope = assign_department_scope(
        actor=admin,
        company=company,
        user=manager,
        department=root,
        reason="授予部门经理范围",
    )

    client.force_login(scoped_without_role)
    response = client.get(
        reverse("masterdata:employee-detail", args=[inside_employee.pk])
    )
    assert response.status_code == 403

    client.force_login(manager)
    department_list = client.get(reverse("masterdata:department-list"))
    assert department_list.status_code == 200
    visible_department_ids = {
        row["object"].pk for row in department_list.context["rows"]
    }
    assert visible_department_ids == {root.pk, child.pk}
    assert client.get(
        reverse("masterdata:department-detail", args=[child.pk])
    ).status_code == 200
    outside_department_response = client.get(
        reverse("masterdata:department-detail", args=[outside.pk])
    )
    assert outside_department_response.status_code in {403, 404}
    assert "范围外" not in outside_department_response.content.decode()
    assert client.post(
        reverse("masterdata:department-edit", args=[child.pk]),
        {
            "code": "CHILD-HACK",
            "name": "范围内也不允许维护部门主数据",
            "parent": root.pk,
            "manager_employee": "",
        },
    ).status_code == 403
    assert client.get(
        reverse("masterdata:employee-detail", args=[inside_employee.pk])
    ).status_code == 200
    outside_response = client.get(
        reverse("masterdata:employee-detail", args=[outside_employee.pk])
    )
    assert outside_response.status_code in {403, 404}
    assert "范围外员工" not in outside_response.content.decode()

    revoke_department_scope(
        actor=admin,
        scope=manager_scope,
        reason="立即撤销管理范围",
    )
    revoked_response = client.get(
        reverse("masterdata:employee-detail", args=[inside_employee.pk])
    )
    assert revoked_response.status_code in {403, 404}
    assert "范围内员工" not in revoked_response.content.decode()
    revoked_department_response = client.get(
        reverse("masterdata:department-detail", args=[child.pk])
    )
    assert revoked_department_response.status_code in {403, 404}
    assert "授权下级" not in revoked_department_response.content.decode()
    department_list = client.get(reverse("masterdata:department-list"))
    assert department_list.status_code == 200
    assert department_list.context["rows"] == []
    manager_scope.refresh_from_db()
    assert not manager_scope.is_active
    assert AuditLog.objects.filter(
        company=company,
        action="scope_revoke",
        object_id=str(manager_scope.pk),
    ).exists()


def test_system_admin_cannot_write_hr_data_but_hr_http_and_service_can(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    department = create_department(
        actor=admin,
        company=company,
        data={"code": "D1", "name": "部门"},
    )
    employee_data = {
        "employee_no": "E1",
        "name": "HR 创建员工",
        "department": department,
        "employment_status": "active",
        "is_active": True,
    }

    with pytest.raises(PermissionDenied):
        create_employee(actor=admin, company=company, data=employee_data)

    client.force_login(admin)
    response = client.post(
        reverse("masterdata:employee-create"),
        {
            "employee_no": "E1",
            "name": "管理员越权创建",
            "department": department.pk,
            "employment_status": "active",
            "hire_date": "",
            "termination_date": "",
            "mobile": "",
            "remark": "",
        },
    )
    assert response.status_code == 403
    assert not Employee.objects.filter(company=company, employee_no="E1").exists()

    client.force_login(hr)
    response = client.post(
        reverse("masterdata:employee-create"),
        {
            "employee_no": "E1",
            "name": "HR 创建员工",
            "department": department.pk,
            "employment_status": "active",
            "hire_date": "",
            "termination_date": "",
            "mobile": "",
            "remark": "",
        },
    )
    assert response.status_code == 302, (
        response.context["form"].errors.as_json() if response.context else ""
    )
    employee = Employee.objects.get(company=company, normalized_employee_no="e1")
    assert employee.name == "HR 创建员工"

    client.force_login(admin)
    response = client.post(
        reverse("masterdata:employee-edit", args=[employee.pk]),
        {
            "employee_no": "E1",
            "name": "管理员篡改姓名",
            "department": department.pk,
            "employment_status": "active",
            "hire_date": "",
            "termination_date": "",
            "mobile": "",
            "remark": "",
        },
    )
    assert response.status_code == 403
    employee.refresh_from_db()
    assert employee.name == "HR 创建员工"

    linked_account = make_user("employee-login", "employee")
    response = client.post(
        reverse("masterdata:employee-user-link", args=[employee.pk]),
        {"user": linked_account.pk},
    )
    assert response.status_code == 302
    employee.refresh_from_db()
    assert employee.user_id == linked_account.pk
    assert employee.employment_status == "active"
    assert employee.is_active
    assert AuditLog.objects.filter(
        company=company,
        action="create",
        object_type="Employee",
        object_id=str(employee.pk),
        user=hr,
    ).exists()


def test_setup_progress_recomputes_real_data_persists_and_blocks_unscoped_manager(
    client,
):
    company = make_company()
    admin = make_user("admin", "system_admin")
    finance = make_user("finance", "finance")
    hr = make_user("hr", "hr")
    manager = make_user("manager", "department_manager")
    department = create_department(
        actor=admin,
        company=company,
        data={"code": "D1", "name": "部门"},
    )
    create_employee(
        actor=hr,
        company=company,
        data={"employee_no": "E1", "name": "员工", "department": department},
    )
    create_asset_category(
        actor=admin,
        company=company,
        data={"code": "EQ", "name": "设备", "category_type": "equipment"},
    )
    create_location(
        actor=admin,
        company=company,
        data={"code": "P1", "name": "具体位置", "location_type": "position"},
    )

    setting = refresh_initialization_progress(company=company, actor=admin)
    assert setting.company_configured
    assert setting.departments_configured
    assert setting.employees_configured
    assert setting.categories_configured
    assert setting.locations_configured
    assert setting.users_configured
    assert not setting.permissions_configured
    assert not setting.coding_scheme_configured
    assert not setting.finance_rules_configured
    assert not setting.initialization_completed

    client.force_login(admin)
    response = client.get(reverse("setup"))
    assert response.status_code == 200
    assert manager in response.context["managers_without_scope"]
    step_eight = next(
        step for step in response.context["steps"] if step["number"] == 8
    )
    assert not step_eight["complete"]

    scope = assign_department_scope(
        actor=admin,
        company=company,
        user=manager,
        department=department,
        reason="完成部门经理范围",
    )
    setting.refresh_from_db()
    assert setting.permissions_configured
    setting_pk = setting.pk

    assert client.post(reverse("logout")).status_code == 302
    client.force_login(admin)
    response = client.get(reverse("setup"))
    assert response.status_code == 200
    assert response.context["setting"].pk == setting_pk
    assert response.context["progress"]["users_configured"]
    assert response.context["progress"]["permissions_configured"]
    assert not response.context["setting"].initialization_completed

    revoke_department_scope(actor=admin, scope=scope, reason="撤销后重算")
    setting.refresh_from_db()
    assert not setting.permissions_configured

    InitializationSetting.objects.filter(pk=setting.pk).update(
        users_configured=True,
        permissions_configured=True,
    )
    finance.is_active = False
    finance.save(update_fields=["is_active"])
    setting = refresh_initialization_progress(company=company, actor=admin)
    assert not setting.users_configured
    assert not setting.permissions_configured
    assert not setting.initialization_completed
    response = client.get(reverse("setup"))
    assert response.status_code == 200
    assert manager in response.context["managers_without_scope"]


def test_setup_counts_application_users_not_recovery_superuser():
    company = make_company()
    recovery = get_user_model().objects.create_superuser(
        username="recovery-root",
        password=PASSWORD,
        display_name="恢复管理员",
    )
    recovery.groups.set(
        Group.objects.filter(name__in=("system_admin", "finance"))
    )
    ordinary_admin = make_user("ordinary-admin", "system_admin")

    setting = refresh_initialization_progress(
        company=company,
        actor=ordinary_admin,
    )

    assert not setting.users_configured
    finance = make_user("application-finance", "finance")
    setting = refresh_initialization_progress(
        company=company,
        actor=ordinary_admin,
    )
    assert setting.users_configured
    assert finance.is_active


def test_user_permission_urls_reject_recovery_superuser_target(client):
    make_company()
    admin = make_user("admin", "system_admin")
    recovery = get_user_model().objects.create_superuser(
        username="recovery-root",
        password=PASSWORD,
        display_name="恢复管理员",
    )
    department = make_department(Company.objects.get(is_active=True), "D1")
    client.force_login(admin)

    assert client.get(
        reverse("masterdata:user-permissions-detail", args=[recovery.pk])
    ).status_code == 404
    assert client.post(
        reverse("masterdata:user-roles-update", args=[recovery.pk]),
        {
            "roles": ["finance"],
            "reason": "不允许绕过恢复账号边界",
            "current_password": PASSWORD,
        },
    ).status_code == 404
    assert client.post(
        reverse("masterdata:user-scope-assign", args=[recovery.pk]),
        {
            "department": department.pk,
            "include_descendants": "on",
            "reason": "不允许给恢复账号授权",
        },
    ).status_code == 404
    recovery.refresh_from_db()
    assert not recovery.groups.filter(name="finance").exists()
    assert not UserDepartmentScope.objects.filter(user=recovery).exists()


def test_system_setting_registry_types_sprint_boundary_and_audits_are_exact():
    company = make_company()
    admin = make_user("admin", "system_admin")
    finance = make_user("finance", "finance")
    assert {
        key: entry["value_type"] for key, entry in SYSTEM_SETTING_REGISTRY.items()
    } == {
        "attachment_allowed_extensions": "string_list",
        "attachment_max_size_bytes": "integer",
        "fixed_asset_warning_amount": "decimal",
    }
    assert SystemSetting.REGISTRY_TYPES == {
        "attachment_allowed_extensions": "string_list",
        "attachment_max_size_bytes": "integer",
        "fixed_asset_warning_amount": "decimal",
    }
    assert get_system_setting(
        company=company, key="fixed_asset_warning_amount"
    ) == Decimal("5000.00")
    for invalid_decimal in ("NaN", "Infinity", "-Infinity", "-0.01"):
        with pytest.raises(ValidationError, match="有限"):
            _serialize_setting("fixed_asset_warning_amount", invalid_decimal)

    extensions = set_system_setting(
        actor=admin,
        company=company,
        key="attachment_allowed_extensions",
        value=[".PDF", "jpg", "pdf"],
        value_type="string_list",
    )
    maximum = set_system_setting(
        actor=admin,
        company=company,
        key="attachment_max_size_bytes",
        value="1024",
        value_type="integer",
    )
    assert extensions.value == '["pdf","jpg"]'
    assert maximum.value == "1024"
    assert get_system_setting(
        company=company, key="attachment_allowed_extensions"
    ) == ["pdf", "jpg"]
    assert get_system_setting(
        company=company, key="attachment_max_size_bytes"
    ) == 1024

    with pytest.raises(PermissionDenied):
        set_system_setting(
            actor=finance,
            company=company,
            key="attachment_max_size_bytes",
            value="2048",
        )
    for actor in (admin, finance):
        with pytest.raises(PermissionDenied, match="Sprint 4"):
            set_system_setting(
                actor=actor,
                company=company,
                key="fixed_asset_warning_amount",
                value="5000.00",
                value_type="decimal",
            )
    for forbidden_key in (
        "secret_key",
        "currency",
        "business_timezone",
        "default_salvage_rate",
    ):
        with pytest.raises(ValidationError, match="未知"):
            set_system_setting(
                actor=admin,
                company=company,
                key=forbidden_key,
                value="do-not-store",
            )
    with pytest.raises(ValidationError, match="value_type"):
        set_system_setting(
            actor=admin,
            company=company,
            key="attachment_max_size_bytes",
            value="1024",
            value_type="decimal",
        )
    for invalid_size in (0, 20 * 1024 * 1024 + 1, "not-an-integer"):
        with pytest.raises(ValidationError):
            set_system_setting(
                actor=admin,
                company=company,
                key="attachment_max_size_bytes",
                value=invalid_size,
            )
    for invalid_extensions in ([], ["svg"], ["xlsx", "exe"]):
        with pytest.raises(ValidationError):
            set_system_setting(
                actor=admin,
                company=company,
                key="attachment_allowed_extensions",
                value=invalid_extensions,
            )

    with pytest.raises(IntegrityError), transaction.atomic():
        SystemSetting.objects.create(
            company=company,
            key="attachment_max_size_bytes",
            value="2048",
            value_type="integer",
            description="重复真源",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SystemSetting.objects.create(
            company=company,
            key="secret_key",
            value="secret",
            value_type="integer",
            description="禁止的密钥",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SystemSetting.objects.create(
            company=company,
            key="attachment_allowed_extensions",
            value='["jpg"]',
            value_type="integer",
            description="错误类型",
        )

    assert SystemSetting.objects.filter(company=company).count() == 2
    audits = AuditLog.objects.filter(
        company=company,
        object_type="SystemSetting",
        action="create",
    )
    assert audits.count() == 2
    serialized_audits = json.dumps(
        [audit.new_data_json for audit in audits], ensure_ascii=False
    )
    assert "secret" not in serialized_audits.lower()


def test_system_setting_http_exposes_only_two_sprint1_attachment_fields():
    company = make_company()
    admin = make_user("settings-admin", "system_admin")
    finance = make_user("settings-finance", "finance")

    admin_client = Client()
    admin_client.force_login(admin)
    response = admin_client.get(reverse("masterdata:system-settings"))
    assert response.status_code == 200
    assert list(response.context["form"].fields) == [
        "attachment_allowed_extensions",
        "attachment_max_size_bytes",
    ]
    content = response.content.decode()
    assert "fixed_asset_warning_amount" not in content
    assert "secret_key" not in content

    finance_client = Client()
    finance_client.force_login(finance)
    assert finance_client.get(reverse("masterdata:system-settings")).status_code == 200
    assert finance_client.post(
        reverse("masterdata:system-settings"),
        {
            "attachment_allowed_extensions": ["xlsx"],
            "attachment_max_size_bytes": 1024,
        },
    ).status_code == 403
    assert not SystemSetting.objects.filter(company=company).exists()
