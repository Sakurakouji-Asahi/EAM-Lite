from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import (
    change_asset_assignment,
    set_asset_idle,
)
from apps.assets.models import (
    AssetLabelAttachmentRequest,
    AssetMovement,
)
from apps.assets.qr_services import (
    confirm_label_attachment,
    confirm_print_batch,
    generate_print_batch,
    rotate_qr_identity,
)
from apps.maintenance.models import MaintenanceRecord
from apps.maintenance.services import complete_maintenance, void_maintenance_record
from tests.test_sprint7_support import active_asset_context, add_target_assignment
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def _remove_legacy_attachment_request(idempotency_key):
    if connection.vendor != "sqlite":
        pytest.skip("Legacy request fixture uses SQLite direct deletion.")
    table = connection.ops.quote_name(
        AssetLabelAttachmentRequest._meta.db_table
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table} WHERE idempotency_key = %s",
            [idempotency_key],
        )
    assert not AssetLabelAttachmentRequest.objects.filter(
        idempotency_key=idempotency_key
    ).exists()


def test_lifecycle_rejects_effective_minute_before_first_label_activation(client):
    context, asset, _qr = active_asset_context("CORRDATE")
    department, employee, location = add_target_assignment(context, "CORRDATE")
    activation = AssetMovement.objects.get(
        asset=asset, movement_type=AssetMovement.MovementType.LABEL_ACTIVATION
    )
    backdated = activation.effective_at - timedelta(minutes=1)

    with pytest.raises(ValidationError) as exc_info:
        change_asset_assignment(
            actor=context["equipment"],
            asset=asset,
            to_department=department,
            to_responsible_employee=employee,
            to_location=location,
            effective_at=backdated,
            reason="倒序调拨应拒绝",
            idempotency_key="CORRDATE-service",
            expected_status=asset.asset_status,
            expected_department_id=asset.department_id,
            expected_responsible_employee_id=asset.responsible_employee_id,
            expected_location_id=asset.location_id,
        )
    assert "effective_at" in exc_info.value.message_dict

    client.force_login(context["equipment"])
    response = client.post(
        reverse("assets:lifecycle-transfer", args=[asset.pk]),
        {
            "to_department": department.pk,
            "to_responsible_employee": employee.pk,
            "to_location": location.pk,
            "effective_at": timezone.localtime(backdated).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "reason": "HTTP 倒序调拨应拒绝",
            "remark": "",
            "idempotency_key": "CORRDATE-http",
            "expected_status": asset.asset_status,
            "expected_department_id": asset.department_id,
            "expected_responsible_employee_id": asset.responsible_employee_id,
            "expected_location_id": asset.location_id,
        },
    )

    asset.refresh_from_db()
    assert response.status_code == 200
    assert "effective_at" in response.context["form"].errors
    assert asset.department_id != department.pk
    assert AssetMovement.objects.filter(asset=asset).count() == 1

    movement = change_asset_assignment(
        actor=context["equipment"],
        asset=asset,
        to_department=department,
        to_responsible_employee=employee,
        to_location=location,
        effective_at=activation.effective_at.replace(second=0, microsecond=0),
        reason="同分钟调拨应保持精确顺序",
        idempotency_key="CORRDATE-same-minute",
        expected_status=asset.asset_status,
        expected_department_id=asset.department_id,
        expected_responsible_employee_id=asset.responsible_employee_id,
        expected_location_id=asset.location_id,
    )
    assert movement.effective_at >= activation.effective_at


def test_relabel_collision_cannot_report_success_while_qr_remains_printed(client):
    context, asset, _old_qr = active_asset_context("CORRQR")
    set_asset_idle(
        actor=context["equipment"],
        asset=asset,
        effective_at=timezone.now(),
        reason="建立碰撞用闲置变动",
        idempotency_key="CORRQR-collision",
    )
    asset.refresh_from_db()
    qr = rotate_qr_identity(
        actor=context["finance"], asset=asset, reason="纠正测试换标"
    )
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key="CORRQR-reprint",
    )
    confirm_print_batch(actor=context["finance"], batch=batch)
    qr.refresh_from_db()
    assert qr.label_status == "printed"

    client.force_login(context["finance"])
    response = client.post(
        reverse("assets:qr-attach", args=[qr.public_token]),
        {
            "scanned_token": qr.public_token,
            "label_attached": "on",
            "responsibility_confirmed": "on",
            "target_status": "idle",
            "idempotency_key": "CORRQR-collision",
        },
    )

    qr.refresh_from_db()
    assert response.status_code == 400
    assert response.context["attachment_form"].errors
    assert qr.label_status == "printed"
    assert not AssetLabelAttachmentRequest.objects.filter(
        company=context["company"], idempotency_key="CORRQR-collision"
    ).exists()


def test_v1_legacy_label_activation_replay_remains_compatible(client):
    context, asset, qr = active_asset_context("CORRQRV1")
    key = "CORRQRV1-attach"
    _remove_legacy_attachment_request(key)

    replayed = confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr.public_token,
        target_status="in_use",
        idempotency_key=key,
    )

    request = AssetLabelAttachmentRequest.objects.get(idempotency_key=key)
    assert replayed.pk == qr.pk
    assert request.qr_identity_id == qr.pk
    assert qr.version == 1

    _remove_legacy_attachment_request(key)
    client.force_login(context["finance"])
    response = client.post(
        reverse("assets:qr-attach", args=[qr.public_token]),
        {
            "scanned_token": qr.public_token,
            "label_attached": "on",
            "responsibility_confirmed": "on",
            "target_status": "in_use",
            "idempotency_key": key,
        },
    )
    request = AssetLabelAttachmentRequest.objects.get(idempotency_key=key)
    assert response.status_code == 302
    assert request.qr_identity_id == qr.pk


def test_v1_legacy_key_cannot_be_rebound_to_attached_v2(client):
    context, asset, v1 = active_asset_context("CORRQRV2")
    legacy_key = "CORRQRV2-attach"
    _remove_legacy_attachment_request(legacy_key)
    v2 = rotate_qr_identity(
        actor=context["finance"], asset=asset, reason="终审换标"
    )
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key="CORRQRV2-print-v2",
    )
    confirm_print_batch(actor=context["finance"], batch=batch)
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=v2.public_token,
        idempotency_key="CORRQRV2-attach-v2",
    )
    v1.refresh_from_db()
    v2.refresh_from_db()
    assert v1.status == "revoked"
    assert v2.version == 2 and v2.label_status == "attached"

    with pytest.raises(ValidationError):
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token=v2.public_token,
            target_status="in_use",
            idempotency_key=legacy_key,
        )
    assert not AssetLabelAttachmentRequest.objects.filter(
        idempotency_key=legacy_key
    ).exists()

    client.force_login(context["finance"])
    response = client.post(
        reverse("assets:qr-attach", args=[v2.public_token]),
        {
            "scanned_token": v2.public_token,
            "label_attached": "on",
            "responsibility_confirmed": "on",
            "target_status": "in_use",
            "idempotency_key": legacy_key,
        },
    )

    v2.refresh_from_db()
    assert response.status_code == 400
    assert response.context["attachment_form"].errors
    assert v2.label_status == "attached"
    assert not AssetLabelAttachmentRequest.objects.filter(
        idempotency_key=legacy_key
    ).exists()


def test_current_maintenance_instance_cannot_complete_before_or_on_previous_date(
    client,
):
    context = maintenance_context("CORRMAINT")
    first_completed = timezone.localdate() - timedelta(days=1)
    first = complete_maintenance(
        actor=context["responsible_user"],
        plan=context["plan"],
        scheduled_date=context["plan"].next_maintenance_date,
        completed_date=first_completed,
        actual_content="首次有效保养",
        result="normal",
        idempotency_key="CORRMAINT-first",
    )
    context["plan"].refresh_from_db()
    next_due = context["plan"].next_maintenance_date

    with pytest.raises(ValidationError) as exc_info:
        complete_maintenance(
            actor=context["responsible_user"],
            plan=context["plan"],
            scheduled_date=next_due,
            completed_date=first.completed_date,
            actual_content="倒序保养应拒绝",
            result="normal",
            idempotency_key="CORRMAINT-service",
        )
    assert "completed_date" in exc_info.value.message_dict

    client.force_login(context["responsible_user"])
    response = client.post(
        reverse("maintenance:plan-complete", args=[context["plan"].pk]),
        {
            "idempotency_key": "CORRMAINT-http",
            "completed_date": first.completed_date.isoformat(),
            "actual_content": "HTTP 倒序保养应拒绝",
            "result": "normal",
            "problem_description": "",
            "remark": "",
            "security_class": "A0",
        },
    )

    context["plan"].refresh_from_db()
    assert response.status_code == 200
    assert "completed_date" in response.context["form"].errors
    assert MaintenanceRecord.objects.filter(
        maintenance_plan=context["plan"], status="confirmed"
    ).count() == 1
    assert context["plan"].last_maintenance_date == first.completed_date
    assert context["plan"].next_maintenance_date == next_due


def test_first_maintenance_completion_must_advance_beyond_current_instance():
    context = maintenance_context("CORRMAINTFIRST")
    plan = context["plan"]
    scheduled = plan.next_maintenance_date
    completed = scheduled - timedelta(days=31)

    with pytest.raises(ValidationError) as exc_info:
        complete_maintenance(
            actor=context["responsible_user"],
            plan=plan,
            scheduled_date=scheduled,
            completed_date=completed,
            actual_content="首次倒序保养应拒绝",
            result="normal",
            idempotency_key="CORRMAINTFIRST-service",
        )

    plan.refresh_from_db()
    assert "completed_date" in exc_info.value.message_dict
    assert not MaintenanceRecord.objects.filter(maintenance_plan=plan).exists()
    assert plan.last_maintenance_date is None
    assert plan.next_maintenance_date == scheduled


def test_voided_maintenance_rebuild_stays_between_adjacent_valid_records():
    context = maintenance_context("CORRMAINTREBUILD")
    plan = context["plan"]
    today = timezone.localdate()
    first = complete_maintenance(
        actor=context["responsible_user"],
        plan=plan,
        scheduled_date=plan.next_maintenance_date,
        completed_date=today - timedelta(days=10),
        actual_content="首次保养 A",
        result="normal",
        idempotency_key="CORRMAINTREBUILD-a",
    )
    plan.refresh_from_db()
    following = complete_maintenance(
        actor=context["responsible_user"],
        plan=plan,
        scheduled_date=plan.next_maintenance_date,
        completed_date=today - timedelta(days=5),
        actual_content="后续保养 B",
        result="normal",
        idempotency_key="CORRMAINTREBUILD-b",
    )
    void_maintenance_record(
        actor=context["equipment"],
        record=first,
        reason="重建 A 的完成日期",
        idempotency_key="CORRMAINTREBUILD-void-a",
    )
    plan.refresh_from_db()
    expected_last = following.completed_date
    expected_next = plan.next_maintenance_date

    for suffix, invalid_completed in (
        ("same-day", following.completed_date),
        ("after", following.completed_date + timedelta(days=1)),
    ):
        with pytest.raises(ValidationError) as exc_info:
            complete_maintenance(
                actor=context["responsible_user"],
                plan=plan,
                scheduled_date=first.scheduled_date,
                completed_date=invalid_completed,
                actual_content="倒序重建 A 应拒绝",
                result="normal",
                idempotency_key=f"CORRMAINTREBUILD-invalid-{suffix}",
            )
        assert "completed_date" in exc_info.value.message_dict

    plan.refresh_from_db()
    assert not MaintenanceRecord.objects.filter(
        idempotency_key__startswith="CORRMAINTREBUILD-invalid-"
    ).exists()
    assert plan.records.count() == 2
    assert plan.last_maintenance_date == expected_last
    assert plan.next_maintenance_date == expected_next

    rebuilt = complete_maintenance(
        actor=context["responsible_user"],
        plan=plan,
        scheduled_date=first.scheduled_date,
        completed_date=first.completed_date + timedelta(days=1),
        actual_content="合法重建 A",
        result="normal",
        idempotency_key="CORRMAINTREBUILD-valid",
    )
    plan.refresh_from_db()
    assert rebuilt.scheduled_date == first.scheduled_date
    assert first.completed_date < rebuilt.completed_date < following.completed_date
    assert plan.last_maintenance_date == expected_last
    assert plan.next_maintenance_date == expected_next


def test_multiple_void_rebuild_uses_voided_history_boundaries():
    context = maintenance_context("CORRMAINTMULTIVOID")
    plan = context["plan"]
    today = timezone.localdate()
    first = complete_maintenance(
        actor=context["responsible_user"],
        plan=plan,
        scheduled_date=plan.next_maintenance_date,
        completed_date=today - timedelta(days=10),
        actual_content="首次保养 A",
        result="normal",
        idempotency_key="CORRMAINTMULTIVOID-a",
    )
    plan.refresh_from_db()
    following = complete_maintenance(
        actor=context["responsible_user"],
        plan=plan,
        scheduled_date=plan.next_maintenance_date,
        completed_date=today - timedelta(days=5),
        actual_content="后续保养 B",
        result="normal",
        idempotency_key="CORRMAINTMULTIVOID-b",
    )
    void_maintenance_record(
        actor=context["equipment"],
        record=following,
        reason="先作废 B",
        idempotency_key="CORRMAINTMULTIVOID-void-b",
    )
    void_maintenance_record(
        actor=context["equipment"],
        record=first,
        reason="再作废 A",
        idempotency_key="CORRMAINTMULTIVOID-void-a",
    )
    plan.refresh_from_db()
    assert plan.last_maintenance_date is None
    assert plan.next_maintenance_date == plan.first_due_date
    assert following.scheduled_date != plan.next_maintenance_date
    assert not plan.records.filter(status="confirmed").exists()

    for suffix, invalid_completed in (
        ("before", first.completed_date - timedelta(days=1)),
        ("same-day", first.completed_date),
    ):
        with pytest.raises(ValidationError) as exc_info:
            complete_maintenance(
                actor=context["responsible_user"],
                plan=plan,
                scheduled_date=following.scheduled_date,
                completed_date=invalid_completed,
                actual_content="B 不得越过作废历史 A",
                result="normal",
                idempotency_key=f"CORRMAINTMULTIVOID-invalid-{suffix}",
            )
        assert "completed_date" in exc_info.value.message_dict

    plan.refresh_from_db()
    assert plan.records.count() == 2
    assert plan.last_maintenance_date is None
    assert plan.next_maintenance_date == plan.first_due_date

    rebuilt_following = complete_maintenance(
        actor=context["responsible_user"],
        plan=plan,
        scheduled_date=following.scheduled_date,
        completed_date=following.completed_date,
        actual_content="合法重建 B",
        result="normal",
        idempotency_key="CORRMAINTMULTIVOID-rebuild-b",
    )
    plan.refresh_from_db()
    assert plan.next_maintenance_date > rebuilt_following.scheduled_date
    assert not plan.records.filter(
        status="confirmed", scheduled_date=plan.next_maintenance_date
    ).exists()

    rebuilt_first = complete_maintenance(
        actor=context["responsible_user"],
        plan=plan,
        scheduled_date=first.scheduled_date,
        completed_date=first.completed_date,
        actual_content="合法重建 A",
        result="normal",
        idempotency_key="CORRMAINTMULTIVOID-rebuild-a",
    )
    plan.refresh_from_db()
    assert rebuilt_first.completed_date < rebuilt_following.completed_date
    assert plan.last_maintenance_date == rebuilt_following.completed_date
    assert plan.next_maintenance_date > rebuilt_following.scheduled_date
    assert not plan.records.filter(
        status="confirmed", scheduled_date=plan.next_maintenance_date
    ).exists()
