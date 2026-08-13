from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from apps.assets.models import AttachmentLink
from apps.inventory.models import (
    InventoryResolution,
    InventoryScan,
    InventorySurplus,
    InventoryTaskAsset,
)
from apps.inventory.services import publish_inventory_task
from apps.inventory.services import (
    cancel_inventory_task,
    close_inventory_task,
    create_inventory_surplus,
    resolve_inventory_surplus,
    scan_inventory_asset,
    stop_inventory_scanning,
)
from tests.test_sprint3_support import direct_attachment, make_company, make_user
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL trigger/constraint acceptance")


def _enable_fixture_guard(*names):
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        for name in names:
            cursor.execute(
                "SELECT set_config(%s, 'on', true)",
                [f"eam_lite.controlled_inventory_{name}_mutation"],
            )


def _published_fixture(prefix):
    context, asset, _qr = inventory_context(prefix)
    task = publish_inventory_task(
        actor=context["finance"], task=_draft(context, f"{prefix}-T")
    )
    row = task.task_assets.get(asset=asset)
    return context, asset, task, row


def test_database_unique_current_scan_and_resolution_constraints():
    context, asset, task, row = _published_fixture("S8DBUNIQUE")
    values = dict(
        company=context["company"], inventory_task=task, task_asset=row,
        asset=asset, scan_mode="normal", scanned_by=context["equipment"],
        scanned_at=timezone.now(), actual_location=asset.location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status, result="normal", is_effective=True,
    )
    with transaction.atomic():
        _enable_fixture_guard("scan")
        InventoryScan._base_manager.create(
            **values, idempotency_key="S8DBUNIQUE-scan-a"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            InventoryScan._base_manager.create(
                **values, idempotency_key="S8DBUNIQUE-scan-b"
            )

    resolution_values = dict(
        company=context["company"], inventory_task_asset=row,
        resolution_type="master_confirmed", conclusion="主档无误",
        status="active", resolved_by=context["finance"],
        resolved_at=timezone.now(),
    )
    with transaction.atomic():
        _enable_fixture_guard("resolution")
        InventoryResolution._base_manager.create(
            **resolution_values, idempotency_key="S8DBUNIQUE-res-a"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            InventoryResolution._base_manager.create(
                **resolution_values, idempotency_key="S8DBUNIQUE-res-b"
            )


def test_postgresql_rejects_raw_snapshot_scan_resolution_and_surplus_mutation():
    _postgresql_only()
    context, asset, task, row = _published_fixture("S8DBGUARD")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            "UPDATE inventory_inventorytaskasset "
            "SET expected_name_snapshot=%s WHERE id=%s",
            ["被篡改", row.pk],
        )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("DELETE FROM inventory_inventorytaskasset WHERE id=%s", [row.pk])

    with transaction.atomic():
        _enable_fixture_guard("scan")
        scan = InventoryScan._base_manager.create(
            company=context["company"], inventory_task=task, task_asset=row,
            asset=asset, scan_mode="normal", scanned_by=context["equipment"],
            scanned_at=timezone.now(), actual_location=asset.location,
            actual_employee=asset.responsible_employee,
            actual_status=asset.asset_status, result="normal", is_effective=True,
            idempotency_key="S8DBGUARD-scan",
        )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("DELETE FROM inventory_inventoryscan WHERE id=%s", [scan.pk])

    # Even internal code holding the publish capability cannot append a new
    # immutable snapshot after the task has left in_progress.
    stopped = stop_inventory_scanning(
        actor=context["finance"], task=task, reason="冻结快照后验证数据库门禁",
        idempotency_key="S8DBGUARD-stop",
    )
    with pytest.raises((DatabaseError, IntegrityError)), transaction.atomic():
        _enable_fixture_guard("task")
        InventoryTaskAsset._base_manager.create(
            company=context["company"], inventory_task=stopped, asset=asset,
            expected_department=asset.department,
            expected_employee=asset.responsible_employee,
            expected_location=asset.location,
            expected_asset_status=asset.asset_status,
            expected_code_snapshot=f"{asset.asset_code}-duplicate",
            expected_name_snapshot=asset.asset_name,
            expected_category_snapshot=asset.category.name,
            expected_department_snapshot=asset.department.name,
            expected_employee_snapshot=asset.responsible_employee.name,
            expected_location_path_snapshot=asset.location.name,
        )


def test_attachment_has_exactly_one_real_inventory_target_and_same_company():
    _postgresql_only()
    context, asset, task, row = _published_fixture("S8DBATT")
    surplus = InventorySurplus._base_manager.create(
        company=context["company"], inventory_task=task,
        temporary_name="现场未知设备", temporary_category_text="设备",
        temporary_location_text="一号车间", found_by=context["equipment"],
        found_at=timezone.now(), idempotency_key="S8DBATT-surplus",
    )
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/inventory/S8DBATT.jpg", filename="S8DBATT.jpg",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=context["company"], attachment=attachment,
            asset=asset, inventory_surplus=surplus,
            role="surplus_evidence", security_class="A0",
            created_by=context["equipment"],
        )

    other_company = make_company("S8DBATT-OTHER", active=False)
    other_attachment = direct_attachment(
        other_company, context["equipment"],
        key="private/inventory/S8DBATT-other.jpg",
        filename="S8DBATT-other.jpg",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=context["company"], attachment=other_attachment,
            inventory_surplus=surplus, role="surplus_evidence",
            security_class="A0", created_by=context["equipment"],
        )


def test_actor_deletion_preserves_terminal_task_and_resolved_surplus_history():
    context, asset, qr = inventory_context("S8DBACTORNULL")
    historical_actor = make_user(
        "s8-db-actor-null", "finance", "equipment"
    )

    reconciliation = publish_inventory_task(
        actor=historical_actor,
        task=_draft(context, "S8DBACTORNULL-RECONCILIATION"),
    )
    reconciliation = stop_inventory_scanning(
        actor=historical_actor, task=reconciliation,
        reason="验证停止人删除后历史合法",
        idempotency_key="S8DBACTORNULL-stop",
    )

    closed = publish_inventory_task(
        actor=historical_actor, task=_draft(context, "S8DBACTORNULL-CLOSED")
    )
    scan = scan_inventory_asset(
        actor=historical_actor, task=closed, qr_identity=qr,
        actual_location=asset.location,
        actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,
        idempotency_key="S8DBACTORNULL-scan",
    )
    closed = stop_inventory_scanning(
        actor=historical_actor, task=closed, reason="准备关闭",
        idempotency_key="S8DBACTORNULL-closed-stop",
    )
    closed = close_inventory_task(
        actor=historical_actor, task=closed,
        idempotency_key="S8DBACTORNULL-close",
    )

    cancelled = publish_inventory_task(
        actor=historical_actor, task=_draft(context, "S8DBACTORNULL-CANCELLED")
    )
    cancelled = cancel_inventory_task(
        actor=historical_actor, task=cancelled,
        reason="验证取消人删除后历史合法",
        idempotency_key="S8DBACTORNULL-cancel",
    )

    surplus_task = publish_inventory_task(
        actor=historical_actor, task=_draft(context, "S8DBACTORNULL-SURPLUS")
    )
    surplus = create_inventory_surplus(
        actor=historical_actor, task=surplus_task,
        temporary_name="历史盘盈", temporary_category_text="工装",
        temporary_location_text="现场",
        idempotency_key="S8DBACTORNULL-surplus",
    )
    attachment = direct_attachment(
        context["company"], historical_actor,
        key="private/inventory/S8DBACTORNULL.jpg", filename="历史盘盈.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        inventory_surplus=surplus, role="surplus_evidence",
        security_class="A0", created_by=historical_actor,
    )
    surplus_task = stop_inventory_scanning(
        actor=historical_actor, task=surplus_task, reason="处理盘盈",
        idempotency_key="S8DBACTORNULL-surplus-stop",
    )
    surplus = resolve_inventory_surplus(
        actor=historical_actor, surplus=surplus,
        resolution_status="other", remark="保留历史结论",
        idempotency_key="S8DBACTORNULL-surplus-resolve",
    )

    get_user_model().objects.get(pk=historical_actor.pk).delete()

    for task in (reconciliation, closed, cancelled, surplus_task):
        task.refresh_from_db()
    scan.refresh_from_db()
    surplus.refresh_from_db()
    assert reconciliation.status == "reconciliation"
    assert reconciliation.scanning_stopped_by_id is None
    assert closed.status == "closed"
    assert closed.scanning_stopped_by_id is None and closed.closed_by_id is None
    assert cancelled.status == "cancelled" and cancelled.cancelled_by_id is None
    assert cancelled.cancellation_reason
    assert surplus.resolution_status == "other"
    assert surplus.found_by_id is None and surplus.resolved_by_id is None
    assert surplus.remark == "保留历史结论"
    assert scan.scanned_by_id is None
