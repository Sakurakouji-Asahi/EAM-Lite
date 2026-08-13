from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.inventory.models import (
    InventoryScan,
    InventoryTaskAsset,
)
from apps.inventory.services import (
    create_inventory_task_draft,
    inventory_task_summary,
    publish_inventory_task,
    scan_inventory_asset,
    stop_inventory_scanning,
    supplemental_scan,
)
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_support import add_active_asset, inventory_context


pytestmark = pytest.mark.django_db


def _task_data(context, key, **overrides):
    data = {
        "task_code": key,
        "name": f"{key} 盘点任务",
        "inventory_type": "department",
        "scope_type": "department",
        "scope_department": context["department"],
        "scope_location": None,
        "scope_category": None,
        "selected_asset_ids": [],
        "planned_start": timezone.localdate(),
        "planned_end": timezone.localdate() + timedelta(days=1),
        "remark": "Sprint 8 自动验收",
        "idempotency_key": f"{key}-create",
    }
    data.update(overrides)
    return data


def _draft(context, key, *, actor=None, assignees=None, **overrides):
    return create_inventory_task_draft(
        actor=actor or context["finance"],
        company=context["company"],
        data=_task_data(context, key, **overrides),
        assignee_users=assignees or [context["equipment"]],
    )


def _published(context, key, **overrides):
    return publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, key, **overrides),
    )


def _scan(context, task, qr, key, **overrides):
    payload = {
        "actor": context["equipment"],
        "task": task,
        "qr_identity": qr,
        "actual_location": context["location"],
        "actual_employee": context["employee"],
        "actual_status": "in_use",
        "idempotency_key": key,
    }
    payload.update(overrides)
    return scan_inventory_asset(**payload)


def test_draft_has_no_snapshot_rejects_scan_and_full_inventory_is_finance_only():
    context, _asset, qr = inventory_context("S8DRAFT")
    draft = _draft(context, "S8DRAFT-T")
    assert draft.status == "draft"
    assert draft.snapshot_at is None
    assert draft.expected_asset_count is None
    assert not InventoryTaskAsset.objects.filter(inventory_task=draft).exists()
    with pytest.raises(PermissionDenied):
        _scan(context, draft, qr, "S8DRAFT-scan")

    full_data = _task_data(
        context, "S8DRAFT-FULL", inventory_type="full",
        scope_type="company", scope_department=None,
    )
    with pytest.raises(PermissionDenied):
        create_inventory_task_draft(
            actor=context["equipment"], company=context["company"],
            data=full_data, assignee_users=[context["equipment"]],
        )
    full = create_inventory_task_draft(
        actor=context["finance"], company=context["company"],
        data=full_data, assignee_users=[context["equipment"]],
    )
    assert publish_inventory_task(actor=context["finance"], task=full).status == "in_progress"


def test_publish_creates_complete_immutable_snapshot_and_is_idempotent():
    context, asset, _qr = inventory_context("S8SNAP")
    task = _published(context, "S8SNAP-T")
    row = task.task_assets.get()
    assert task.expected_asset_count == 1
    assert row.asset_id == asset.pk
    assert row.expected_code_snapshot == asset.asset_code
    assert row.expected_name_snapshot == asset.asset_name
    assert row.expected_department_id == asset.department_id
    assert row.expected_employee_id == asset.responsible_employee_id
    assert row.expected_location_id == asset.location_id
    assert row.expected_asset_status == "in_use"
    assert context["location"].name in row.expected_location_path_snapshot
    assert publish_inventory_task(actor=context["finance"], task=task).pk == task.pk
    assert task.task_assets.count() == 1
    assert AuditLog.objects.filter(
        action="inventory.task_published", object_id=str(task.pk)
    ).count() == 1


@pytest.mark.parametrize(
    ("scope_type", "scope_field"),
    (
        ("category", "scope_category"),
        ("location", "scope_location"),
        ("selected_assets", "selected_asset_ids"),
    ),
)
def test_special_inventory_supports_each_non_department_scope(
    scope_type, scope_field
):
    context, asset, _qr = inventory_context(
        f"S8SCOPE{scope_type.replace('_', '')}"
    )
    overrides = {
        "inventory_type": "special",
        "scope_type": scope_type,
        "scope_department": None,
    }
    overrides[scope_field] = {
        "scope_category": context["category"],
        "scope_location": context["location"],
        "selected_asset_ids": [asset],
    }[scope_field]
    task = _published(context, f"S8SCOPE-{scope_type}", **overrides)
    assert task.expected_asset_count == 1
    assert task.task_assets.get().asset_id == asset.pk


def test_snapshot_stays_fixed_after_formal_assignment_change():
    from apps.assets.lifecycle_services import change_asset_assignment

    context, asset, _qr = inventory_context("S8SNAPMOVE")
    task = _published(context, "S8SNAPMOVE-T")
    row = task.task_assets.get()
    expected = (
        row.expected_department_id,
        row.expected_employee_id,
        row.expected_location_id,
    )
    department, employee, location = add_target_assignment(context, "S8SNAPMOVE-N")
    change_asset_assignment(
        actor=context["equipment"], asset=asset,
        to_department=department, to_responsible_employee=employee,
        to_location=location, effective_at=timezone.now() - timedelta(seconds=1),
        reason="盘点发布后调拨", idempotency_key="S8SNAPMOVE-move",
        expected_status="in_use", expected_department_id=asset.department_id,
        expected_responsible_employee_id=asset.responsible_employee_id,
        expected_location_id=asset.location_id,
    )
    row.refresh_from_db()
    assert (
        row.expected_department_id,
        row.expected_employee_id,
        row.expected_location_id,
    ) == expected


def test_scan_derives_all_results_keeps_dimension_evidence_and_rescan_history():
    context, asset, qr = inventory_context("S8DIFF")
    task = _published(context, "S8DIFF-T")
    _department, other_employee, other_location = add_target_assignment(
        context, "S8DIFF-N"
    )
    cases = [
        ("normal", {}),
        ("location_mismatch", {"actual_location": other_location}),
        ("responsible_mismatch", {"actual_employee": other_employee}),
        ("status_mismatch", {"actual_status": "idle"}),
        (
            "multiple_mismatch",
            {
                "actual_location": other_location,
                "actual_employee": other_employee,
                "actual_status": "idle",
            },
        ),
        ("other_mismatch", {"other_mismatch": True, "note": "外观标识异常"}),
    ]
    previous = None
    for index, (expected, overrides) in enumerate(cases):
        scan = _scan(context, task, qr, f"S8DIFF-{index}", **overrides)
        assert scan.result == expected
        assert scan.is_effective
        assert scan.supersedes_scan_id == (previous.pk if previous else None)
        if previous:
            previous.refresh_from_db()
            assert not previous.is_effective
        previous = scan

    assert InventoryScan.objects.filter(task_asset=task.task_assets.get()).count() == 6
    assert InventoryScan.objects.filter(task_asset=task.task_assets.get(), is_effective=True).count() == 1
    multi = InventoryScan.objects.get(result="multiple_mismatch")
    assert multi.actual_location_id == other_location.pk
    assert multi.actual_employee_id == other_employee.pk
    assert multi.actual_status == "idle"
    # Scan evidence never overwrites the current master record.
    asset.refresh_from_db()
    assert asset.location_id == context["location"].pk
    assert asset.responsible_employee_id == context["employee"].pk
    assert asset.asset_status == "in_use"
    assert "result" not in inspect.signature(scan_inventory_asset).parameters


def test_scan_idempotency_non_scope_invalid_token_and_revoked_assignee():
    context, _asset, qr = inventory_context("S8SCANSEC")
    assignee = make_user("s8-scan-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8SCANSEC-T", assignees=[assignee]),
    )
    first = _scan(
        context, task, qr, "S8SCANSEC-same", actor=assignee
    )
    replay = _scan(
        context, task, qr, "S8SCANSEC-same", actor=assignee
    )
    assert replay.pk == first.pk
    assert inventory_task_summary(task)["scanned"] == 1
    with pytest.raises(ValidationError, match="不同请求"):
        _scan(
            context, task, qr, "S8SCANSEC-same",
            actor=assignee, actual_status="idle",
        )

    _other_asset, other_qr = add_active_asset(context, "S8SCANSEC-OTHER")
    with pytest.raises(ValidationError, match="非本任务资产"):
        _scan(context, task, other_qr, "S8SCANSEC-other", actor=assignee)
    with pytest.raises(ValidationError, match="二维码无效"):
        scan_inventory_asset(
            actor=assignee, task=task, public_token="invalid-token",
            actual_location=context["location"], actual_employee=context["employee"],
            actual_status="in_use", idempotency_key="S8SCANSEC-invalid",
        )
    assignee.groups.clear()
    with pytest.raises(PermissionDenied):
        _scan(context, task, qr, "S8SCANSEC-revoked", actor=assignee)


def test_stop_scanning_marks_unscanned_missing_and_controlled_supplement_stays_reconciliation():
    context, first_asset, first_qr = inventory_context("S8SUPP")
    second_asset, second_qr = add_active_asset(context, "S8SUPP-SECOND")
    assignee = make_user("s8-supp-assignee", "warehouse")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8SUPP-T", assignees=[assignee]),
    )
    assert task.expected_asset_count == 2
    scan_inventory_asset(
        actor=assignee, task=task, qr_identity=first_qr,
        actual_location=first_asset.location,
        actual_employee=first_asset.responsible_employee,
        actual_status=first_asset.asset_status,
        idempotency_key="S8SUPP-first",
    )
    stopped = stop_inventory_scanning(
        actor=context["finance"], task=task, reason="进入差异处理",
        idempotency_key="S8SUPP-stop",
    )
    assert stopped.status == "reconciliation"
    missing = stopped.task_assets.get(asset=second_asset)
    assert missing.inventory_status == "missing"
    assert inventory_task_summary(stopped)["missing"] == 1
    with pytest.raises(PermissionDenied):
        scan_inventory_asset(
            actor=assignee, task=stopped, qr_identity=second_qr,
            actual_location=second_asset.location,
            actual_employee=second_asset.responsible_employee,
            actual_status=second_asset.asset_status,
            idempotency_key="S8SUPP-normal-after-stop",
        )
    with pytest.raises(PermissionDenied):
        supplemental_scan(
            actor=assignee, task_asset=missing, qr_identity=second_qr,
            actual_location=second_asset.location,
            actual_employee=second_asset.responsible_employee,
            actual_status=second_asset.asset_status,
            supplement_reason="普通执行人越权补盘",
            idempotency_key="S8SUPP-denied",
        )
    supplemental = supplemental_scan(
        actor=context["finance"], task_asset=missing, qr_identity=second_qr,
        actual_location=second_asset.location,
        actual_employee=second_asset.responsible_employee,
        actual_status=second_asset.asset_status,
        supplement_reason="经现场核实后补盘",
        idempotency_key="S8SUPP-ok",
    )
    stopped.refresh_from_db()
    missing.refresh_from_db()
    assert stopped.status == "reconciliation"
    assert supplemental.scan_mode == "supplemental"
    assert supplemental.is_effective
    assert missing.inventory_status == "normal"
    summary = inventory_task_summary(stopped)
    assert summary["scanned"] == 2
    assert summary["missing"] == 0
    assert AuditLog.objects.filter(action="inventory.scan_supplemented").count() == 1
