from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.inventory.models import (
    InventoryResolution,
    InventoryScan,
    InventorySurplus,
    InventoryTask,
    InventoryTaskAsset,
    InventoryTaskAssignee,
)
from apps.inventory.permissions import (
    can_close_inventory_task,
    can_create_inventory_task,
    can_scan_inventory_task,
    scoped_inventory_tasks,
)
from apps.inventory.services import publish_inventory_task
from tests.test_sprint4_acceptance import _base_context
from tests.test_sprint3_support import grant_scope, make_department, make_user
from tests.test_sprint8_services import _draft as service_draft
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db


def _draft(
    context, key, *, inventory_type="department", department=None,
    status=InventoryTask.Status.DRAFT,
):
    department = department or context["department"]
    return InventoryTask.objects.create(
        company=context["company"],
        task_code=key,
        name=f"{key} 盘点",
        inventory_type=inventory_type,
        scope_type=("company" if inventory_type == "full" else "department"),
        scope_department=(None if inventory_type == "full" else department),
        planned_start=timezone.localdate(),
        planned_end=timezone.localdate() + timedelta(days=1),
        idempotency_key=f"{key}-idem",
        created_by=context["finance"],
        status=status,
        snapshot_at=(timezone.now() if status != InventoryTask.Status.DRAFT else None),
        expected_asset_count=(0 if status != InventoryTask.Status.DRAFT else None),
    )


def test_permission_matrix_for_task_type_scope_and_assignee_isolation():
    context = _base_context("S8PERM")
    manager = make_user("s8-perm-manager", "department_manager")
    employee = make_user("s8-perm-employee", "employee")
    warehouse = make_user("s8-perm-warehouse", "warehouse")
    other_department = make_department(context["company"], "S8PERM-OTHER")
    grant_scope(manager, context["company"], context["department"])

    assert can_create_inventory_task(
        context["finance"], context["company"], "full"
    )
    assert not can_create_inventory_task(
        context["equipment"], context["company"], "full"
    )
    assert can_create_inventory_task(
        context["equipment"], context["company"], "special"
    )
    assert can_create_inventory_task(
        manager,
        context["company"],
        "department",
        scope_department=context["department"],
    )
    assert not can_create_inventory_task(
        manager,
        context["company"],
        "department",
        scope_department=other_department,
    )
    assert not can_create_inventory_task(
        warehouse, context["company"], "department",
        scope_department=context["department"],
    )

    task = _draft(context, "S8PERM-TASK")
    InventoryTaskAssignee.objects.create(
        company=context["company"], inventory_task=task,
        user=employee, assigned_by=context["finance"],
    )
    task = publish_inventory_task(
        actor=context["finance"], task=task,
    )
    assert can_scan_inventory_task(employee, task)
    assert not can_scan_inventory_task(warehouse, task)
    assert scoped_inventory_tasks(employee, context["company"]).get() == task

    # Assignment does not grant company asset-ledger access or close rights.
    from apps.assets.permissions import scoped_assets

    assert not scoped_assets(employee, context["company"]).exists()
    assert not can_close_inventory_task(employee, task)

    # Removing the execution role immediately revokes the assignment action.
    employee.groups.clear()
    assert not can_scan_inventory_task(employee, task)


def test_full_close_and_cancel_role_remains_finance_only():
    context = _base_context("S8FULLPERM")
    full = _draft(context, "S8FULLPERM-TASK", inventory_type="full")
    assert can_close_inventory_task(context["finance"], full)
    assert not can_close_inventory_task(context["equipment"], full)


def test_model_constraints_reject_scope_matrix_and_duplicate_assignee():
    context = _base_context("S8MODEL")
    with pytest.raises(IntegrityError), transaction.atomic():
        InventoryTask._base_manager.create(
            company=context["company"], task_code="S8MODEL-BAD",
            name="非法范围", inventory_type="full",
            scope_type="company", scope_department=context["department"],
            planned_start=timezone.localdate(),
            planned_end=timezone.localdate(),
            idempotency_key="S8MODEL-BAD",
            created_by=context["finance"],
        )

    task = _draft(context, "S8MODEL-TASK")
    actor = make_user("s8-model-employee", "employee")
    InventoryTaskAssignee.objects.create(
        company=context["company"], inventory_task=task,
        user=actor, assigned_by=context["finance"],
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        InventoryTaskAssignee._base_manager.create(
            company=context["company"], inventory_task=task,
            user=actor, assigned_by=context["finance"],
        )


def test_history_models_refuse_normal_update_and_delete():
    context, _asset, _qr = inventory_context("S8IMM")
    task = service_draft(context, "S8IMM-TASK")
    with pytest.raises(ValidationError):
        task.name = "篡改名称"
        task.save()
    with pytest.raises(ValidationError):
        task.delete()
    with pytest.raises(ValidationError):
        InventoryTask.objects.filter(pk=task.pk).update(name="批量篡改")
    for model in (
        InventoryTaskAsset, InventoryScan, InventoryResolution, InventorySurplus,
    ):
        with pytest.raises(ValidationError):
            model.objects.all().update(company=None)
        with pytest.raises(ValidationError):
            model.objects.all().delete()

    actor = make_user("s8-immutable-assignee", "employee")
    InventoryTaskAssignee.objects.all().delete()
    assignee = InventoryTaskAssignee.objects.create(
        company=context["company"], inventory_task=task, user=actor,
        assigned_by=context["finance"],
    )
    task = publish_inventory_task(actor=context["finance"], task=task)
    assignee.refresh_from_db()
    with pytest.raises(ValidationError):
        assignee.delete()
    with pytest.raises(ValidationError):
        InventoryTaskAssignee.objects.filter(pk=assignee.pk).delete()
