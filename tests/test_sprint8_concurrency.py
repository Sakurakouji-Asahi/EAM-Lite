from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection

from apps.accounts.models import User
from apps.inventory.models import (
    InventoryResolution,
    InventoryScan,
    InventoryTask,
    InventoryTaskAsset,
)
from apps.inventory.services import (
    close_inventory_task,
    correct_inventory_resolution,
    create_inventory_task_draft,
    publish_inventory_task,
    resolve_inventory_difference,
    scan_inventory_asset,
    stop_inventory_scanning,
    supplemental_scan,
)
from tests.test_sprint7_support import add_target_assignment
from tests.test_sprint8_services import _draft, _task_data
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency acceptance")


def _run_two(worker):
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(worker, range(2)))


def test_postgresql_concurrent_publish_creates_one_complete_snapshot_set():
    _postgresql_only()
    context, asset, _qr = inventory_context("S8CONPUB")
    task = _draft(context, "S8CONPUB-T")
    task_id, actor_id = task.pk, context["finance"].pk
    barrier = Barrier(2)

    def worker(_):
        close_old_connections()
        try:
            barrier.wait()
            result = publish_inventory_task(
                actor=User.objects.get(pk=actor_id),
                task=InventoryTask.objects.get(pk=task_id),
            )
            return str(result.pk)
        finally:
            close_old_connections()

    assert len(set(_run_two(worker))) == 1
    task.refresh_from_db()
    assert task.status == "in_progress"
    assert task.expected_asset_count == 1
    assert InventoryTaskAsset.objects.filter(
        inventory_task=task, asset=asset
    ).count() == 1


def test_postgresql_concurrent_task_create_idempotency_is_serialized_by_company():
    _postgresql_only()
    context, _asset, _qr = inventory_context("S8CONCREATE")
    base_data = _task_data(context, "S8CONCREATE-T")
    ids = {
        "actor": context["finance"].pk,
        "company": context["company"].pk,
        "department": context["department"].pk,
        "assignee": context["equipment"].pk,
    }
    barrier = Barrier(2)

    def worker(_):
        from apps.masterdata.models import Company, Department

        close_old_connections()
        try:
            barrier.wait()
            data = dict(base_data)
            data["scope_department"] = Department.objects.get(
                pk=ids["department"]
            )
            task = create_inventory_task_draft(
                actor=User.objects.get(pk=ids["actor"]),
                company=Company.objects.get(pk=ids["company"]),
                data=data,
                assignee_users=[User.objects.get(pk=ids["assignee"])],
            )
            return str(task.pk)
        finally:
            close_old_connections()

    assert len(set(_run_two(worker))) == 1
    assert InventoryTask.objects.filter(
        company_id=ids["company"],
        idempotency_key=base_data["idempotency_key"],
    ).count() == 1

    conflict_data = dict(
        base_data,
        task_code="S8CONCREATE-CONFLICT",
        idempotency_key="S8CONCREATE-conflict-key",
    )
    barrier = Barrier(2)

    def conflicting_worker(index):
        from apps.masterdata.models import Company, Department

        close_old_connections()
        try:
            barrier.wait()
            data = dict(
                conflict_data,
                name=f"并发不同任务名 {index}",
                scope_department=Department.objects.get(pk=ids["department"]),
            )
            try:
                task = create_inventory_task_draft(
                    actor=User.objects.get(pk=ids["actor"]),
                    company=Company.objects.get(pk=ids["company"]),
                    data=data,
                    assignee_users=[User.objects.get(pk=ids["assignee"])],
                )
                return ("created", str(task.pk))
            except ValidationError:
                return ("rejected", "")
        finally:
            close_old_connections()

    assert sorted(kind for kind, _ in _run_two(conflicting_worker)) == [
        "created", "rejected"
    ]
    assert InventoryTask.objects.filter(
        company_id=ids["company"],
        idempotency_key=conflict_data["idempotency_key"],
    ).count() == 1


def test_postgresql_concurrent_same_key_scan_is_one_event():
    _postgresql_only()
    context, asset, qr = inventory_context("S8CONSCAN")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8CONSCAN-T")
    )
    ids = {
        "actor": context["equipment"].pk,
        "task": task.pk,
        "qr": qr.pk,
        "location": asset.location_id,
        "employee": asset.responsible_employee_id,
    }
    barrier = Barrier(2)

    def worker(_):
        from apps.assets.models import AssetQrIdentity
        from apps.masterdata.models import Employee, Location

        close_old_connections()
        try:
            barrier.wait()
            scan = scan_inventory_asset(
                actor=User.objects.get(pk=ids["actor"]),
                task=InventoryTask.objects.get(pk=ids["task"]),
                qr_identity=AssetQrIdentity.objects.get(pk=ids["qr"]),
                actual_location=Location.objects.get(pk=ids["location"]),
                actual_employee=Employee.objects.get(pk=ids["employee"]),
                actual_status="in_use", idempotency_key="S8CONSCAN-same",
            )
            return str(scan.pk)
        finally:
            close_old_connections()

    assert len(set(_run_two(worker))) == 1
    assert task.scans.count() == 1
    assert task.scans.filter(is_effective=True).count() == 1


def test_postgresql_concurrent_rescans_keep_history_and_one_effective_result():
    _postgresql_only()
    context, asset, qr = inventory_context("S8CONRESCAN")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8CONRESCAN-T")
    )
    ids = {
        "actor": context["equipment"].pk,
        "task": task.pk,
        "qr": qr.pk,
        "location": asset.location_id,
        "employee": asset.responsible_employee_id,
    }
    barrier = Barrier(2)

    def worker(index):
        from apps.assets.models import AssetQrIdentity
        from apps.masterdata.models import Employee, Location

        close_old_connections()
        try:
            barrier.wait()
            scan = scan_inventory_asset(
                actor=User.objects.get(pk=ids["actor"]),
                task=InventoryTask.objects.get(pk=ids["task"]),
                qr_identity=AssetQrIdentity.objects.get(pk=ids["qr"]),
                actual_location=Location.objects.get(pk=ids["location"]),
                actual_employee=Employee.objects.get(pk=ids["employee"]),
                actual_status="in_use",
                idempotency_key=f"S8CONRESCAN-{index}",
            )
            return str(scan.pk)
        finally:
            close_old_connections()

    assert len(set(_run_two(worker))) == 2
    scans = InventoryScan.objects.filter(inventory_task=task)
    assert scans.count() == 2
    assert scans.filter(is_effective=True).count() == 1
    current = scans.get(is_effective=True)
    assert current.supersedes_scan_id is not None


def test_postgresql_stop_and_scan_race_produces_one_consistent_state():
    _postgresql_only()
    context, asset, qr = inventory_context("S8CONSTOP")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8CONSTOP-T")
    )
    ids = {
        "finance": context["finance"].pk,
        "equipment": context["equipment"].pk,
        "task": task.pk,
        "qr": qr.pk,
        "location": asset.location_id,
        "employee": asset.responsible_employee_id,
    }
    barrier = Barrier(2)

    def worker(index):
        from apps.assets.models import AssetQrIdentity
        from apps.masterdata.models import Employee, Location

        close_old_connections()
        try:
            barrier.wait()
            locked_task = InventoryTask.objects.get(pk=ids["task"])
            try:
                if index == 0:
                    stop_inventory_scanning(
                        actor=User.objects.get(pk=ids["finance"]),
                        task=locked_task, reason="并发停止扫码",
                        idempotency_key="S8CONSTOP-stop",
                    )
                    return "stopped"
                scan_inventory_asset(
                    actor=User.objects.get(pk=ids["equipment"]),
                    task=locked_task,
                    qr_identity=AssetQrIdentity.objects.get(pk=ids["qr"]),
                    actual_location=Location.objects.get(pk=ids["location"]),
                    actual_employee=Employee.objects.get(pk=ids["employee"]),
                    actual_status="in_use", idempotency_key="S8CONSTOP-scan",
                )
                return "scanned"
            except (PermissionDenied, ValidationError):
                return "rejected"
        finally:
            close_old_connections()

    results = _run_two(worker)
    task.refresh_from_db()
    row = task.task_assets.get()
    assert task.status == "reconciliation"
    if task.scans.filter(is_effective=True).exists():
        assert row.inventory_status == "normal"
        assert "scanned" in results
    else:
        assert row.inventory_status == "missing"
        assert "rejected" in results


def test_postgresql_supplement_and_close_race_cannot_close_unresolved_result():
    _postgresql_only()
    context, asset, qr = inventory_context("S8CONCLOSE")
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8CONCLOSE-T")
    )
    task = stop_inventory_scanning(
        actor=context["finance"], task=task, reason="准备并发补盘",
        idempotency_key="S8CONCLOSE-stop",
    )
    row = task.task_assets.get()
    ids = {
        "finance": context["finance"].pk,
        "task": task.pk,
        "row": row.pk,
        "qr": qr.pk,
        "location": asset.location_id,
        "employee": asset.responsible_employee_id,
    }
    barrier = Barrier(2)

    def worker(index):
        from apps.assets.models import AssetQrIdentity
        from apps.inventory.models import InventoryTaskAsset
        from apps.masterdata.models import Employee, Location

        close_old_connections()
        try:
            barrier.wait()
            actor = User.objects.get(pk=ids["finance"])
            try:
                if index == 0:
                    scan = supplemental_scan(
                        actor=actor,
                        task_asset=InventoryTaskAsset.objects.get(pk=ids["row"]),
                        qr_identity=AssetQrIdentity.objects.get(pk=ids["qr"]),
                        actual_location=Location.objects.get(pk=ids["location"]),
                        actual_employee=Employee.objects.get(pk=ids["employee"]),
                        actual_status="in_use", supplement_reason="并发受控补盘",
                        idempotency_key="S8CONCLOSE-supp",
                    )
                    return ("supplemented", str(scan.pk))
                closed = close_inventory_task(
                    actor=actor, task=InventoryTask.objects.get(pk=ids["task"]),
                    idempotency_key="S8CONCLOSE-close",
                )
                return ("closed", str(closed.pk))
            except (PermissionDenied, ValidationError) as exc:
                return ("rejected", str(exc))
        finally:
            close_old_connections()

    results = _run_two(worker)
    task.refresh_from_db()
    if task.status == "closed":
        assert task.scans.filter(scan_mode="supplemental", is_effective=True).count() == 1
    else:
        assert task.status == "reconciliation"
    assert any(kind == "supplemented" for kind, _ in results)


def test_postgresql_supplement_and_resolution_race_has_only_one_outcome():
    _postgresql_only()
    context, asset, qr = inventory_context("S8CONSUPRES")
    task = stop_inventory_scanning(
        actor=context["finance"],
        task=publish_inventory_task(
            actor=context["finance"], task=_draft(context, "S8CONSUPRES-T")
        ),
        reason="并发补盘与结论测试",
        idempotency_key="S8CONSUPRES-stop",
    )
    row = task.task_assets.get()
    ids = {
        "actor": context["finance"].pk,
        "row": row.pk,
        "qr": qr.pk,
        "location": asset.location_id,
        "employee": asset.responsible_employee_id,
    }
    barrier = Barrier(2)

    def worker(index):
        from apps.assets.models import AssetQrIdentity
        from apps.masterdata.models import Employee, Location

        close_old_connections()
        try:
            barrier.wait()
            actor = User.objects.get(pk=ids["actor"])
            try:
                if index == 0:
                    result = supplemental_scan(
                        actor=actor,
                        task_asset=InventoryTaskAsset.objects.get(pk=ids["row"]),
                        qr_identity=AssetQrIdentity.objects.get(pk=ids["qr"]),
                        actual_location=Location.objects.get(pk=ids["location"]),
                        actual_employee=Employee.objects.get(pk=ids["employee"]),
                        actual_status="in_use",
                        supplement_reason="现场重新扫码",
                        idempotency_key="S8CONSUPRES-supplement",
                    )
                    return ("supplemented", str(result.pk))
                result = resolve_inventory_difference(
                    actor=actor,
                    task_asset=InventoryTaskAsset.objects.get(pk=ids["row"]),
                    resolution_type="master_confirmed",
                    conclusion="确认未盘但保留主档",
                    idempotency_key="S8CONSUPRES-resolution",
                )
                return ("resolved", str(result.pk))
            except (PermissionDenied, ValidationError):
                return ("rejected", "")
        finally:
            close_old_connections()

    results = _run_two(worker)
    assert sorted(kind for kind, _ in results).count("rejected") == 1
    assert (
        InventoryScan.objects.filter(
            task_asset_id=ids["row"], is_effective=True
        ).count()
        + InventoryResolution.objects.filter(
            inventory_task_asset_id=ids["row"], status="active"
        ).count()
        == 1
    )


def test_postgresql_concurrent_closed_resolution_corrections_keep_one_active():
    _postgresql_only()
    context, asset, qr = inventory_context("S8CONCORRECT")
    _department, _employee, other_location = add_target_assignment(
        context, "S8CONCORRECT-N"
    )
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, "S8CONCORRECT-T")
    )
    scan_inventory_asset(
        actor=context["equipment"], task=task, qr_identity=qr,
        actual_location=other_location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,
        idempotency_key="S8CONCORRECT-scan",
    )
    task = stop_inventory_scanning(
        actor=context["finance"], task=task, reason="并发更正准备",
        idempotency_key="S8CONCORRECT-stop",
    )
    original = resolve_inventory_difference(
        actor=context["finance"], task_asset=task.task_assets.get(),
        resolution_type="master_confirmed", conclusion="初次处理结论",
        idempotency_key="S8CONCORRECT-original",
    )
    close_inventory_task(
        actor=context["finance"], task=task,
        idempotency_key="S8CONCORRECT-close",
    )
    ids = {"actor": context["finance"].pk, "resolution": original.pk}
    barrier = Barrier(2)

    def worker(index):
        close_old_connections()
        try:
            barrier.wait()
            try:
                corrected = correct_inventory_resolution(
                    actor=User.objects.get(pk=ids["actor"]),
                    resolution=InventoryResolution.objects.get(
                        pk=ids["resolution"]
                    ),
                    resolution_type="other",
                    conclusion=f"并发更正结论 {index}",
                    correction_reason=f"并发复核原因 {index}",
                    idempotency_key=f"S8CONCORRECT-{index}",
                )
                return ("corrected", str(corrected.pk))
            except (PermissionDenied, ValidationError):
                return ("rejected", "")
        finally:
            close_old_connections()

    results = _run_two(worker)
    original.refresh_from_db()
    chain = InventoryResolution.objects.filter(
        inventory_task_asset=original.inventory_task_asset
    )
    assert sorted(kind for kind, _ in results) == ["corrected", "rejected"]
    assert original.status == "superseded"
    assert chain.count() == 2
    assert chain.filter(status="active").count() == 1
    assert chain.filter(supersedes_resolution=original).count() == 1
