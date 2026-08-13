from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.assets.lifecycle_services import (
    complete_disposal,
    initiate_disposal,
    loan_asset,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
)
from apps.assets.models import Asset, AssetLoan, AssetMovement, AttachmentLink
from apps.finance.models import AssetDepreciationProfile, DepreciationProfileEvent
from tests.test_sprint3_support import direct_attachment, make_company
from tests.test_sprint4_services import _profile_context
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL trigger/constraint acceptance test")


def _loan(context, asset, key):
    return loan_asset(
        actor=context["equipment"], asset=asset,
        borrower_type="internal_employee",
        borrower_employee=context["employee"],
        loan_date=timezone.localdate(),
        expected_return_date=timezone.localdate() + timedelta(days=3),
        handled_by=context["equipment"], reason="数据库约束测试借出",
        idempotency_key=key, expected_status=asset.asset_status,
    )


def _open_disposal(context, asset, key):
    today = timezone.localdate()
    return initiate_disposal(
        actor=context["equipment"], asset=asset, disposal_type="scrap",
        application_date=today, planned_disposal_date=today,
        reason="数据库约束测试处置", idempotency_key=key,
        expected_status=asset.asset_status,
    )


def _complete_disposal(context, asset, key):
    disposal = _open_disposal(context, asset, f"{key}-start")
    disposal = record_disposal_actual_details(
        actor=context["equipment"], disposal=disposal,
        actual_disposal_date=timezone.localdate(),
        handled_by=context["equipment"], idempotency_key=f"{key}-actual",
    )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"], disposal=disposal,
        disposal_income="0.00", idempotency_key=f"{key}-snapshot",
    )
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key=f"private/disposals/{key}.jpg", filename=f"{key}.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        asset_disposal=disposal, role="disposal", security_class="A0",
        created_by=context["equipment"],
    )
    return complete_disposal(
        actor=context["equipment"], disposal=disposal,
        idempotency_key=f"{key}-complete",
    )


def test_database_rejects_invalid_loan_field_matrix_and_movement_reuse():
    context, asset, _qr = active_asset_context("S7DBLOAN")
    loan = _loan(context, asset, "S7DBLOAN-first")

    invalid = {
        "company_id": context["company"].pk,
        "asset_id": asset.pk,
        "borrower_type": "external",
        "borrower_employee_id": context["employee"].pk,
        "borrower_name_snapshot": "",
        "borrower_name": "外部人员",
        "borrower_organization": "外部单位",
        "loan_date": timezone.localdate(),
        "expected_return_date": timezone.localdate() - timedelta(days=1),
        "handled_by_id": context["equipment"].pk,
        "previous_asset_status": "in_use",
        "reason": "非法借出",
        "status": "active",
        "loan_movement_id": loan.loan_movement_id,
        "loan_idempotency_key": "S7DBLOAN-invalid",
        "created_by_id": context["equipment"].pk,
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetLoan._base_manager.create(**invalid)

    # The OneToOne constraint is independent from the active-loan constraint.
    invalid.update(
        borrower_type="internal_employee",
        borrower_name_snapshot=context["employee"].name,
        borrower_name="",
        borrower_organization="",
        expected_return_date=timezone.localdate() + timedelta(days=1),
        loan_idempotency_key="S7DBLOAN-reuse",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetLoan._base_manager.create(**invalid)


def test_database_attachment_requires_exactly_one_same_company_target():
    _postgresql_only()
    context, asset, _qr = active_asset_context("S7DBATT")
    disposal = _open_disposal(context, asset, "S7DBATT-start")
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/disposals/S7DBATT.jpg", filename="S7DBATT.jpg",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=context["company"], attachment=attachment,
            asset=asset, asset_disposal=disposal,
            role="disposal", security_class="A0",
            created_by=context["equipment"],
        )

    other_company = make_company("S7DBATT-OTHER", active=False)
    other_attachment = direct_attachment(
        other_company, context["equipment"],
        key="private/disposals/S7DBATT-other.jpg",
        filename="S7DBATT-other.jpg",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=context["company"], attachment=other_attachment,
            asset_disposal=disposal, role="disposal", security_class="A0",
            created_by=context["equipment"],
        )


def test_database_rejects_snapshot_tampering_and_history_deletion():
    _postgresql_only()
    context, asset, _qr = active_asset_context("S7DBIMM")
    disposal = _complete_disposal(context, asset, "S7DBIMM")
    movement = AssetMovement.objects.get(
        asset=asset, movement_type="disposal_complete"
    )

    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE assets_assetdisposal SET book_value_snapshot = %s WHERE id = %s",
            ["1.00", disposal.pk],
        )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM assets_assetmovement WHERE id = %s", [movement.pk])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM assets_assetdisposal WHERE id = %s", [disposal.pk])

    disposal.refresh_from_db()
    assert disposal.book_value_snapshot == disposal.original_cost_snapshot
    assert AssetMovement.objects.filter(pk=movement.pk).exists()


def test_formal_and_disposed_assets_are_never_physically_deleted():
    context, asset, _qr = active_asset_context("S7DBDELETE")
    with pytest.raises(ValidationError):
        asset.delete()
    with pytest.raises(ValidationError):
        Asset.objects.filter(pk=asset.pk).delete()

    disposal = _complete_disposal(context, asset, "S7DBDELETE")
    with pytest.raises((ProtectedError, IntegrityError)):
        Asset._base_manager.filter(pk=asset.pk)._raw_delete(using="default")
    assert Asset.objects.filter(pk=asset.pk).exists()
    assert type(disposal)._base_manager.filter(pk=disposal.pk).exists()


def test_depreciation_disposal_event_field_matrix_has_database_constraints():
    company, finance, _management, _admin, asset, _finance, profile = (
        _profile_context()
    )

    # A normal/manual event may not carry disposal-only structured fields.
    with pytest.raises(IntegrityError), transaction.atomic():
        DepreciationProfileEvent.objects.create(
            company=company, asset=asset,
            depreciation_profile=profile, event_type="stop",
            effective_date=timezone.localdate(), reason="非法来源字段",
            previous_profile_status="active", created_by=finance,
        )
