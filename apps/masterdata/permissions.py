"""Sprint 1 master-data authorization and department-scope helpers."""

from __future__ import annotations

from collections import defaultdict, deque

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.accounts.roles import ROLE_NAMES


GLOBAL_DEPARTMENT_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "warehouse", "hr", "management"}
)

MASTERDATA_VIEW_ROLES = {
    "company": frozenset({"system_admin", "finance", "equipment", "management"}),
    "department": frozenset(
        {"system_admin", "finance", "department_manager", "management"}
    ),
    "employee": frozenset(
        {
            "system_admin",
            "finance",
            "equipment",
            "department_manager",
            "employee",
            "warehouse",
            "hr",
            "management",
        }
    ),
    "location": frozenset(
        {"system_admin", "finance", "equipment", "management"}
    ),
    "asset_category": frozenset(
        {"system_admin", "finance", "equipment", "management"}
    ),
    "system_setting": frozenset(
        {"system_admin", "finance", "equipment", "management"}
    ),
    "user_permissions": frozenset({"system_admin"}),
}

MASTERDATA_MANAGE_ROLES = {
    "company": frozenset({"system_admin"}),
    "department": frozenset({"system_admin"}),
    "employee": frozenset({"hr"}),
    "employee_user": frozenset({"system_admin"}),
    "location": frozenset({"system_admin", "equipment"}),
    "asset_category": frozenset({"system_admin", "equipment"}),
    "system_setting": frozenset({"system_admin"}),
    "user_permissions": frozenset({"system_admin"}),
}


def assigned_role_names_for(user) -> set[str]:
    """Return configured fixed roles without turning them into authorization."""
    if not getattr(user, "pk", None):
        return set()
    return set(user.groups.filter(name__in=ROLE_NAMES).values_list("name", flat=True))


def role_names_for(user) -> set[str]:
    if (
        not getattr(user, "is_authenticated", False)
        or not user.is_active
        or user.is_superuser
    ):
        return set()
    return assigned_role_names_for(user)


def require_application_user_target(user):
    """Reject Django recovery accounts at every application assignment edge."""
    if user is None:
        return None
    if not getattr(user, "pk", None) or user.is_superuser:
        raise PermissionDenied("Django recovery superuser 不能作为应用用户配置。")
    return user


def has_role(user, role_name: str) -> bool:
    return role_name in role_names_for(user)


def has_any_role(user, roles) -> bool:
    return bool(role_names_for(user).intersection(roles))


def require_roles(user, roles, message="您没有执行此操作的权限。") -> None:
    if not has_any_role(user, roles):
        raise PermissionDenied(message)


def can_view_masterdata(user, resource: str) -> bool:
    return has_any_role(user, MASTERDATA_VIEW_ROLES.get(resource, ()))


def can_manage_masterdata(user, resource: str) -> bool:
    return has_any_role(user, MASTERDATA_MANAGE_ROLES.get(resource, ()))


def require_view_masterdata(user, resource: str) -> None:
    if not can_view_masterdata(user, resource):
        raise PermissionDenied("您没有查看此基础资料的权限。")


def require_manage_masterdata(user, resource: str) -> None:
    if not can_manage_masterdata(user, resource):
        raise PermissionDenied("您没有维护此基础资料的权限。")


def current_company(*, include_inactive=False):
    """Return the V1 company without implying that company switching exists."""
    from apps.masterdata.models import Company

    active = Company.objects.filter(is_active=True).order_by("created_at").first()
    if active is not None or not include_inactive:
        return active
    return Company.objects.order_by("created_at").first()


def resolve_department_ids(user, company, *, require_action_role=True) -> set:
    """Resolve active grants against the current tree on every call.

    A grant alone never authorizes an action.  Callers must separately require
    ``department_manager`` (or another matrix role).
    """
    from apps.masterdata.models import Department, UserDepartmentScope

    if company is None or (
        require_action_role and not has_role(user, "department_manager")
    ):
        return set()

    scopes = list(
        UserDepartmentScope.objects.filter(
            company=company,
            user=user,
            is_active=True,
        ).values_list("department_id", "include_descendants")
    )
    if not scopes:
        return set()

    children = defaultdict(list)
    for department_id, parent_id in Department.objects.filter(company=company).values_list(
        "id", "parent_id"
    ):
        children[parent_id].append(department_id)

    result = set()
    for root_id, include_descendants in scopes:
        result.add(root_id)
        if not include_descendants:
            continue
        queue = deque(children.get(root_id, ()))
        while queue:
            department_id = queue.popleft()
            if department_id in result:
                continue
            result.add(department_id)
            queue.extend(children.get(department_id, ()))
    return result


def user_can_access_department(user, company, department) -> bool:
    if department is None or company is None or department.company_id != company.pk:
        return False
    roles = role_names_for(user)
    if roles.intersection(GLOBAL_DEPARTMENT_ROLES):
        return True
    if "department_manager" in roles and department.pk in resolve_department_ids(
        user, company
    ):
        return True
    if "employee" in roles:
        from apps.masterdata.models import Employee

        return Employee.objects.filter(
            company=company,
            user=user,
            department=department,
        ).exists()
    return False


def scoped_departments(user, company, queryset=None):
    from apps.masterdata.models import Department, Employee

    queryset = queryset if queryset is not None else Department.objects.all()
    queryset = queryset.filter(company=company)
    roles = role_names_for(user)
    if roles.intersection(GLOBAL_DEPARTMENT_ROLES):
        return queryset
    allowed_ids = set()
    if "department_manager" in roles:
        allowed_ids.update(resolve_department_ids(user, company))
    if "employee" in roles:
        allowed_ids.update(
            Employee.objects.filter(company=company, user=user).values_list(
                "department_id", flat=True
            )
        )
    return queryset.filter(pk__in=allowed_ids)


def scoped_employees(user, company, queryset=None):
    from apps.masterdata.models import Employee

    queryset = queryset if queryset is not None else Employee.objects.all()
    queryset = queryset.filter(company=company)
    roles = role_names_for(user)
    if roles.intersection(GLOBAL_DEPARTMENT_ROLES):
        return queryset
    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(department_id__in=resolve_department_ids(user, company))
    if "employee" in roles:
        filters |= Q(user=user)
    return queryset.filter(filters).distinct()


def is_login_capable(user) -> bool:
    return bool(
        user.is_active
        and not user.is_superuser
        and user.has_usable_password()
    )


def can_access_setup(user) -> bool:
    # Only the roles that coordinate or write Sprint 1 setup steps may enter.
    return has_any_role(user, {"system_admin", "hr", "equipment"})
