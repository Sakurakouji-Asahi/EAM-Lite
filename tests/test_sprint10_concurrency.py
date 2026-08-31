from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection
from django.utils import timezone

from apps.accounts.models import User
from apps.assets.models import AssetMovement
from apps.audit.models import AuditLog
from apps.masterdata.models import Employee
from apps.masterdata.services import update_employee
from apps.offboarding.models import (
    EmployeeAssetClearance,
    EmployeeAssetClearanceItem,
)
from apps.offboarding.services import (
    complete_clearance,
    initiate_clearance,
    transfer_clearance_item,
)
from tests.test_sprint10_support import (
    additional_employee,
    formal_asset,
    offboarding_context,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 10 row-lock concurrency requires PostgreSQL 18.6")


def test_concurrent_initiation_creates_one_clearance_and_one_snapshot_set():
    _postgresql_only()
    context = offboarding_context("S10CONINIT")
    formal_asset(context, "S10CONINIT-A")
    employee_id = context["employee"].pk
    actor_id = context["hr"].pk
    barrier = Barrier(2)

    def worker(key):
        close_old_connections()
        try:
            employee = Employee.objects.get(pk=employee_id)
            actor = User.objects.get(pk=actor_id)
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                result = initiate_clearance(
                    actor=actor,
                    employee=employee,
                    idempotency_key=key,
                    remark="并发发起同一员工",
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", str(result.pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(worker, ("S10CONINIT-A", "S10CONINIT-B"))
        )

    assert [kind for kind, _ in results] == ["ok", "ok"]
    assert len({value for _, value in results}) == 1
    clearance = EmployeeAssetClearance.objects.get(employee_id=employee_id)
    assert clearance.items.count() == 1
    assert (clearance.total_assets_snapshot, clearance.unresolved_assets) == (1, 1)
    employee = Employee.objects.get(pk=employee_id)
    assert employee.employment_status == "leaving"
    assert AuditLog.objects.filter(
        action="employee_offboarding.initiated",
        object_id=str(clearance.pk),
    ).count() == 1


def test_two_handlers_cannot_both_transfer_the_same_pending_item():
    _postgresql_only()
    context = offboarding_context("S10CONITEM")
    asset, _ = formal_asset(context, "S10CONITEM-A")
    receiver_a = additional_employee(context, "S10CONITEM-RA")
    receiver_b = additional_employee(context, "S10CONITEM-RB")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10CONITEM-init",
    )
    item_id = clearance.items.get().pk
    actor_id = context["equipment"].pk
    barrier = Barrier(2)

    def worker(receiver_id, key):
        close_old_connections()
        try:
            item = EmployeeAssetClearanceItem.objects.get(pk=item_id)
            actor = User.objects.get(pk=actor_id)
            receiver = Employee.objects.select_related("department").get(
                pk=receiver_id
            )
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                result = transfer_clearance_item(
                    actor=actor,
                    item=item,
                    to_department=receiver.department,
                    to_responsible_employee=receiver,
                    to_location=context["location"],
                    effective_at=timezone.now(),
                    reason="并发处理同一清退项",
                    idempotency_key=key,
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", str(result.pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(worker, receiver_a.pk, "S10CONITEM-A"),
            pool.submit(worker, receiver_b.pk, "S10CONITEM-B"),
        )
        results = [future.result(timeout=45) for future in futures]

    assert sorted(kind for kind, _ in results) == ["ok", "validation"]
    transfers = AssetMovement.objects.filter(
        asset=asset,
        movement_type="transfer",
        from_employee=context["employee"],
    )
    assert transfers.count() == 1
    item = EmployeeAssetClearanceItem.objects.get(pk=item_id)
    clearance.refresh_from_db()
    assert item.resolution == "transferred"
    assert item.movement_id == transfers.get().pk
    assert clearance.unresolved_assets == 0


def test_final_transfer_racing_completion_never_commits_false_completion_or_deadlock():
    _postgresql_only()
    context = offboarding_context("S10CONDONE")
    asset, _ = formal_asset(context, "S10CONDONE-A")
    receiver = additional_employee(context, "S10CONDONE-R")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10CONDONE-init",
    )
    item_id = clearance.items.get().pk
    clearance_id = clearance.pk
    equipment_id = context["equipment"].pk
    hr_id = context["hr"].pk
    receiver_id = receiver.pk
    barrier = Barrier(2)

    def transfer_worker():
        close_old_connections()
        try:
            actor = User.objects.get(pk=equipment_id)
            item = EmployeeAssetClearanceItem.objects.get(pk=item_id)
            target = Employee.objects.select_related("department").get(
                pk=receiver_id
            )
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                transfer_clearance_item(
                    actor=actor,
                    item=item,
                    to_department=target.department,
                    to_responsible_employee=target,
                    to_location=context["location"],
                    effective_at=timezone.now(),
                    reason="最后一项转交",
                    idempotency_key="S10CONDONE-transfer",
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", None)
        finally:
            close_old_connections()

    def complete_worker():
        close_old_connections()
        try:
            actor = User.objects.get(pk=hr_id)
            target = EmployeeAssetClearance.objects.get(pk=clearance_id)
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                complete_clearance(
                    actor=actor,
                    clearance=target,
                    termination_date=timezone.localdate(),
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", None)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        transfer_future = pool.submit(transfer_worker)
        complete_future = pool.submit(complete_worker)
        results = [
            transfer_future.result(timeout=45),
            complete_future.result(timeout=45),
        ]

    assert "database" not in {kind for kind, _ in results}
    assert results[0][0] == "ok"
    clearance = EmployeeAssetClearance.objects.get(pk=clearance_id)
    item = EmployeeAssetClearanceItem.objects.get(pk=item_id)
    employee = Employee.objects.get(pk=context["employee"].pk)
    asset.refresh_from_db()
    assert item.resolution == "transferred"
    assert clearance.unresolved_assets == 0
    assert asset.responsible_employee_id == receiver_id
    if clearance.status == "completed":
        assert employee.employment_status == "resigned"
        assert employee.termination_date == timezone.localdate()
    else:
        assert clearance.status == "open"
        assert employee.employment_status == "leaving"


def test_update_employee_racing_initiation_has_consistent_lock_order():
    _postgresql_only()
    context = offboarding_context("S10CONEMPINIT")
    employee_id = context["employee"].pk
    actor_id = context["hr"].pk
    barrier = Barrier(2)

    def update_worker():
        close_old_connections()
        try:
            employee = Employee.objects.get(pk=employee_id)
            actor = User.objects.get(pk=actor_id)
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                updated = update_employee(
                    actor=actor,
                    employee=employee,
                    data={"remark": "并发离职发起前的普通资料更新"},
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", updated.remark)
        finally:
            close_old_connections()

    def initiate_worker():
        close_old_connections()
        try:
            employee = Employee.objects.get(pk=employee_id)
            actor = User.objects.get(pk=actor_id)
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                clearance = initiate_clearance(
                    actor=actor,
                    employee=employee,
                    idempotency_key="S10CONEMPINIT-init",
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", str(clearance.pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(update_worker), pool.submit(initiate_worker))
        results = [future.result(timeout=45) for future in futures]

    assert "database" not in {kind for kind, _ in results}
    assert results[1][0] == "ok"
    assert results[0][0] in {"ok", "validation"}
    employee = Employee.objects.get(pk=employee_id)
    clearance = EmployeeAssetClearance.objects.get(employee_id=employee_id)
    assert employee.employment_status == "leaving"
    assert not employee.is_active
    if results[0][0] == "ok":
        assert employee.remark == "并发离职发起前的普通资料更新"
    assert clearance.status == "open"
    assert clearance.unresolved_assets == 0


def test_update_employee_racing_initial_completion_has_consistent_lock_order():
    _postgresql_only()
    context = offboarding_context("S10CONEMPDONE")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10CONEMPDONE-init",
    )
    clearance_id = clearance.pk
    employee_id = context["employee"].pk
    actor_id = context["hr"].pk
    barrier = Barrier(2)

    def update_worker():
        close_old_connections()
        try:
            employee = Employee.objects.get(pk=employee_id)
            actor = User.objects.get(pk=actor_id)
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                updated = update_employee(
                    actor=actor,
                    employee=employee,
                    data={"remark": "并发完成清退前的普通资料更新"},
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", updated.remark)
        finally:
            close_old_connections()

    def complete_worker():
        close_old_connections()
        try:
            target = EmployeeAssetClearance.objects.get(pk=clearance_id)
            actor = User.objects.get(pk=actor_id)
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", None)
            try:
                completed = complete_clearance(
                    actor=actor,
                    clearance=target,
                    termination_date=timezone.localdate(),
                )
            except ValidationError as exc:
                return ("validation", ";".join(exc.messages))
            except DatabaseError as exc:
                return ("database", type(exc).__name__)
            return ("ok", str(completed.pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(update_worker), pool.submit(complete_worker))
        results = [future.result(timeout=45) for future in futures]

    assert "database" not in {kind for kind, _ in results}
    assert results[1][0] == "ok"
    assert results[0][0] in {"ok", "validation"}
    employee = Employee.objects.get(pk=employee_id)
    clearance.refresh_from_db()
    assert employee.employment_status == "resigned"
    assert not employee.is_active
    assert employee.termination_date == timezone.localdate()
    if results[0][0] == "ok":
        assert employee.remark == "并发完成清退前的普通资料更新"
    assert clearance.status == "completed"
    assert clearance.unresolved_assets == 0
