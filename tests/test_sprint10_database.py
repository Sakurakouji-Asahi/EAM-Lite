from __future__ import annotations

import uuid

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.assets.lifecycle_services import complete_disposal
from apps.assets.models import AssetDisposalReversal, AttachmentLink
from apps.masterdata.models import Employee
from apps.offboarding.models import (
    EmployeeAssetClearance,
    EmployeeAssetClearanceItem,
)
from apps.offboarding.services import (
    complete_clearance,
    initiate_clearance,
    transfer_clearance_item,
)
from tests.test_sprint3_support import direct_attachment, make_company, make_user
from tests.test_sprint10_support import (
    additional_employee,
    formal_asset,
    offboarding_context,
)
from tests.test_sprint10_lifecycle import _initiate_disposal, _lock_and_evidence


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 10 database acceptance requires PostgreSQL 18.4")


def _force_deferred_constraints():
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def _one_item_clearance(prefix):
    context = offboarding_context(prefix)
    asset, _ = formal_asset(context, f"{prefix}-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key=f"{prefix}-init",
    )
    return context, asset, clearance, clearance.items.get()


def test_postgresql_18_4_and_sprint10_triggers_are_installed():
    _postgresql_only()
    expected = {
        "trg_offboarding_employee_transition": (False, False),
        "trg_offboarding_employee_commit": (True, True),
        "trg_offboarding_clearance_write": (False, False),
        "trg_offboarding_item_write": (False, False),
        "trg_offboarding_clearance_commit": (True, True),
        "trg_offboarding_item_clearance_commit": (True, True),
        "trg_offboarding_item_evidence_commit": (True, True),
        "trg_offboarding_asset_evidence_commit": (True, True),
        "trg_offboarding_loan_evidence_commit": (True, True),
        "trg_00_offboarding_block_disposal_reversal": (False, False),
    }
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        assert cursor.fetchone()[0].startswith("18.4")
        cursor.execute(
            """
            SELECT tgname, tgdeferrable, tginitdeferred
              FROM pg_trigger
             WHERE tgname = ANY(%s) AND NOT tgisinternal
            """,
            [list(expected)],
        )
        installed = {
            name: (deferrable, initially_deferred)
            for name, deferrable, initially_deferred in cursor.fetchall()
        }
    assert expected == installed


def test_raw_employee_status_and_termination_bypass_is_rejected():
    _postgresql_only()
    context = offboarding_context("S10DBEMP")
    employee = context["employee"]
    attempts = (
        (
            "UPDATE masterdata_employee SET employment_status='leaving', is_active=false WHERE id=%s",
            [employee.pk],
        ),
        (
            "UPDATE masterdata_employee SET employment_status='resigned', termination_date=%s, is_active=false WHERE id=%s",
            [timezone.localdate(), employee.pk],
        ),
    )
    for sql, params in attempts:
        with pytest.raises((IntegrityError, DatabaseError)), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
            _force_deferred_constraints()
    employee.refresh_from_db()
    assert employee.employment_status == "active"
    assert employee.is_active is True
    assert employee.termination_date is None


def test_raw_clearance_insert_update_delete_and_item_snapshot_tampering_are_rejected():
    _postgresql_only()
    context, _asset, clearance, item = _one_item_clearance("S10DBRAW")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO offboarding_employeeassetclearance
                (id, company_id, employee_id, initiated_at, initiated_by_id,
                 total_assets_snapshot, unresolved_assets, status, remark,
                 idempotency_key, supplement_reason)
            VALUES (%s,%s,%s,%s,%s,0,0,'open','','raw-illegal','')
            """,
            [
                uuid.uuid4(),
                context["company"].pk,
                context["employee"].pk,
                timezone.now(),
                context["hr"].pk,
            ],
        )
    for sql, params in (
        (
            "UPDATE offboarding_employeeassetclearance SET remark='tampered' WHERE id=%s",
            [clearance.pk],
        ),
        (
            "UPDATE offboarding_employeeassetclearanceitem SET asset_name_snapshot='tampered' WHERE id=%s",
            [item.pk],
        ),
        (
            "UPDATE offboarding_employeeassetclearanceitem SET resolution='returned', resolved_at=clock_timestamp(), resolved_by_id=%s WHERE id=%s",
            [context["equipment"].pk, item.pk],
        ),
        (
            "DELETE FROM offboarding_employeeassetclearanceitem WHERE id=%s",
            [item.pk],
        ),
        (
            "DELETE FROM offboarding_employeeassetclearance WHERE id=%s",
            [clearance.pk],
        ),
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)
    clearance.refresh_from_db()
    item.refresh_from_db()
    assert clearance.remark == ""
    assert item.asset_name_snapshot != "tampered"
    assert item.resolution == "pending"


def test_deferred_counter_status_and_employee_coherence_rejects_partial_final_state():
    _postgresql_only()
    context, _asset, clearance, item = _one_item_clearance("S10DBCNT")
    invalid_updates = (
        (
            "UPDATE offboarding_employeeassetclearance SET total_assets_snapshot=9, unresolved_assets=9 WHERE id=%s",
            [clearance.pk],
        ),
        (
            "UPDATE offboarding_employeeassetclearance SET status='open' WHERE id=%s",
            [clearance.pk],
        ),
    )
    for sql, params in invalid_updates:
        with pytest.raises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('eam_lite.controlled_clearance_mutation','on',true)"
                )
                cursor.execute(sql, params)
            _force_deferred_constraints()
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_employee_offboarding','on',true)"
            )
            cursor.execute(
                "UPDATE masterdata_employee SET employment_status='resigned', termination_date=%s WHERE id=%s",
                [timezone.localdate(), context["employee"].pk],
            )
        _force_deferred_constraints()
    clearance.refresh_from_db()
    item.refresh_from_db()
    context["employee"].refresh_from_db()
    assert (clearance.total_assets_snapshot, clearance.unresolved_assets) == (1, 1)
    assert clearance.status == "blocked"
    assert item.resolution == "pending"
    assert context["employee"].employment_status == "leaving"


def test_database_rejects_cross_company_clearance_attachment_target_and_two_targets():
    _postgresql_only()
    context, asset, clearance, item = _one_item_clearance("S10DBATT")
    attachment = direct_attachment(
        context["company"],
        context["hr"],
        key="private/clearance/S10DBATT.jpg",
        filename="S10DBATT.jpg",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=context["company"],
            attachment=attachment,
            clearance=clearance,
            clearance_item=item,
            role="clearance",
            security_class="A0",
            created_by=context["hr"],
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=context["company"],
            attachment=attachment,
            asset=asset,
            role="clearance",
            security_class="A0",
            created_by=context["hr"],
        )
    other = make_company("S10DBATT-OTHER", active=False)
    other_attachment = direct_attachment(
        other,
        context["hr"],
        key="private/clearance/S10DBATT-other.jpg",
        filename="S10DBATT-other.jpg",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=context["company"],
            attachment=other_attachment,
            clearance=clearance,
            role="clearance",
            security_class="A0",
            created_by=context["hr"],
        )


def test_history_foreign_keys_protect_targets_and_set_null_only_actor_columns():
    _postgresql_only()
    context = offboarding_context("S10DBNULL")
    receiver = additional_employee(context, "S10DBNULL-R")
    historical_hr = make_user("s10dbnull-historical-hr", "hr")
    asset, _ = formal_asset(context, "S10DBNULL-A")
    clearance = initiate_clearance(
        actor=historical_hr,
        employee=context["employee"],
        idempotency_key="S10DBNULL-init",
    )
    item = clearance.items.get()
    item = transfer_clearance_item(
        actor=context["equipment"],
        item=item,
        to_department=receiver.department,
        to_responsible_employee=receiver,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="数据库历史保护",
        idempotency_key="S10DBNULL-transfer",
    )
    clearance = complete_clearance(
        actor=historical_hr,
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    historical_hr.delete()
    clearance.refresh_from_db()
    item.refresh_from_db()
    assert clearance.initiated_by_id is None
    assert clearance.completed_by_id is None
    assert item.movement_id is not None
    with pytest.raises((ProtectedError, IntegrityError)):
        Employee._base_manager.filter(pk=context["employee"].pk)._raw_delete(
            using="default"
        )
    with pytest.raises((ProtectedError, IntegrityError)):
        type(asset)._base_manager.filter(pk=asset.pk)._raw_delete(using="default")


def test_raw_disposal_reversal_is_blocked_when_disposal_resolves_clearance():
    _postgresql_only()
    context, asset, clearance, item = _one_item_clearance("S10DBREV")
    disposal = _initiate_disposal(context, asset, "S10DBREV-start")
    disposal = _lock_and_evidence(context, disposal, "S10DBREV")
    disposal = complete_disposal(
        actor=context["equipment"],
        disposal=disposal,
        idempotency_key="S10DBREV-complete",
    )
    item.refresh_from_db()
    clearance.refresh_from_db()
    assert item.resolution == "disposed"
    assert item.disposal_id == disposal.pk
    assert clearance.unresolved_assets == 0

    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_asset_disposal_reversal_insert','on',true)"
            )
        AssetDisposalReversal._base_manager.create(
            company=context["company"],
            asset_disposal=disposal,
            reason="raw bypass attempt",
            restored_asset_status=disposal.previous_asset_status,
            idempotency_key="S10DBREV-raw-reversal",
            reversed_by=context["finance"],
            reversed_at=timezone.now(),
        )
    assert not AssetDisposalReversal._base_manager.filter(
        asset_disposal=disposal
    ).exists()


def test_raw_completed_clearance_and_resolved_item_remain_immutable():
    _postgresql_only()
    context, _asset, clearance, item = _one_item_clearance("S10DBDONE")
    receiver = additional_employee(context, "S10DBDONE-R")
    item = transfer_clearance_item(
        actor=context["equipment"],
        item=item,
        to_department=receiver.department,
        to_responsible_employee=receiver,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="完成前转交",
        idempotency_key="S10DBDONE-transfer",
    )
    clearance = complete_clearance(
        actor=context["hr"],
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    for sql, params in (
        (
            "UPDATE offboarding_employeeassetclearance SET status='cancelled' WHERE id=%s",
            [clearance.pk],
        ),
        (
            "UPDATE offboarding_employeeassetclearanceitem SET resolution='pending', movement_id=NULL, resolved_at=NULL, resolved_by_id=NULL WHERE id=%s",
            [item.pk],
        ),
    ):
        with pytest.raises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
    clearance.refresh_from_db()
    item.refresh_from_db()
    assert clearance.status == "completed"
    assert item.resolution == "transferred"
