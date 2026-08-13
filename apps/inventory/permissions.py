"""Sprint 8 inventory authorization and object-scope helpers.

Assignment grants access only to one inventory task.  It never widens the
caller's normal asset-ledger scope and it never grants access to F1 fields.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import resolve_department_ids, role_names_for


INVENTORY_EXECUTION_ROLES = frozenset(
    {"finance", "equipment", "department_manager", "employee", "warehouse"}
)
INVENTORY_GLOBAL_VIEW_ROLES = frozenset({"finance", "equipment", "management"})
INVENTORY_GLOBAL_EXECUTION_ROLES = frozenset({"finance", "equipment"})


def _roles(user) -> set[str]:
    return role_names_for(user)


def _persisted_same_company(instance, company=None) -> bool:
    if instance is None or not getattr(instance, "pk", None):
        return False
    company_id = getattr(instance, "company_id", None)
    return bool(company_id and (company is None or company_id == company.pk))


def _department_task_in_scope(user, task) -> bool:
    return bool(
        task.inventory_type == "department"
        and task.scope_type == "department"
        and task.scope_department_id
        and task.scope_department_id
        in resolve_department_ids(user, task.company)
    )


def _has_current_assignment(user, task) -> bool:
    if not getattr(user, "pk", None) or not user.is_active:
        return False
    from apps.inventory.models import InventoryTaskAssignee

    return InventoryTaskAssignee.objects.filter(
        company=task.company,
        inventory_task=task,
        user=user,
    ).exists()


def scoped_inventory_tasks(user, company, queryset=None):
    """Apply company scope and the fixed inventory view matrix."""

    from apps.inventory.models import InventoryTask

    queryset = queryset if queryset is not None else InventoryTask.objects.all()
    queryset = queryset.filter(company=company)
    roles = _roles(user)
    if roles.intersection(INVENTORY_GLOBAL_VIEW_ROLES):
        return queryset
    filters = Q(pk__in=[])
    if "department_manager" in roles:
        filters |= Q(
            inventory_type="department",
            scope_type="department",
            scope_department_id__in=resolve_department_ids(user, company),
        )
    if roles.intersection({"employee", "warehouse"}):
        filters |= Q(assignees__user=user)
    return queryset.filter(filters).distinct()


def can_view_inventory_task(user, task) -> bool:
    return bool(
        _persisted_same_company(task)
        and scoped_inventory_tasks(user, task.company).filter(pk=task.pk).exists()
    )


def require_view_inventory_task(user, task) -> None:
    if not can_view_inventory_task(user, task):
        raise PermissionDenied("您没有查看此盘点任务的权限。")


def can_create_inventory_task(
    user, company, inventory_type, *, scope_department=None
) -> bool:
    roles = _roles(user)
    if inventory_type == "full":
        return "finance" in roles
    if inventory_type == "special":
        return bool(roles.intersection({"finance", "equipment"}))
    if inventory_type != "department":
        return False
    if roles.intersection({"finance", "equipment"}):
        return True
    return bool(
        "department_manager" in roles
        and scope_department is not None
        and scope_department.company_id == company.pk
        and scope_department.pk in resolve_department_ids(user, company)
    )


def require_create_inventory_task(
    user, company, inventory_type, *, scope_department=None
) -> None:
    if not can_create_inventory_task(
        user, company, inventory_type, scope_department=scope_department
    ):
        raise PermissionDenied("您没有在此类型或范围创建盘点任务的权限。")


def can_publish_inventory_task(user, task) -> bool:
    return bool(
        _persisted_same_company(task)
        and task.status == "draft"
        and can_create_inventory_task(
            user,
            task.company,
            task.inventory_type,
            scope_department=task.scope_department,
        )
    )


def require_publish_inventory_task(user, task) -> None:
    if not can_publish_inventory_task(user, task):
        raise PermissionDenied("您没有发布此盘点任务的权限。")


def can_scan_inventory_task_scope(user, task) -> bool:
    """Authorize execution independent of mutable task state."""

    if not _persisted_same_company(task):
        return False
    roles = _roles(user)
    if roles.intersection(INVENTORY_GLOBAL_EXECUTION_ROLES):
        return True
    if "department_manager" in roles:
        if _department_task_in_scope(user, task):
            return True
        roles = roles - {"department_manager"}
    return bool(
        roles.intersection({"employee", "warehouse"})
        and _has_current_assignment(user, task)
    )


def can_scan_inventory_task(user, task) -> bool:
    """Authorize a normal scan without granting general asset access."""

    return bool(
        task.status == "in_progress" and can_scan_inventory_task_scope(user, task)
    )


def require_scan_inventory_task(user, task) -> None:
    if not can_scan_inventory_task(user, task):
        raise PermissionDenied("您不是此任务的有效执行人，或任务已停止扫码。")


def require_scan_inventory_task_scope(user, task) -> None:
    if not can_scan_inventory_task_scope(user, task):
        raise PermissionDenied("您不是此任务的有效执行人。")


def can_reconcile_inventory_task_scope(user, task) -> bool:
    """Authorize reconciliation independent of mutable task state."""

    if not _persisted_same_company(task):
        return False
    roles = _roles(user)
    if roles.intersection({"finance", "equipment"}):
        return True
    return bool(
        "department_manager" in roles and _department_task_in_scope(user, task)
    )


def can_reconcile_inventory_task(user, task) -> bool:
    return bool(
        task.status in {"in_progress", "reconciliation"}
        and can_reconcile_inventory_task_scope(user, task)
    )


def require_reconcile_inventory_task(user, task) -> None:
    if not can_reconcile_inventory_task(user, task):
        raise PermissionDenied("您没有停止扫码、补盘或处理此任务差异的权限。")


def require_reconcile_inventory_task_scope(user, task) -> None:
    if not can_reconcile_inventory_task_scope(user, task):
        raise PermissionDenied("您没有处理此盘点任务差异的业务权限。")


def can_close_inventory_task(user, task) -> bool:
    """Apply the task-type-specific close/cancel/correction matrix."""

    if not _persisted_same_company(task):
        return False
    roles = _roles(user)
    if task.inventory_type == "full":
        return "finance" in roles
    if task.inventory_type == "special":
        return bool(roles.intersection({"finance", "equipment"}))
    if task.inventory_type != "department":
        return False
    return bool(
        roles.intersection({"finance", "equipment"})
        or (
            "department_manager" in roles
            and _department_task_in_scope(user, task)
        )
    )


def require_close_inventory_task(user, task) -> None:
    if not can_close_inventory_task(user, task):
        raise PermissionDenied("您没有关闭、取消或更正此类盘点任务的权限。")


def can_convert_inventory_surplus(user, surplus) -> bool:
    return bool(
        _persisted_same_company(surplus)
        and "finance" in _roles(user)
        and can_view_inventory_task(user, surplus.inventory_task)
    )


def require_convert_inventory_surplus(user, surplus) -> None:
    if not can_convert_inventory_surplus(user, surplus):
        raise PermissionDenied("只有 finance 可确认盘盈并转为资产草稿。")


def can_view_inventory_attachment(user, link) -> bool:
    if getattr(link, "status", None) != "active":
        return False
    target = (
        getattr(link, "inventory_surplus", None)
        or getattr(link, "inventory_scan", None)
        or getattr(link, "inventory_resolution", None)
    )
    if target is None:
        return False
    task = getattr(target, "inventory_task", None)
    if task is None:
        task_asset = getattr(target, "inventory_task_asset", None) or getattr(
            target, "task_asset", None
        )
        task = getattr(task_asset, "inventory_task", None)
    if task is None or not can_view_inventory_task(user, task):
        return False
    if getattr(link, "security_class", None) == "A1":
        return bool(_roles(user).intersection({"finance", "management"}))
    return bool(
        _roles(user).intersection(
            {
                "finance", "equipment", "management", "department_manager",
                "employee", "warehouse",
            }
        )
    )


def require_view_inventory_attachment(user, link) -> None:
    if not can_view_inventory_attachment(user, link):
        raise PermissionDenied("您没有查看或下载此盘点附件的权限。")


def can_manage_inventory_attachment(user, target) -> bool:
    task = getattr(target, "inventory_task", None)
    if task is None:
        task_asset = getattr(target, "inventory_task_asset", None) or getattr(
            target, "task_asset", None
        )
        task = getattr(task_asset, "inventory_task", None)
    if task is None or task.status in {"closed", "cancelled"}:
        return False
    if task.status == "in_progress":
        return can_scan_inventory_task(user, task)
    return can_reconcile_inventory_task(user, task)


__all__ = [
    "INVENTORY_EXECUTION_ROLES",
    "can_close_inventory_task",
    "can_convert_inventory_surplus",
    "can_create_inventory_task",
    "can_manage_inventory_attachment",
    "can_publish_inventory_task",
    "can_reconcile_inventory_task_scope",
    "can_reconcile_inventory_task",
    "can_scan_inventory_task_scope",
    "can_scan_inventory_task",
    "can_view_inventory_attachment",
    "can_view_inventory_task",
    "require_close_inventory_task",
    "require_convert_inventory_surplus",
    "require_create_inventory_task",
    "require_publish_inventory_task",
    "require_reconcile_inventory_task_scope",
    "require_reconcile_inventory_task",
    "require_scan_inventory_task_scope",
    "require_scan_inventory_task",
    "require_view_inventory_attachment",
    "require_view_inventory_task",
    "scoped_inventory_tasks",
]
