"""Controlled Sprint 1 master-data services.

Views, imports and setup pages must call these functions instead of updating
models directly.  Each mutating function re-checks authorization and company
boundaries, runs in a transaction and appends its audit event in that same
transaction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.roles import ROLE_NAMES, ensure_fixed_roles
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import (
    current_company,
    is_login_capable,
    require_application_user_target,
    require_manage_masterdata,
    require_roles,
    resolve_department_ids,
)


SYSTEM_SETTING_REGISTRY = {
    "attachment_allowed_extensions": {
        "value_type": "string_list",
        "writer": "system_admin",
        "default": ["jpg", "jpeg", "png", "webp", "pdf", "xlsx", "docx"],
        "description": "允许上传的附件扩展名",
    },
    "attachment_max_size_bytes": {
        "value_type": "integer",
        "writer": "system_admin",
        "default": 20 * 1024 * 1024,
        "description": "单个附件最大字节数",
    },
    "fixed_asset_warning_amount": {
        "value_type": "decimal",
        "writer": "finance",
        "default": Decimal("5000.00"),
        "description": "固定资产认定提示金额",
        "available_from_sprint": 4,
    },
}

SAFE_ATTACHMENT_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "webp", "pdf", "xlsx", "docx"}
)

CURRENT_LOCATION_LEAF_ASSET_STATUSES = frozenset(
    {
        "pending_finance",
        "pending_label",
        "in_use",
        "idle",
        "loaned",
        "under_repair",
        "pending_disposal",
    }
)


def _snapshot(instance, fields) -> dict:
    data = {}
    for field in fields:
        value = getattr(instance, field)
        if hasattr(value, "pk"):
            value = value.pk
        data[field] = value
    return data


def _audit(
    *,
    company,
    actor,
    action,
    instance,
    old_data=None,
    new_data=None,
    request=None,
):
    return write_business_audit_log(
        company=company,
        user=actor,
        action=action,
        object_type=instance._meta.object_name,
        object_id=instance.pk,
        old_data=old_data or {},
        new_data=new_data or {},
        **request_audit_context(request),
    )


def _save(instance):
    instance.full_clean()
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError as exc:
        raise ValidationError(
            "保存失败：代码、编号或活动记录与现有数据重复。"
        ) from exc
    return instance


def _validate_same_company(company, **objects):
    for label, value in objects.items():
        if value is not None and getattr(value, "company_id", None) != company.pk:
            raise ValidationError({label: "所选记录不属于当前公司。"})


def _require_current_company(company, *, include_inactive=False):
    """Enforce the single-company V1 boundary at every Service entry."""
    current = current_company(include_inactive=include_inactive)
    if (
        company is None
        or current is None
        or getattr(company, "pk", None) != current.pk
    ):
        raise PermissionDenied("目标记录不属于当前公司。")
    return current


def _advisory_xact_lock(key):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key]
            )


def _lock_tree_write(model, *company_ids):
    table = model._meta.db_table
    _advisory_xact_lock(f"masterdata:tree:{table}:write")
    for company_id in sorted({value for value in company_ids if value is not None}):
        _advisory_xact_lock(f"masterdata:tree:{table}:company:{company_id}")


def _lock_manager_write(*company_ids):
    _advisory_xact_lock("masterdata:manager:write")
    for company_id in sorted({value for value in company_ids if value is not None}):
        _advisory_xact_lock(f"masterdata:manager:company:{company_id}")


def _lock_manager_rows(*, employee_ids=(), department_ids=()):
    from apps.masterdata.models import Department, Employee

    employee_ids = sorted({value for value in employee_ids if value is not None})
    employees = {
        employee.pk: employee
        for employee in Employee.objects.select_for_update()
        .filter(pk__in=employee_ids)
        .order_by("pk")
    }
    department_ids = sorted(
        {value for value in department_ids if value is not None}
        | {employee.department_id for employee in employees.values()}
    )
    departments = {
        department.pk: department
        for department in Department.objects.select_for_update()
        .filter(pk__in=department_ids)
        .order_by("pk")
    }
    return employees, departments


def _validate_department_manager(company, employee):
    if employee is None:
        return
    _validate_same_company(company, manager_employee=employee)
    if employee.employment_status != "active" or not employee.is_active:
        raise ValidationError(
            {"manager_employee": "新绑定经理必须是在职且启用的员工。"}
        )
    if not employee.department_id or not employee.department.is_active:
        raise ValidationError(
            {"manager_employee": "新绑定经理必须属于一个启用部门。"}
        )


def _apply(instance, data: Mapping, allowed_fields: Iterable[str]):
    for field in allowed_fields:
        if field in data:
            setattr(instance, field, data[field])
    return instance


def _validate_location_parent_is_available(model, parent):
    """Keep every current formal asset assigned to a leaf Location."""

    if parent is None or model._meta.label_lower != "masterdata.location":
        return
    from apps.assets.models import Asset

    if Asset._base_manager.filter(
        location_id=parent.pk,
        record_status="active",
        asset_status__in=CURRENT_LOCATION_LEAF_ASSET_STATUSES,
    ).exists():
        raise ValidationError(
            {"parent": "该位置仍是当前正式资产的位置，不能在其下新增节点。"}
        )


@transaction.atomic
def create_company(*, actor, data, request=None):
    from apps.masterdata.models import Company, InitializationSetting

    require_manage_masterdata(actor, "company")
    if Company.objects.select_for_update().exists():
        raise ValidationError("V1 只允许配置一个公司，不能创建第二个公司。")
    company = _apply(
        Company(),
        data,
        {"code", "name", "short_name", "currency", "timezone"},
    )
    company.is_active = True
    _save(company)
    setting = InitializationSetting.objects.create(company=company)
    _audit(
        company=company,
        actor=actor,
        action="create",
        instance=company,
        new_data=_snapshot(
            company,
            ("code", "name", "short_name", "currency", "timezone", "is_active"),
        ),
        request=request,
    )
    _audit(
        company=company,
        actor=actor,
        action="create",
        instance=setting,
        new_data={"initialization_completed": False},
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return company


@transaction.atomic
def update_company(*, actor, company, data, request=None):
    from apps.masterdata.models import Company

    require_manage_masterdata(actor, "company")
    _require_current_company(company, include_inactive=True)
    company = Company.objects.select_for_update().get(pk=company.pk)
    fields = ("code", "name", "short_name", "currency", "timezone", "is_active")
    old = _snapshot(company, fields)
    _apply(company, data, fields)
    _save(company)
    _audit(
        company=company,
        actor=actor,
        action="update",
        instance=company,
        old_data=old,
        new_data=_snapshot(company, fields),
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return company


def _scope_snapshots(company):
    from apps.masterdata.models import UserDepartmentScope

    user_ids = set(
        UserDepartmentScope.objects.filter(company=company, is_active=True).values_list(
            "user_id", flat=True
        )
    )
    User = get_user_model()
    return {
        str(user_id): sorted(
            str(value)
            for value in resolve_department_ids(
                user, company, require_action_role=False
            )
        )
        for user_id in user_ids
        if (user := User.objects.filter(pk=user_id).first()) is not None
    }


@transaction.atomic
def create_department(*, actor, company, data, request=None):
    from apps.masterdata.models import Department

    require_manage_masterdata(actor, "department")
    _require_current_company(company)
    _lock_manager_write(company.pk)
    _lock_tree_write(Department, company.pk)
    department = _apply(
        Department(company=company),
        data,
        {"code", "name", "parent", "manager_employee", "is_active"},
    )
    employees, departments = _lock_manager_rows(
        employee_ids=(department.manager_employee_id,),
        department_ids=(department.parent_id,),
    )
    if department.parent_id:
        department.parent = departments[department.parent_id]
    if department.manager_employee_id:
        department.manager_employee = employees[department.manager_employee_id]
    _validate_same_company(company, parent=department.parent)
    _validate_department_manager(company, department.manager_employee)
    _save(department)
    _audit(
        company=company,
        actor=actor,
        action="create",
        instance=department,
        new_data=_snapshot(
            department,
            ("code", "name", "parent", "manager_employee", "is_active"),
        ),
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return department


@transaction.atomic
def update_department(*, actor, department, data, request=None):
    from apps.masterdata.models import Department

    require_manage_masterdata(actor, "department")
    _require_current_company(department.company)
    _lock_manager_write(department.company_id)
    _lock_tree_write(Department, department.company_id)
    department = Department.objects.get(pk=department.pk)
    company = department.company
    _lock_manager_write(company.pk)
    _lock_tree_write(Department, company.pk)
    fields = ("code", "name", "parent", "manager_employee", "is_active")
    old = _snapshot(department, fields)
    scopes_before = _scope_snapshots(company)
    _apply(department, data, fields)
    employee_ids = {department.manager_employee_id}
    if old["is_active"] and not department.is_active:
        employee_ids.update(
            department.employees.values_list("pk", flat=True)
        )
    managed_department_ids = Department.objects.filter(
        manager_employee_id__in=employee_ids
    ).values_list("pk", flat=True)
    employees, departments = _lock_manager_rows(
        employee_ids=employee_ids,
        department_ids=(department.pk, department.parent_id, *managed_department_ids),
    )
    department = departments[department.pk]
    _apply(department, data, fields)
    if department.parent_id:
        department.parent = departments[department.parent_id]
    if department.manager_employee_id:
        department.manager_employee = employees[department.manager_employee_id]
    _validate_same_company(company, parent=department.parent)
    _validate_department_manager(company, department.manager_employee)
    # A deferred PostgreSQL trigger prevents a manager's home department from
    # becoming inactive while that employee is still referenced as any
    # department's manager.  Clear those references first, in the same atomic
    # operation, so the transition is valid and every unlink is audited.
    if old["is_active"] and not department.is_active:
        from apps.masterdata.models import Employee

        for employee in Employee.objects.filter(
            pk__in=employees, company=company, department=department
        ).order_by("pk"):
            _clear_manager_assignments(
                employee=employee, actor=actor, request=request
            )
    _save(department)
    scopes_after = _scope_snapshots(company)
    changed_scopes = {
        user_id: {
            "before": scopes_before.get(user_id, []),
            "after": scopes_after.get(user_id, []),
        }
        for user_id in set(scopes_before) | set(scopes_after)
        if scopes_before.get(user_id, []) != scopes_after.get(user_id, [])
    }
    new = _snapshot(department, fields)
    if changed_scopes:
        new["scope_impact"] = changed_scopes
        department.scope_impact = changed_scopes
    _audit(
        company=company,
        actor=actor,
        action="update",
        instance=department,
        old_data=old,
        new_data=new,
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return department


@transaction.atomic
def create_employee(*, actor, company, data, request=None):
    from apps.masterdata.models import Employee

    require_manage_masterdata(actor, "employee")
    _require_current_company(company)
    values = dict(data)
    requested_user = values.get("user")
    require_application_user_target(requested_user)
    if requested_user is not None and not actor.groups.filter(
        name="system_admin"
    ).exists():
        raise PermissionDenied("人员登录账号只能由 system_admin 进行技术关联。")
    if requested_user is not None:
        # A User is global to Django while every business relation is scoped
        # to one Company.  Lock and validate the existing employee/scope edges
        # before creating the reverse edge, otherwise SQLite and a crafted
        # Service call could bind one login identity across two companies (and
        # PostgreSQL would surface only a deferred IntegrityError at commit).
        values["user"] = _lock_user_company_links(company, requested_user)
    employee = _apply(
        Employee(company=company),
        values,
        {
            "employee_no",
            "name",
            "department",
            "employment_status",
            "hire_date",
            "termination_date",
            "mobile",
            "remark",
            "is_active",
            "user",
        },
    )
    _validate_same_company(company, department=employee.department)
    if employee.employment_status in {"leaving", "resigned"}:
        employee.is_active = False
    _save(employee)
    _audit(
        company=company,
        actor=actor,
        action="create",
        instance=employee,
        new_data=_snapshot(
            employee,
            (
                "employee_no",
                "name",
                "department",
                "user",
                "employment_status",
                "hire_date",
                "termination_date",
                "mobile",
                "remark",
                "is_active",
            ),
        ),
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return employee


def _clear_manager_assignments(*, employee, actor, request):
    from apps.masterdata.models import Department
    from apps.supplies.models import SupplyWarehouse

    departments = list(
        Department.objects.select_for_update().filter(manager_employee=employee)
    )
    for department in departments:
        old = {"manager_employee": employee.pk}
        department.manager_employee = None
        department.save(update_fields=["manager_employee", "updated_at"])
        _audit(
            company=department.company,
            actor=actor,
            action="manager_cleared",
            instance=department,
            old_data=old,
            new_data={
                "manager_employee": None,
                "reason": "经理进入离职处理中、已离职或被停用",
            },
            request=request,
        )

    warehouses = list(
        SupplyWarehouse.objects.select_for_update().filter(
            manager_employee=employee
        )
    )
    for warehouse in warehouses:
        old = {"manager_employee": employee.pk}
        warehouse.manager_employee = None
        warehouse.updated_by = actor
        warehouse.save(
            update_fields=["manager_employee", "updated_by", "updated_at"]
        )
        _audit(
            company=warehouse.company,
            actor=actor,
            action="supply_warehouse_manager_cleared",
            instance=warehouse,
            old_data=old,
            new_data={
                "manager_employee": None,
                "reason": "负责人进入离职处理中、已离职或被停用",
            },
            request=request,
        )


@transaction.atomic
def update_employee(*, actor, employee, data, request=None):
    from apps.masterdata.models import Department, Employee

    business_fields = {
        "employee_no",
        "name",
        "department",
        "employment_status",
        "hire_date",
        "termination_date",
        "mobile",
        "remark",
        "is_active",
    }
    if any(field in data for field in business_fields):
        require_manage_masterdata(actor, "employee")
    if "user" in data:
        require_manage_masterdata(actor, "employee_user")
        require_application_user_target(data.get("user"))
    _require_current_company(employee.company)
    _lock_manager_write(employee.company_id)

    requested_user = data.get("user") if "user" in data else None
    user_ids_to_lock = sorted(
        {
            user_id
            for user_id in (employee.user_id, getattr(requested_user, "pk", None))
            if user_id is not None
        }
    )
    locked_users = {
        user_id: _lock_user_company_links(
            employee.company, get_user_model().objects.get(pk=user_id)
        )
        for user_id in user_ids_to_lock
    }
    linked_user = (
        locked_users.get(getattr(requested_user, "pk", None))
        if "user" in data
        else None
    )
    requested_department = data.get("department") if "department" in data else None
    managed_department_ids = Department.objects.filter(
        manager_employee_id=employee.pk
    ).values_list("pk", flat=True)
    employees, departments = _lock_manager_rows(
        employee_ids=(employee.pk,),
        department_ids=(
            employee.department_id,
            getattr(requested_department, "pk", None),
            *managed_department_ids,
        ),
    )
    employee = employees[employee.pk]
    company = employee.company
    can_hr = actor.groups.filter(name="hr").exists()
    can_admin = actor.groups.filter(name="system_admin").exists()
    if not can_hr and not can_admin:
        raise PermissionDenied("您没有维护人员资料的权限。")

    if any(field in data for field in business_fields) and not can_hr:
        raise PermissionDenied("system_admin 只能维护人员的登录账号技术关联。")
    if "user" in data and data["user"] != employee.user and not can_admin:
        raise PermissionDenied("人员登录账号只能由 system_admin 进行技术关联。")

    if (
        "employment_status" in data
        and data["employment_status"] != employee.employment_status
    ):
        raise ValidationError(
            {"employment_status": "任职状态只能通过离职资产清退流程变更。"}
        )
    if (
        "termination_date" in data
        and data["termination_date"] != employee.termination_date
    ):
        raise ValidationError(
            {"termination_date": "实际离职日期只能由清退完成动作写入。"}
        )

    fields = (*sorted(business_fields), "user")
    old = _snapshot(employee, fields)
    old_status = employee.employment_status
    _apply(employee, data, fields)
    if employee.department_id:
        employee.department = departments[employee.department_id]
    if "user" in data:
        employee.user = linked_user
    _validate_same_company(company, department=employee.department)
    if old_status == "resigned" and employee.employment_status != "resigned":
        raise ValidationError(
            {"employment_status": "普通页面不允许将已离职员工恢复为在职。"}
        )
    if employee.employment_status in {"leaving", "resigned"}:
        employee.is_active = False
    # The database guard is deferred, but clearing first makes the Service's
    # invariant explicit and keeps it correct even outside PostgreSQL tests.
    if employee.employment_status != "active" or not employee.is_active:
        _clear_manager_assignments(employee=employee, actor=actor, request=request)
    _save(employee)
    _audit(
        company=company,
        actor=actor,
        action="update",
        instance=employee,
        old_data=old,
        new_data=_snapshot(employee, fields),
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return employee


@transaction.atomic
def set_employee_active(*, actor, employee, is_active, request=None):
    if is_active and employee.employment_status != "active":
        raise ValidationError("离职处理中或已离职员工不能重新启用。")
    return update_employee(
        actor=actor,
        employee=employee,
        data={"is_active": bool(is_active)},
        request=request,
    )


@transaction.atomic
def link_employee_user(*, actor, employee, user, request=None):
    require_manage_masterdata(actor, "employee_user")
    return update_employee(
        actor=actor,
        employee=employee,
        data={"user": user},
        request=request,
    )


def _create_tree_master(
    *, actor, company, model, resource, level_field, data, request=None
):
    require_manage_masterdata(actor, resource)
    if resource == "asset_category" and "default_coding_scheme" in data:
        require_manage_masterdata(actor, "coding_scheme")
    _require_current_company(company)
    _lock_tree_write(model, company.pk)
    instance = _apply(
        model(company=company),
        data,
        {
            "code",
            "name",
            "parent",
            "location_type",
            "category_type",
            "is_maintenance_required_default",
            "default_coding_scheme",
            "is_active",
        },
    )
    if instance.parent_id:
        instance.parent = model.objects.select_for_update().get(pk=instance.parent_id)
    _validate_same_company(company, parent=instance.parent)
    _validate_location_parent_is_available(model, instance.parent)
    if level_field:
        setattr(instance, level_field, (getattr(instance.parent, level_field) + 1) if instance.parent else 1)
    _save(instance)
    fields = ["code", "name", "parent", level_field, "is_active"]
    fields.extend(
        field
        for field in ("location_type", "category_type", "is_maintenance_required_default", "default_coding_scheme")
        if hasattr(instance, field)
    )
    _audit(
        company=company,
        actor=actor,
        action="create",
        instance=instance,
        new_data=_snapshot(instance, fields),
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return instance


@transaction.atomic
def create_location(*, actor, company, data, request=None):
    from apps.masterdata.models import Location

    return _create_tree_master(
        actor=actor,
        company=company,
        model=Location,
        resource="location",
        level_field="level",
        data=data,
        request=request,
    )


@transaction.atomic
def create_asset_category(*, actor, company, data, request=None):
    from apps.masterdata.models import AssetCategory

    return _create_tree_master(
        actor=actor,
        company=company,
        model=AssetCategory,
        resource="asset_category",
        level_field="category_level",
        data=data,
        request=request,
    )


def _update_tree_master(
    *, actor, instance, model, resource, level_field, data, request=None
):
    require_manage_masterdata(actor, resource)
    if resource == "asset_category" and "default_coding_scheme" in data:
        require_manage_masterdata(actor, "coding_scheme")
    _require_current_company(instance.company)
    _lock_tree_write(model, instance.company_id)
    instance = model.objects.select_for_update().get(pk=instance.pk)
    company = instance.company
    _lock_tree_write(model, company.pk)
    fields = ["code", "name", "parent", "is_active"]
    fields.extend(
        field
        for field in ("location_type", "category_type", "is_maintenance_required_default", "default_coding_scheme")
        if hasattr(instance, field)
    )
    old = _snapshot(instance, [*fields, level_field])
    _apply(instance, data, fields)
    if instance.parent_id:
        instance.parent = model.objects.select_for_update().get(pk=instance.parent_id)
    _validate_same_company(company, parent=instance.parent)
    if instance.parent_id != old["parent"]:
        _validate_location_parent_is_available(model, instance.parent)
    setattr(instance, level_field, (getattr(instance.parent, level_field) + 1) if instance.parent else 1)
    _save(instance)
    # Reparenting changes the materialized level of every descendant.  Lock and
    # recompute the subtree in this transaction so clients never observe a
    # partially updated path.
    pending = [instance]
    seen = {instance.pk}
    while pending:
        parent = pending.pop(0)
        children = list(
            model.objects.select_for_update().filter(
                company=company, parent=parent
            )
        )
        for child in children:
            if child.pk in seen:
                raise ValidationError("树结构存在循环，无法重算下级层级。")
            seen.add(child.pk)
            expected_level = getattr(parent, level_field) + 1
            if getattr(child, level_field) != expected_level:
                setattr(child, level_field, expected_level)
                child.full_clean()
                child.save(update_fields=[level_field, "updated_at"])
            pending.append(child)
    _audit(
        company=company,
        actor=actor,
        action="update",
        instance=instance,
        old_data=old,
        new_data=_snapshot(instance, [*fields, level_field]),
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return instance


@transaction.atomic
def update_location(*, actor, location, data, request=None):
    from apps.masterdata.models import Location

    return _update_tree_master(
        actor=actor,
        instance=location,
        model=Location,
        resource="location",
        level_field="level",
        data=data,
        request=request,
    )


@transaction.atomic
def update_asset_category(*, actor, category, data, request=None):
    from apps.masterdata.models import AssetCategory

    return _update_tree_master(
        actor=actor,
        instance=category,
        model=AssetCategory,
        resource="asset_category",
        level_field="category_level",
        data=data,
        request=request,
    )


def _normalize_string_list(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in value.replace("\n", ",").split(",")]
    else:
        parsed = list(value) if isinstance(value, (list, tuple, set)) else []
    normalized = []
    for item in parsed:
        item = str(item).strip().lower().lstrip(".")
        if item and item not in normalized:
            normalized.append(item)
    if not normalized:
        raise ValidationError("附件扩展名白名单至少包含一项。")
    invalid = sorted(set(normalized) - SAFE_ATTACHMENT_EXTENSIONS)
    if invalid:
        raise ValidationError(f"不允许的附件扩展名：{', '.join(invalid)}")
    return normalized


def _serialize_setting(key, value):
    entry = SYSTEM_SETTING_REGISTRY[key]
    value_type = entry["value_type"]
    if value_type == "string_list":
        normalized = _normalize_string_list(value)
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":")), normalized
    if value_type == "integer":
        if isinstance(value, bool):
            raise ValidationError("附件大小上限必须是整数。")
        try:
            normalized = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError("附件大小上限必须是整数。") from exc
        if normalized < 1 or normalized > 20 * 1024 * 1024:
            raise ValidationError("附件大小上限必须在 1 至 20971520 字节之间。")
        return str(normalized), normalized
    if value_type == "decimal":
        try:
            normalized = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError("设置值必须是 Decimal 数值。") from exc
        if not normalized.is_finite() or normalized < 0:
            raise ValidationError("设置值必须是有限且不小于 0 的 Decimal 数值。")
        return format(normalized.quantize(Decimal("0.01")), "f"), normalized
    raise ValidationError("不支持的设置类型。")


def get_system_setting(*, company, key):
    from apps.masterdata.models import SystemSetting

    _require_current_company(company)
    if key not in SYSTEM_SETTING_REGISTRY:
        raise ValidationError("未知的系统设置 key。")
    entry = SYSTEM_SETTING_REGISTRY[key]
    setting = SystemSetting.objects.filter(company=company, key=key).first()
    if setting is None:
        default = entry["default"]
        return list(default) if isinstance(default, list) else default
    if setting.value_type != entry["value_type"]:
        raise ValidationError("系统设置保存的 value_type 与 registry 不一致。")
    _, parsed = _serialize_setting(key, setting.value)
    return parsed


@transaction.atomic
def set_system_setting(
    *, actor, company, key, value, value_type=None, request=None
):
    from apps.masterdata.models import SystemSetting

    _require_current_company(company)
    if key not in SYSTEM_SETTING_REGISTRY:
        raise ValidationError("未知的系统设置 key。")
    entry = SYSTEM_SETTING_REGISTRY[key]
    require_roles(actor, {entry["writer"]}, "您没有修改此系统设置的权限。")
    if value_type is not None and value_type != entry["value_type"]:
        raise ValidationError("value_type 与固定 registry 不一致。")
    serialized, parsed = _serialize_setting(key, value)
    setting = SystemSetting.objects.select_for_update().filter(
        company=company, key=key
    ).first()
    old = {}
    if setting is None:
        setting = SystemSetting(company=company, key=key)
        action = "create"
    else:
        action = "update"
        old = {"key": setting.key, "value": setting.value, "value_type": setting.value_type}
    setting.value = serialized
    setting.value_type = entry["value_type"]
    setting.description = entry["description"]
    setting.updated_by = actor
    _save(setting)
    _audit(
        company=company,
        actor=actor,
        action=action,
        instance=setting,
        old_data=old,
        new_data={"key": key, "value": parsed, "value_type": setting.value_type},
        request=request,
    )
    return setting


def _validate_user_company(company, user):
    from apps.masterdata.models import Employee, UserDepartmentScope

    if Employee.objects.filter(user=user).exclude(company=company).exists():
        raise ValidationError("用户已绑定其他公司的员工，不能跨公司授权。")
    if UserDepartmentScope.objects.filter(user=user).exclude(company=company).exists():
        raise ValidationError("用户已有其他公司的部门范围，不能跨公司绑定员工。")


def _lock_user_company_links(company, user):
    """Serialize Employee/UserDepartmentScope changes for one User."""
    from apps.masterdata.models import Employee, UserDepartmentScope

    require_application_user_target(user)
    User = get_user_model()
    user = User.objects.select_for_update().get(pk=user.pk)
    require_application_user_target(user)
    list(
        Employee.objects.select_for_update()
        .filter(user=user)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    list(
        UserDepartmentScope.objects.select_for_update()
        .filter(user=user)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    _validate_user_company(company, user)
    return user


@transaction.atomic
def assign_department_scope(
    *, actor, company, user, department, include_descendants=True, reason="", request=None
):
    from apps.masterdata.models import UserDepartmentScope

    require_manage_masterdata(actor, "user_permissions")
    _require_current_company(company)
    require_application_user_target(user)
    if not str(reason).strip():
        raise ValidationError("分配部门范围必须填写原因。")
    user = _lock_user_company_links(company, user)
    _validate_same_company(company, department=department)
    if not department.is_active:
        raise ValidationError("只能新分配启用部门的数据范围。")
    if UserDepartmentScope.objects.filter(
        company=company, user=user, department=department, is_active=True
    ).exists():
        raise ValidationError("该用户已有此部门的活动授权范围。")
    scope = UserDepartmentScope(
        company=company,
        user=user,
        department=department,
        include_descendants=bool(include_descendants),
        is_active=True,
        assigned_by=actor,
        assigned_at=timezone.now(),
    )
    _save(scope)
    _audit(
        company=company,
        actor=actor,
        action="scope_assign",
        instance=scope,
        new_data={
            "user": user.pk,
            "department": department.pk,
            "include_descendants": scope.include_descendants,
            "reason": str(reason).strip(),
        },
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return scope


@transaction.atomic
def revoke_department_scope(*, actor, scope, reason="", request=None):
    from apps.masterdata.models import UserDepartmentScope

    require_manage_masterdata(actor, "user_permissions")
    if not str(reason).strip():
        raise ValidationError("撤销部门范围必须填写原因。")
    scope = UserDepartmentScope.objects.select_for_update().get(pk=scope.pk)
    _require_current_company(scope.company)
    if not scope.is_active:
        return scope
    old = {
        "is_active": True,
        "department": scope.department_id,
        "include_descendants": scope.include_descendants,
    }
    scope.is_active = False
    scope.revoked_by = actor
    scope.revoked_at = timezone.now()
    _save(scope)
    _audit(
        company=scope.company,
        actor=actor,
        action="scope_revoke",
        instance=scope,
        old_data=old,
        new_data={"is_active": False, "reason": str(reason).strip()},
        request=request,
    )
    refresh_initialization_progress(
        company=scope.company, actor=actor, request=request
    )
    return scope


def _login_capable_role_users(role_name, *, excluding=None, lock=False):
    User = get_user_model()
    user_ids = User.objects.filter(
        is_active=True, groups__name=role_name
    ).values_list("pk", flat=True)
    users = User.objects.filter(pk__in=user_ids)
    if excluding is not None:
        users = users.exclude(pk=excluding.pk)
    if lock:
        users = users.select_for_update().order_by("pk")
    return [user for user in users if is_login_capable(user)]


def _lock_role_changes(company, roles):
    for role in sorted(roles):
        _advisory_xact_lock(f"masterdata:role:{company.pk}:{role}")


@transaction.atomic
def set_user_roles(
    *, actor, company, user, roles, reason, current_password="", request=None
):
    require_manage_masterdata(actor, "user_permissions")
    _require_current_company(company)
    require_application_user_target(user)
    ensure_fixed_roles()
    normalized_roles = set(roles)
    unknown = normalized_roles - set(ROLE_NAMES)
    if unknown:
        raise ValidationError(f"不允许的角色：{', '.join(sorted(unknown))}")
    if not str(reason).strip():
        raise ValidationError("角色变更原因不能为空。")

    high_risk = {"system_admin", "finance"}
    _lock_role_changes(company, high_risk)

    from apps.masterdata.models import UserDepartmentScope

    if "department_manager" in normalized_roles and not UserDepartmentScope.objects.filter(
        company=company, user=user, is_active=True, department__is_active=True
    ).exists():
        raise ValidationError(
            "授予 department_manager 前必须先为该用户分配至少一个启用部门范围。"
        )

    User = get_user_model()
    user = _lock_user_company_links(company, user)
    old_roles = set(user.groups.filter(name__in=ROLE_NAMES).values_list("name", flat=True))
    changed = old_roles.symmetric_difference(normalized_roles)
    if changed.intersection(high_risk) and not actor.check_password(current_password):
        raise ValidationError("当前密码验证失败，不能执行高风险角色变更。")

    for protected_role in high_risk:
        if protected_role in old_roles and protected_role not in normalized_roles:
            if not _login_capable_role_users(
                protected_role, excluding=user, lock=True
            ):
                label = "system_admin" if protected_role == "system_admin" else "finance"
                raise ValidationError(f"不能移除最后一名可登录的 {label}。")

    groups = list(Group.objects.filter(name__in=normalized_roles))
    user.groups.set(groups)
    _audit(
        company=company,
        actor=actor,
        action="roles_update",
        instance=user,
        old_data={"roles": sorted(old_roles)},
        new_data={"roles": sorted(normalized_roles), "reason": str(reason).strip()},
        request=request,
    )
    refresh_initialization_progress(company=company, actor=actor, request=request)
    return user


@transaction.atomic
def create_application_user(
    *,
    actor,
    company,
    username,
    display_name,
    password,
    roles,
    reason,
    current_password,
    email="",
    mobile="",
    initial_department=None,
    include_descendants=True,
    request=None,
):
    """Create one ordinary application user through controlled permissions."""

    require_manage_masterdata(actor, "user_permissions")
    _require_current_company(company)
    if not actor.check_password(current_password):
        raise ValidationError({"current_password": "当前操作人密码验证失败。"})

    reason = str(reason).strip()
    if not reason:
        raise ValidationError({"reason": "创建用户必须填写原因。"})

    normalized_roles = set(roles)
    if not normalized_roles:
        raise ValidationError({"roles": "新用户至少需要一个固定角色。"})
    unknown = normalized_roles - set(ROLE_NAMES)
    if unknown:
        raise ValidationError({"roles": f"不允许的角色：{', '.join(sorted(unknown))}"})
    if "department_manager" in normalized_roles and initial_department is None:
        raise ValidationError(
            {"initial_department": "部门负责人必须同时配置一个启用部门范围。"}
        )
    if "department_manager" not in normalized_roles and initial_department is not None:
        raise ValidationError(
            {"initial_department": "只有部门负责人需要在创建时配置部门范围。"}
        )

    User = get_user_model()
    username = User.normalize_username(str(username).strip())
    display_name = str(display_name).strip()
    if not username or not display_name:
        raise ValidationError("用户名和显示名称不得为空。")
    if User.objects.filter(username__iexact=username).exists():
        raise ValidationError({"username": "该用户名已存在。"})

    user = User(
        username=username,
        display_name=display_name,
        email=str(email).strip(),
        mobile=str(mobile).strip(),
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )
    validate_password(password, user=user)
    user.full_clean(exclude={"password"})
    ensure_fixed_roles()
    user.set_password(password)
    try:
        user.save()
    except IntegrityError as exc:
        raise ValidationError({"username": "该用户名已存在。"}) from exc

    _audit(
        company=company,
        actor=actor,
        action="user_create",
        instance=user,
        new_data={
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "mobile": user.mobile,
            "is_active": user.is_active,
            "roles": sorted(normalized_roles),
            "initial_department": (
                initial_department.pk if initial_department is not None else None
            ),
            "include_descendants": bool(include_descendants),
            "reason": reason,
        },
        request=request,
    )

    if initial_department is not None:
        assign_department_scope(
            actor=actor,
            company=company,
            user=user,
            department=initial_department,
            include_descendants=include_descendants,
            reason=reason,
            request=request,
        )
    set_user_roles(
        actor=actor,
        company=company,
        user=user,
        roles=normalized_roles,
        reason=reason,
        current_password=current_password,
        request=request,
    )
    return user


def compute_initialization_progress(company) -> dict[str, bool]:
    # Progress is also recomputed while a controlled company deactivate or
    # recovery transition is in flight.
    _require_current_company(company, include_inactive=True)
    from apps.masterdata.models import (
        AssetCategory,
        AssetCodingScheme,
        Department,
        Employee,
        Location,
        UserDepartmentScope,
    )

    User = get_user_model()
    login_users = [user for user in User.objects.filter(is_active=True) if is_login_capable(user)]
    login_ids = [user.pk for user in login_users]
    has_admin = User.objects.filter(
        pk__in=login_ids, groups__name="system_admin"
    ).exists()
    has_finance = User.objects.filter(pk__in=login_ids, groups__name="finance").exists()
    managers = User.objects.filter(
        is_active=True,
        is_superuser=False,
        groups__name="department_manager",
    ).distinct()
    manager_scopes_ok = all(
        UserDepartmentScope.objects.filter(
            company=company,
            user=manager,
            department__is_active=True,
            is_active=True,
        ).exists()
        for manager in managers
    )
    today = timezone.localdate()
    coding_defaults = list(
        AssetCodingScheme.objects.filter(
            company=company,
            status="active",
            is_default=True,
            effective_from__lte=today,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .prefetch_related("segments")
    )
    coding_configured = False
    if len(coding_defaults) == 1:
        from apps.coding.domain import validate_scheme_structure

        try:
            validate_scheme_structure(coding_defaults[0])
        except ValidationError:
            pass
        else:
            coding_configured = True
    finance_configured = False
    try:
        from apps.finance.models import DepreciationPolicy

        finance_defaults = list(
            DepreciationPolicy.objects.filter(
                company=company,
                status="active",
                is_default=True,
                effective_from__lte=today,
            ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        )
        explicit_warning = (
            company.system_settings.filter(
                key="fixed_asset_warning_amount", value_type="decimal"
            ).first()
        )
        if len(finance_defaults) == 1 and explicit_warning is not None:
            policy = finance_defaults[0]
            try:
                _serialize_setting(
                    "fixed_asset_warning_amount", explicit_warning.value
                )
                # Reuse the same validator used by finance configuration
                # mutations, avoiding a second interpretation of the matrix.
                from apps.finance.services import _validate_policy

                _validate_policy(policy)
            except ValidationError:
                pass
            else:
                finance_configured = True
    except (LookupError, ImportError):
        # Keeps historical migrations/checks usable before finance exists.
        finance_configured = False
    return {
        "company_configured": bool(
            company.is_active
            and company.code
            and company.name
            and company.short_name
            and company.currency == "CNY"
            and company.timezone == "Asia/Shanghai"
        ),
        "departments_configured": Department.objects.filter(
            company=company, is_active=True
        ).exists(),
        "employees_configured": Employee.objects.filter(
            company=company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        ).exists(),
        "categories_configured": AssetCategory.objects.filter(
            company=company, is_active=True
        ).exists(),
        "locations_configured": Location.objects.filter(
            company=company, location_type="position", is_active=True
        ).exists(),
        "coding_scheme_configured": coding_configured,
        "finance_rules_configured": finance_configured,
        "users_configured": has_admin and has_finance,
        "permissions_configured": manager_scopes_ok,
    }


@transaction.atomic
def refresh_initialization_progress(*, company, actor, request=None):
    from apps.masterdata.models import InitializationSetting

    values = compute_initialization_progress(company)
    setting, created = InitializationSetting.objects.select_for_update().get_or_create(
        company=company
    )
    fields = tuple(values)
    old = _snapshot(setting, (*fields, "initialization_completed"))
    if setting.initialization_completed:
        # A completed setup marker is durable.  Revalidation may reveal that a
        # later Sprint introduced a new prerequisite, but writing a false flag
        # beside ``initialization_completed=True`` would both violate the
        # database invariant and silently rewrite the historical completion.
        # Report the live missing conditions and leave the completed snapshot
        # untouched; the UI still renders ``values`` from a fresh computation.
        missing = [field for field, value in values.items() if not value]
        if missing:
            raise ValidationError(
                {"initialization": "当前真实配置仍有未满足项：" + "、".join(missing)}
            )
        return setting
    changed = created
    for field, value in values.items():
        if getattr(setting, field) != value:
            setattr(setting, field, value)
            changed = True
    # Completion is a separate explicit system_admin action. Configuration
    # refreshes never silently complete or unset the durable completion marker.
    if changed:
        setting.save(update_fields=[*fields])
        _audit(
            company=company,
            actor=actor,
            action="setup_progress_update",
            instance=setting,
            old_data=old,
            new_data=_snapshot(setting, (*fields, "initialization_completed")),
            request=request,
        )
    return setting


@transaction.atomic
def complete_initialization(*, actor, company, request=None):
    """Re-query all nine real conditions and atomically complete setup."""

    from apps.masterdata.models import Company, InitializationSetting

    require_roles(actor, {"system_admin"}, "只有 system_admin 可以完成初始化。")
    selected = current_company(include_inactive=True)
    if selected is None or getattr(company, "pk", None) != selected.pk:
        raise PermissionDenied("目标公司不是当前 V1 公司。")
    company = Company.objects.select_for_update().get(pk=company.pk)
    setting, _ = InitializationSetting.objects.select_for_update().get_or_create(
        company=company
    )
    values = compute_initialization_progress(company)
    missing = [key for key, value in values.items() if not value]
    old = _snapshot(
        setting,
        (*values, "initialization_completed", "completed_by", "completed_at"),
    )
    if missing:
        # A failed final check has no partial write.  The setup page renders
        # repair links from the freshly queried ``values`` above, while the
        # durable completion marker (if it already exists) is never rolled
        # back by an ordinary re-check.
        raise ValidationError(
            {"initialization": "仍有未满足项：" + "、".join(missing)}
        )
    if setting.initialization_completed:
        return setting
    for field, value in values.items():
        setattr(setting, field, value)
    setting.initialization_completed = True
    setting.completed_by = actor
    setting.completed_at = timezone.now()
    setting.save(
        update_fields=[
            *values,
            "initialization_completed",
            "completed_by",
            "completed_at",
        ]
    )
    _audit(
        company=company,
        actor=actor,
        action="initialization_complete",
        instance=setting,
        old_data=old,
        new_data=_snapshot(
            setting,
            (*values, "initialization_completed", "completed_by", "completed_at"),
        ),
        request=request,
    )
    return setting
