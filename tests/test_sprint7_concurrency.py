from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.assets.lifecycle_services import (
    change_asset_assignment,
    loan_asset,
    return_loan,
)
from apps.assets.models import Asset, AssetLoan, AssetMovement
from apps.masterdata.models import Department, Employee, Location
from tests.test_sprint7_support import active_asset_context, add_target_assignment


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency acceptance")


def test_postgresql_concurrent_stale_assignment_has_one_winner_and_no_lost_history():
    _postgresql_only()
    context, asset, _qr = active_asset_context("S7CONMOVE")
    target_a = add_target_assignment(context, "S7CONMOVE-A")
    target_b = add_target_assignment(context, "S7CONMOVE-B")
    actor_id = context["equipment"].pk
    asset_id = asset.pk
    expected = (
        asset.department_id,
        asset.responsible_employee_id,
        asset.location_id,
    )
    effective_at = timezone.now()
    barrier = Barrier(2)

    def worker(item):
        close_old_connections()
        try:
            actor = type(context["equipment"]).objects.get(pk=actor_id)
            target, key = item
            department_id, employee_id, location_id = (
                obj.pk for obj in target
            )
            barrier.wait()
            try:
                movement = change_asset_assignment(
                    actor=actor, asset=Asset.objects.get(pk=asset_id),
                    to_department=Department.objects.get(pk=department_id),
                    to_responsible_employee=Employee.objects.get(pk=employee_id),
                    to_location=Location.objects.get(pk=location_id),
                    effective_at=effective_at, reason="并发调拨",
                    idempotency_key=key, expected_status="in_use",
                    expected_department_id=expected[0],
                    expected_responsible_employee_id=expected[1],
                    expected_location_id=expected[2],
                )
                return ("ok", str(movement.pk))
            except ValidationError as exc:
                return ("conflict", str(exc))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            worker,
            ((target_a, "S7CONMOVE-a"), (target_b, "S7CONMOVE-b")),
        ))

    assert sorted(kind for kind, _detail in results) == ["conflict", "ok"]
    asset.refresh_from_db()
    movement = AssetMovement.objects.get(asset=asset, movement_type="transfer")
    assert (
        movement.from_department_id,
        movement.from_employee_id,
        movement.from_location_id,
    ) == expected
    assert (
        asset.department_id,
        asset.responsible_employee_id,
        asset.location_id,
    ) == (
        movement.to_department_id,
        movement.to_employee_id,
        movement.to_location_id,
    )


def test_postgresql_two_simultaneous_returns_create_one_return_movement():
    _postgresql_only()
    context, asset, _qr = active_asset_context("S7CONRETURN")
    loan = loan_asset(
        actor=context["equipment"], asset=asset,
        borrower_type="internal_employee",
        borrower_employee=context["employee"],
        loan_date=timezone.localdate(),
        expected_return_date=timezone.localdate() + timedelta(days=7),
        handled_by=context["equipment"], reason="并发归还测试借出",
        idempotency_key="S7CONRETURN-loan", expected_status="in_use",
    )
    actor_id = context["equipment"].pk
    loan_id = loan.pk
    return_ids = (
        context["employee"].pk,
        context["department"].pk,
        context["location"].pk,
    )
    returned_at = timezone.now()
    barrier = Barrier(2)

    def worker(key):
        close_old_connections()
        try:
            actor = type(context["equipment"]).objects.get(pk=actor_id)
            barrier.wait()
            try:
                result = return_loan(
                    actor=actor,
                    loan=AssetLoan.objects.get(pk=loan_id),
                    returned_at=returned_at,
                    received_by_employee=Employee.objects.get(pk=return_ids[0]),
                    return_department=Department.objects.get(pk=return_ids[1]),
                    return_responsible_employee=Employee.objects.get(pk=return_ids[0]),
                    return_location=Location.objects.get(pk=return_ids[2]),
                    return_asset_status="in_use", idempotency_key=key,
                )
                return ("ok", str(result.pk))
            except ValidationError as exc:
                return ("conflict", str(exc))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ("S7CONRETURN-a", "S7CONRETURN-b")))

    assert sorted(kind for kind, _detail in results) == ["conflict", "ok"]
    asset.refresh_from_db()
    loan.refresh_from_db()
    assert asset.asset_status == "in_use"
    assert loan.status == "returned"
    assert AssetMovement.objects.filter(
        asset=asset, movement_type="loan_return"
    ).count() == 1
    assert loan.return_movement_id is not None
