from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.models import (
    AssetCategory,
    Company,
    Department,
    Employee,
    InitializationSetting,
    Location,
    UserDepartmentScope,
)
from apps.masterdata.permissions import (
    assigned_role_names_for,
    role_names_for,
    scoped_departments,
    scoped_employees,
)
from apps.masterdata.services import (
    assign_department_scope,
    compute_initialization_progress,
    create_asset_category,
    create_department,
    create_employee,
    create_location,
    link_employee_user,
    refresh_initialization_progress,
    revoke_department_scope,
    set_user_roles,
    update_company,
    update_department,
)


pytestmark = pytest.mark.django_db
PASSWORD = "Valid-Password-2026!"
NEW_USER_PASSWORD = "Long-Random-7p!vQ2zN-2026"


def make_user(username, *roles):
    user = get_user_model().objects.create_user(
        username=username,
        password=PASSWORD,
        display_name=username,
    )
    user.groups.set(Group.objects.filter(name__in=roles))
    return user


def make_company(code="C1", *, active=True):
    return Company.objects.create(
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 公司",
        short_name=code,
        is_active=active,
    )


def test_setup_progress_steps_1_to_5_and_8_persist_but_never_complete():
    company = make_company()
    admin = make_user("admin", "system_admin")
    finance = make_user("finance", "finance")
    hr = make_user("hr", "hr")
    manager_user = make_user("manager", "department_manager")

    root = create_department(
        actor=admin, company=company, data={"code": "ROOT", "name": "根部门"}
    )
    create_employee(
        actor=hr,
        company=company,
        data={
            "employee_no": "E1",
            "name": "员工",
            "department": root,
            "employment_status": "active",
            "is_active": True,
        },
    )
    create_asset_category(
        actor=admin,
        company=company,
        data={
            "code": "EQ",
            "name": "设备",
            "category_type": "equipment",
        },
    )
    create_location(
        actor=admin,
        company=company,
        data={
            "code": "POS",
            "name": "位置",
            "location_type": "position",
        },
    )
    assign_department_scope(
        actor=admin,
        company=company,
        user=manager_user,
        department=root,
        reason="初始化部门经理范围",
    )

    progress = compute_initialization_progress(company)
    assert all(
        progress[key]
        for key in (
            "company_configured",
            "departments_configured",
            "employees_configured",
            "categories_configured",
            "locations_configured",
            "users_configured",
            "permissions_configured",
        )
    )
    refresh_initialization_progress(company=company, actor=admin)
    setting = InitializationSetting.objects.get(company=company)
    setting.refresh_from_db()
    assert setting.company_configured
    assert setting.departments_configured
    assert setting.employees_configured
    assert setting.categories_configured
    assert setting.locations_configured
    assert setting.users_configured
    assert setting.permissions_configured
    assert not setting.coding_scheme_configured
    assert not setting.finance_rules_configured
    assert not setting.initialization_completed
    assert finance.is_active


def test_setup_and_mutating_urls_reject_ordinary_user_and_get_status_change(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    ordinary = make_user("ordinary", "employee")
    department = create_department(
        actor=admin, company=company, data={"code": "D1", "name": "部门"}
    )
    client.force_login(ordinary)

    assert client.get("/setup/").status_code == 403
    assert client.get(reverse("masterdata:department-create")).status_code == 403
    assert client.post(
        reverse("masterdata:department-edit", args=[department.pk]),
        {"code": "HACK", "name": "篡改", "parent": "", "manager_employee": ""},
    ).status_code == 403
    assert client.post(
        reverse("imports:confirm", args=[999999]), {"confirm": "1"}
    ).status_code in {
        403,
        404,
    }

    client.force_login(admin)
    assert client.get(
        reverse("masterdata:department-status", args=[department.pk])
    ).status_code == 405


def test_setup_step_eight_explains_missing_finance_and_bootstrap_path(client):
    make_company()
    admin = make_user("admin", "system_admin")
    client.force_login(admin)

    response = client.get(reverse("masterdata:setup-step", args=[8]))

    assert response.status_code == 200
    content = response.content.decode()
    assert "缺少至少一名可登录的" in content
    assert "finance" in content
    assert "bootstrap_user" in content


def test_inactive_user_roles_remain_visible_for_configuration(client):
    make_company()
    admin = make_user("admin", "system_admin")
    target = make_user("inactive-finance", "finance", "employee")
    target.is_active = False
    target.save(update_fields=["is_active"])

    assert role_names_for(target) == set()
    assert assigned_role_names_for(target) == {"finance", "employee"}
    client.force_login(admin)
    response = client.get(
        reverse("masterdata:user-permissions-detail", args=[target.pk])
    )

    assert response.status_code == 200
    assert set(response.context["role_form"].initial["roles"]) == {
        "finance",
        "employee",
    }


def test_system_admin_can_create_login_capable_application_user_from_ui(client):
    make_company()
    admin = make_user("admin", "system_admin")
    equipment = make_user("equipment", "equipment")
    create_url = reverse("masterdata:user-create")

    client.force_login(equipment)
    assert client.get(create_url).status_code == 403
    assert client.post(create_url, {}).status_code == 403

    client.force_login(admin)
    listing = client.get(reverse("masterdata:user-permissions-list"))
    assert create_url in listing.content.decode()
    page = client.get(create_url)
    assert page.status_code == 200
    assert "新增应用用户" in page.content.decode()
    assert 'id="id_roles" class="form-check-input"' not in page.content.decode()
    assert page.content.decode().count('name="roles"') == 8

    response = client.post(
        create_url,
        {
            "username": "web-created-user",
            "display_name": "网页创建用户",
            "email": "web-user@example.test",
            "mobile": "13800000002",
            "roles": ["equipment", "warehouse"],
            "password": NEW_USER_PASSWORD,
            "password_confirm": NEW_USER_PASSWORD,
            "reason": "建立资产与仓库协同账号",
            "current_password": PASSWORD,
            "is_staff": "on",
            "is_superuser": "on",
        },
    )

    assert response.status_code == 302
    target = get_user_model().objects.get(username="web-created-user")
    assert response.url == reverse(
        "masterdata:user-permissions-detail", args=[target.pk]
    )
    assert target.check_password(NEW_USER_PASSWORD)
    assert not target.is_staff and not target.is_superuser
    assert set(target.groups.values_list("name", flat=True)) == {
        "equipment",
        "warehouse",
    }
    assert AuditLog.objects.filter(
        action="user_create", object_id=str(target.pk), user=admin
    ).exists()

    client.logout()
    login = client.post(
        reverse("login"),
        {"username": target.username, "password": NEW_USER_PASSWORD},
    )
    assert login.status_code == 302


def test_department_manager_scope_limits_lists_and_revocation_is_immediate():
    company = make_company()
    admin = make_user("admin", "system_admin")
    manager = make_user("manager", "department_manager")
    root = create_department(
        actor=admin, company=company, data={"code": "ROOT", "name": "授权根"}
    )
    child = create_department(
        actor=admin,
        company=company,
        data={"code": "CHILD", "name": "授权下级", "parent": root},
    )
    outside = create_department(
        actor=admin, company=company, data={"code": "OUT", "name": "范围外"}
    )
    hr = make_user("hr", "hr")
    inside_employee = create_employee(
        actor=hr,
        company=company,
        data={"employee_no": "IN", "name": "范围内", "department": child},
    )
    create_employee(
        actor=hr,
        company=company,
        data={"employee_no": "OUT", "name": "范围外", "department": outside},
    )
    scope = assign_department_scope(
        actor=admin,
        company=company,
        user=manager,
        department=root,
        reason="授权管理范围",
    )

    assert set(scoped_departments(manager, company).values_list("pk", flat=True)) == {
        root.pk,
        child.pk,
    }
    assert list(scoped_employees(manager, company)) == [inside_employee]
    revoke_department_scope(actor=admin, scope=scope, reason="撤销管理范围")
    assert not scoped_departments(manager, company).exists()
    assert not scoped_employees(manager, company).exists()
    assert AuditLog.objects.filter(
        company=company, action="scope_revoke", object_id=str(scope.pk)
    ).exists()


def test_department_manager_department_pages_are_read_only_and_scope_limited(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    manager = make_user("manager", "department_manager")
    root = create_department(
        actor=admin, company=company, data={"code": "ROOT", "name": "授权根"}
    )
    child = create_department(
        actor=admin,
        company=company,
        data={"code": "CHILD", "name": "授权下级", "parent": root},
    )
    outside = create_department(
        actor=admin, company=company, data={"code": "OUT", "name": "范围外"}
    )
    scope = assign_department_scope(
        actor=admin,
        company=company,
        user=manager,
        department=root,
        reason="授权部门只读范围",
    )
    client.force_login(manager)

    response = client.get(reverse("masterdata:department-list"))
    assert response.status_code == 200
    assert {row["object"].pk for row in response.context["rows"]} == {
        root.pk,
        child.pk,
    }
    assert client.get(
        reverse("masterdata:department-detail", args=[child.pk])
    ).status_code == 200
    assert client.get(
        reverse("masterdata:department-detail", args=[outside.pk])
    ).status_code == 404
    assert client.get(
        reverse("masterdata:department-edit", args=[child.pk])
    ).status_code == 403

    revoke_department_scope(actor=admin, scope=scope, reason="立即撤销只读范围")
    assert not client.get(reverse("masterdata:department-list")).context["rows"]
    assert client.get(
        reverse("masterdata:department-detail", args=[child.pk])
    ).status_code == 404


def test_department_reparent_requires_impact_preview_and_explicit_confirmation(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    manager_a = make_user("manager-a", "department_manager")
    manager_b = make_user("manager-b", "department_manager")
    root_a = create_department(
        actor=admin, company=company, data={"code": "A", "name": "甲根部门"}
    )
    root_b = create_department(
        actor=admin, company=company, data={"code": "B", "name": "乙根部门"}
    )
    moving = create_department(
        actor=admin,
        company=company,
        data={"code": "MOVE", "name": "改挂部门", "parent": root_a},
    )
    leaf = create_department(
        actor=admin,
        company=company,
        data={"code": "LEAF", "name": "改挂下级", "parent": moving},
    )
    assign_department_scope(
        actor=admin,
        company=company,
        user=manager_a,
        department=root_a,
        reason="甲树授权",
    )
    assign_department_scope(
        actor=admin,
        company=company,
        user=manager_b,
        department=root_b,
        reason="乙树授权",
    )
    client.force_login(admin)
    url = reverse("masterdata:department-edit", args=[moving.pk])
    payload = {
        "code": moving.code,
        "name": moving.name,
        "parent": root_b.pk,
        "manager_employee": "",
    }
    update_count = AuditLog.objects.filter(
        company=company, action="update", object_id=str(moving.pk)
    ).count()

    preview = client.post(url, payload)

    assert preview.status_code == 200
    content = preview.content.decode()
    assert "部门改挂影响摘要" in content
    assert "范围扩大" in content
    assert "范围缩小" in content
    assert "manager-a" in content
    assert "manager-b" in content
    moving.refresh_from_db()
    assert moving.parent_id == root_a.pk
    assert AuditLog.objects.filter(
        company=company, action="update", object_id=str(moving.pk)
    ).count() == update_count

    token = preview.context["department_scope_impact"]["confirmation_token"]
    saved = client.post(
        url,
        {
            **payload,
            "confirm_scope_impact": "1",
            "scope_impact_token": token,
        },
    )

    assert saved.status_code == 302
    moving.refresh_from_db()
    assert moving.parent_id == root_b.pk
    assert moving.pk not in set(
        scoped_departments(manager_a, company).values_list("pk", flat=True)
    )
    assert leaf.pk not in set(
        scoped_departments(manager_a, company).values_list("pk", flat=True)
    )
    assert {moving.pk, leaf.pk}.issubset(
        set(scoped_departments(manager_b, company).values_list("pk", flat=True))
    )
    audit = AuditLog.objects.filter(
        company=company, action="update", object_id=str(moving.pk)
    ).latest("created_at")
    assert audit.new_data_json["scope_impact"]


def test_setup_step_eight_uses_login_roles_and_active_scopes_as_real_blockers(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    unusable_finance = make_user("unusable-finance", "finance")
    unusable_finance.set_unusable_password()
    unusable_finance.save(update_fields=["password"])
    manager = make_user("manager", "department_manager")
    department = create_department(
        actor=admin, company=company, data={"code": "D1", "name": "部门"}
    )
    client.force_login(admin)
    url = reverse("masterdata:setup-step", args=[8])

    response = client.get(url)

    assert response.status_code == 200
    assert not response.context["progress"]["users_configured"]
    assert not response.context["progress"]["permissions_configured"]
    content = response.content.decode()
    assert "缺少至少一名可登录的" in content
    assert "finance" in content
    assert "部门经理账号 manager 尚无启用部门范围" in content
    assert "bootstrap_user" in content

    saved = client.post(url)
    assert saved.status_code == 302
    setting = InitializationSetting.objects.get(company=company)
    assert not setting.users_configured
    assert not setting.permissions_configured

    unusable_finance.set_password(PASSWORD)
    unusable_finance.save(update_fields=["password"])
    assign_department_scope(
        actor=admin,
        company=company,
        user=manager,
        department=department,
        reason="补齐部门经理范围",
    )
    assert client.post(url).status_code == 302
    setting.refresh_from_db()
    assert setting.users_configured
    assert setting.permissions_configured


def test_setup_step_eight_never_counts_superuser_as_application_admin(client):
    make_company()
    recovery = get_user_model().objects.create_superuser(
        username="recovery",
        password=PASSWORD,
        display_name="恢复账号",
    )
    recovery.groups.add(Group.objects.get(name="system_admin"))
    client.force_login(recovery)

    response = client.get(reverse("masterdata:setup-step", args=[8]))

    # Recovery access bypass is intentionally not an application-role path.
    assert response.status_code == 403


def test_cross_company_post_ids_and_invalid_manager_are_rejected(client):
    c1 = make_company("C1")
    c2 = make_company("C2", active=False)
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    d1 = create_department(
        actor=admin, company=c1, data={"code": "D1", "name": "本公司"}
    )
    d2 = Department.objects.create(
        company=c2, code="D2", normalized_code="d2", name="其他公司"
    )
    inactive_manager = create_employee(
        actor=hr,
        company=c1,
        data={"employee_no": "M1", "name": "停用经理", "department": d1},
    )
    inactive_manager.is_active = False
    inactive_manager.save(update_fields=["is_active"])

    with pytest.raises(ValidationError):
        create_department(
            actor=admin,
            company=c1,
            data={"code": "BAD", "name": "非法经理", "manager_employee": inactive_manager},
        )

    client.force_login(hr)
    response = client.post(
        reverse("masterdata:employee-create"),
        {
            "employee_no": "X1",
            "name": "跨公司篡改",
            "department": d2.pk,
            "employment_status": "active",
            "hire_date": date(2026, 1, 1).isoformat(),
            "termination_date": "",
            "mobile": "",
            "remark": "",
        },
    )
    assert response.status_code == 200
    assert not Employee.objects.filter(company=c1, normalized_employee_no="x1").exists()


def test_employee_create_binds_current_company_before_model_validation(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    department = create_department(
        actor=admin, company=company, data={"code": "D1", "name": "本公司"}
    )
    client.force_login(hr)

    response = client.post(
        reverse("masterdata:employee-create"),
        {
            "employee_no": "E1",
            "name": "员工",
            "department": department.pk,
            "employment_status": "active",
            "hire_date": date(2026, 1, 1).isoformat(),
            "termination_date": "",
            "mobile": "",
            "remark": "",
        },
    )

    assert response.status_code == 302
    assert Employee.objects.filter(
        company=company, normalized_employee_no="e1"
    ).exists()


def test_superuser_cannot_be_targeted_by_permission_configuration_urls(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    recovery = get_user_model().objects.create_superuser(
        username="recovery",
        password=PASSWORD,
        display_name="恢复账号",
    )
    department = create_department(
        actor=admin, company=company, data={"code": "D1", "name": "部门"}
    )
    employee = Employee.objects.create(
        company=company,
        department=department,
        employee_no="E-RECOVERY",
        name="隔离验证员工",
    )
    with pytest.raises(PermissionDenied, match="recovery superuser"):
        assign_department_scope(
            actor=admin,
            company=company,
            user=recovery,
            department=department,
            reason="验证恢复账号隔离",
        )
    with pytest.raises(PermissionDenied, match="recovery superuser"):
        set_user_roles(
            actor=admin,
            company=company,
            user=recovery,
            roles={"system_admin"},
            reason="验证恢复账号隔离",
            current_password=PASSWORD,
        )
    with pytest.raises(PermissionDenied, match="recovery superuser"):
        link_employee_user(actor=admin, employee=employee, user=recovery)
    client.force_login(admin)

    assert client.get(
        reverse("masterdata:user-permissions-detail", args=[recovery.pk])
    ).status_code == 404
    assert client.post(
        reverse("masterdata:user-roles-update", args=[recovery.pk]), {}
    ).status_code == 404
    assert client.post(
        reverse("masterdata:user-scope-assign", args=[recovery.pk]),
        {},
    ).status_code == 404
    assert not UserDepartmentScope.objects.filter(user=recovery).exists()
    assert not recovery.groups.exists()
    assert role_names_for(recovery) == set()
    employee.refresh_from_db()
    assert employee.user_id is None


def test_company_urls_never_expose_or_mutate_noncurrent_inactive_company(client):
    current = make_company("CURRENT")
    legacy = make_company("LEGACY", active=False)
    admin = make_user("admin", "system_admin")
    client.force_login(admin)

    list_response = client.get(reverse("masterdata:company-list"))
    assert list_response.status_code == 200
    assert list(list_response.context["objects"]) == [current]
    assert client.get(
        reverse("masterdata:company-detail", args=[legacy.pk])
    ).status_code == 404
    assert client.post(
        reverse("masterdata:company-edit", args=[legacy.pk]),
        {
            "code": "HACK",
            "name": "越权公司",
            "short_name": "越权",
            "currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
    ).status_code == 404
    assert client.post(
        reverse("masterdata:company-status", args=[legacy.pk]),
        {"confirm": "on"},
    ).status_code == 404
    legacy.refresh_from_db()
    assert legacy.code == "LEGACY"
    assert not legacy.is_active


def test_services_reject_noncurrent_company_and_department():
    current = make_company("CURRENT")
    legacy = make_company("LEGACY", active=False)
    admin = make_user("admin", "system_admin")
    legacy_department = Department.objects.create(
        company=legacy,
        code="OLD",
        name="旧部门",
    )

    with pytest.raises(PermissionDenied, match="当前公司"):
        update_company(
            actor=admin,
            company=legacy,
            data={"name": "越权公司"},
        )
    with pytest.raises(PermissionDenied, match="当前公司"):
        update_department(
            actor=admin,
            department=legacy_department,
            data={"name": "越权部门"},
        )

    legacy.refresh_from_db()
    legacy_department.refresh_from_db()
    assert legacy.name == "LEGACY 公司"
    assert legacy_department.name == "旧部门"
    assert current.is_active


def test_tree_reparent_recalculates_all_descendant_levels_and_audits():
    company = make_company()
    equipment = make_user("equipment", "equipment")
    root_a = create_location(
        actor=equipment,
        company=company,
        data={"code": "A", "name": "A", "location_type": "site"},
    )
    root_b = create_location(
        actor=equipment,
        company=company,
        data={"code": "B", "name": "B", "location_type": "site"},
    )
    child = create_location(
        actor=equipment,
        company=company,
        data={"code": "C", "name": "C", "location_type": "workshop", "parent": root_a},
    )
    leaf = create_location(
        actor=equipment,
        company=company,
        data={"code": "L", "name": "L", "location_type": "position", "parent": child},
    )

    from apps.masterdata.services import update_location

    update_location(actor=equipment, location=child, data={"parent": root_b})
    child.refresh_from_db()
    leaf.refresh_from_db()
    assert child.level == 2
    assert leaf.level == 3
    assert AuditLog.objects.filter(
        company=company, action="update", object_id=str(child.pk)
    ).exists()


def test_asset_category_is_physical_only_and_has_no_finance_fields():
    field_names = {field.name for field in AssetCategory._meta.get_fields()}
    assert "category_type" in field_names
    assert "fixed_asset_category" not in field_names
    assert "depreciation_policy" not in field_names
    assert "coding_scheme" not in field_names
