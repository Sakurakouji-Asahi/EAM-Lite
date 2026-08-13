from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.functional import empty

from apps.assets.models import AssetExternalReference
from apps.audit.models import AuditLog
from apps.masterdata.models import Attachment
from apps.reports.models import ExportLog, ExportLogTotal
from apps.reports.schemas import TPLUS_TOTAL_METRICS
from apps.reports.services import (
    create_or_correct_external_reference,
    generate_report_export,
)
from tests.test_sprint3_support import (
    add_photo,
    make_asset,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
    direct_draft,
)
from tests.test_sprint4_acceptance import _confirm_nonfixed
from tests.test_sprint4_services import _asset, _confirmed_entry, _profile_context
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def isolated_export_storage(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "tmp"
    settings.MEDIA_ROOT.mkdir()
    settings.IMPORT_TEMP_ROOT.mkdir()
    default_storage._wrapped = empty
    yield tmp_path
    default_storage._wrapped = empty


def _pending_log(context, *, key="s11-pending", export_type="asset_ledger"):
    return ExportLog.objects.create(
        company=context["company"],
        export_type=export_type,
        filters_json={},
        request_hash=hashlib.sha256(key.encode()).hexdigest(),
        idempotency_key=key,
        requested_by=context["finance"],
    )


def _raw_update(model, pk, **values):
    return QuerySet.update(model._base_manager.filter(pk=pk), **values)


def _second_formal_asset(context, suffix):
    asset = make_asset(
        actor=context["equipment"],
        company=context["company"],
        category=context["category"],
        department=context["department"],
        employee=context["employee"],
        location=context["location"],
        commissioning_date=timezone.localdate(),
        asset_name=f"{suffix} 第二资产",
    )
    add_photo(context["equipment"], asset)
    from apps.assets.services import submit_asset_for_finance

    asset = submit_asset_for_finance(actor=context["equipment"], asset=asset)
    return _confirm_nonfixed(
        context,
        asset,
        cost=Decimal("100.00"),
        key=f"{suffix}-formalize",
    )


def test_generic_export_publishes_one_private_attachment_hash_and_no_totals(
    isolated_export_storage,
):
    context, asset, _qr = active_asset_context("S11EXPORTOK")
    export_log = generate_report_export(
        actor=context["finance"],
        company=context["company"],
        report_key="asset_ledger",
        filters={},
        idempotency_key="S11EXPORTOK-key",
    )
    export_log.refresh_from_db()
    attachment = export_log.output_attachment
    assert export_log.status == "completed"
    assert export_log.generated_at == export_log.completed_at
    assert export_log.row_count == 1
    assert export_log.data_snapshot_at <= export_log.completed_at
    assert export_log.totals_schema_version == "report_v1"
    assert export_log.totals.count() == 0
    assert attachment.company_id == context["company"].pk
    assert attachment.is_available is True
    assert attachment.sha256 == export_log.output_sha256
    assert default_storage.exists(attachment.storage_key)
    with default_storage.open(attachment.storage_key, "rb") as exported:
        assert hashlib.sha256(exported.read()).hexdigest() == export_log.output_sha256
    assert AuditLog.objects.filter(
        company=context["company"],
        object_type="ExportLog",
        object_id=str(export_log.pk),
        action="report_export_completed",
    ).exists()


def test_export_failure_has_no_attachment_or_totals_and_retry_contract_is_explicit(
    monkeypatch, isolated_export_storage
):
    context, _asset, _qr = active_asset_context("S11EXPORTFAIL")

    def fail_writer(*_args, **_kwargs):
        raise RuntimeError("forced Sprint11 writer failure")

    monkeypatch.setattr("apps.reports.services.write_report_workbook", fail_writer)
    with pytest.raises(RuntimeError, match="forced Sprint11 writer failure"):
        generate_report_export(
            actor=context["finance"],
            company=context["company"],
            report_key="asset_ledger",
            filters={},
            idempotency_key="S11EXPORTFAIL-key",
        )
    failed = ExportLog.objects.get(idempotency_key="S11EXPORTFAIL-key")
    assert failed.status == "failed"
    assert failed.output_attachment_id is None
    assert failed.output_sha256 == ""
    assert failed.totals_schema_version == ""
    assert failed.totals.count() == 0
    assert not list(isolated_export_storage.rglob("*.xlsx"))

    repeated = generate_report_export(
        actor=context["finance"],
        company=context["company"],
        report_key="asset_ledger",
        filters={},
        idempotency_key="S11EXPORTFAIL-key",
    )
    assert repeated.pk == failed.pk
    assert repeated.status == "failed"
    with pytest.raises(ValidationError, match="幂等键"):
        generate_report_export(
            actor=context["finance"],
            company=context["company"],
            report_key="asset_ledger",
            filters={"include_disposed": False},
            idempotency_key="S11EXPORTFAIL-key",
        )


def test_same_request_with_new_idempotency_key_creates_auditable_new_export(
    isolated_export_storage,
):
    context, _asset, _qr = active_asset_context("S11RERUN")
    first = generate_report_export(
        actor=context["finance"],
        company=context["company"],
        report_key="asset_ledger",
        filters={},
        idempotency_key="S11RERUN-one",
    )
    second = generate_report_export(
        actor=context["finance"],
        company=context["company"],
        report_key="asset_ledger",
        filters={},
        idempotency_key="S11RERUN-two",
    )
    assert first.pk != second.pk
    assert first.request_hash == second.request_hash
    assert ExportLog.objects.filter(company=context["company"]).count() == 2


def test_external_reference_preserves_leading_zero_normalizes_uniqueness_and_audits():
    context, first_asset, _qr = active_asset_context("S11EXTONE")
    second_asset = _second_formal_asset(context, "S11EXTTWO")
    reference = create_or_correct_external_reference(
        actor=context["finance"],
        asset=first_asset,
        reference_value=" ００AbC ",
        note="首次核对",
        reason="财务确认 T+ 卡片",
    )
    assert reference.reference_value == "00AbC"
    assert reference.normalized_value == "00abc"
    with pytest.raises(ValidationError):
        create_or_correct_external_reference(
            actor=context["finance"],
            asset=second_asset,
            reference_value="00abc",
            reason="不允许重复认领",
        )
    corrected = create_or_correct_external_reference(
        actor=context["finance"],
        asset=first_asset,
        reference_value="000789",
        note="更正后",
        reason="原卡片号录入错误",
    )
    assert corrected.pk == reference.pk
    assert corrected.reference_value == "000789"
    assert corrected.normalized_value == "000789"
    audits = AuditLog.objects.filter(
        company=context["company"], object_type="AssetExternalReference"
    ).order_by("created_at")
    assert list(audits.values_list("action", flat=True)) == [
        "asset_external_reference_created",
        "asset_external_reference_corrected",
    ]
    assert audits.last().old_data_json["reference_value"] == "00AbC"
    assert audits.last().new_data_json["reference_value"] == "000789"
    assert audits.last().new_data_json["reason"] == "原卡片号录入错误"


def test_external_reference_permissions_cross_company_and_immutability():
    context, asset, _qr = active_asset_context("S11EXTPERM")
    for role in (context["equipment"], context["admin"], make_user("s11-ext-management", "management")):
        with pytest.raises(PermissionDenied):
            create_or_correct_external_reference(
                actor=role,
                asset=asset,
                reference_value="0001",
                reason="越权尝试",
            )
    foreign_company = make_company("S11EXTFOREIGN", active=False)
    foreign_category = make_category(foreign_company, "S11EXTFOREIGN-CAT")
    foreign_asset = direct_draft(
        company=foreign_company,
        category=foreign_category,
        actor=context["equipment"],
        asset_name="跨公司资产",
    )
    with pytest.raises(PermissionDenied):
        create_or_correct_external_reference(
            actor=context["finance"],
            asset=foreign_asset,
            reference_value="0001",
            reason="跨公司尝试",
        )
    reference = create_or_correct_external_reference(
        actor=context["finance"],
        asset=asset,
        reference_value="0001",
        reason="合法创建",
    )
    reference.note = "绕过服务"
    with pytest.raises(ValidationError):
        reference.save()
    with pytest.raises(ValidationError):
        AssetExternalReference.objects.filter(pk=reference.pk).update(note="绕过")
    with pytest.raises(ValidationError):
        reference.delete()
    with pytest.raises(ValidationError):
        asset.delete()


def test_export_actor_is_set_null_compatible_without_losing_completed_fact(
    isolated_export_storage,
):
    context, _asset, _qr = active_asset_context("S11ACTORNULL")
    export_actor = make_user("s11-export-only-finance", "finance")
    export_log = generate_report_export(
        actor=export_actor,
        company=context["company"],
        report_key="asset_ledger",
        filters={},
        idempotency_key="S11ACTORNULL-key",
    )
    user_id = export_actor.pk
    type(export_actor).objects.filter(pk=user_id).delete()
    export_log.refresh_from_db()
    assert export_log.status == "completed"
    assert export_log.requested_by_id is None
    assert export_log.output_attachment_id is not None


@pytest.mark.django_db(transaction=True)
def test_postgresql_guards_reject_incomplete_completion_and_published_mutation():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred guard acceptance")
    context, _asset, _qr = active_asset_context("S11PGGUARD")
    pending = _pending_log(
        context, key="s11-pg-incomplete", export_type="tplus_reconciliation"
    )
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config(%s, %s, true)",
                    ["eam_lite.controlled_export_log_mutation", "on"],
                )
                cursor.execute(
                    """
                    UPDATE reports_exportlog
                       SET status='completed', data_snapshot_at=%s, row_count=0,
                           output_sha256=%s, totals_schema_version='tplus_v1',
                           completed_at=%s
                     WHERE id=%s
                    """,
                    [timezone.now(), "a" * 64, timezone.now(), pending.pk],
                )

    completed = generate_report_export(
        actor=context["finance"],
        company=context["company"],
        report_key="asset_ledger",
        filters={},
        idempotency_key="s11-pg-completed",
    )
    for sql in (
        "UPDATE reports_exportlog SET row_count=row_count+1 WHERE id=%s",
        "DELETE FROM reports_exportlog WHERE id=%s",
    ):
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, [completed.pk])


@pytest.mark.django_db(transaction=True)
def test_postgresql_completed_tplus_totals_are_immutable_and_parent_protected():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred guard acceptance")
    context, _asset, _qr = active_asset_context("S11PGTOTAL")
    parent = _pending_log(
        context, key="s11-pg-total", export_type="tplus_reconciliation"
    )
    totals = []
    for key in TPLUS_TOTAL_METRICS:
        totals.append(
            ExportLogTotal.objects.create(
                company=context["company"],
                export_log=parent,
                metric_key=key,
                amount=Decimal("0.00"),
            )
        )
    attachment = Attachment.objects.create(
        company=context["company"],
        storage_key="private/assets/s11-pg-total.xlsx",
        original_filename="s11.xlsx",
        safe_filename="s11.xlsx",
        file_size=1,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256="d" * 64,
        uploaded_by=context["finance"],
        malware_scan_status="policy_limited",
        is_available=True,
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_export_log_mutation", "on"],
            )
            cursor.execute(
                """
                UPDATE reports_exportlog
                   SET status='completed', data_snapshot_at=%s, row_count=0,
                       output_attachment_id=%s, output_sha256=%s,
                       totals_schema_version='tplus_v1', completed_at=%s
                 WHERE id=%s
                """,
                [timezone.now(), attachment.pk, attachment.sha256, timezone.now(), parent.pk],
            )
    for sql, params in (
        ("UPDATE reports_exportlogtotal SET amount=1 WHERE id=%s", [totals[0].pk]),
        ("DELETE FROM reports_exportlogtotal WHERE id=%s", [totals[0].pk]),
        (
            "INSERT INTO reports_exportlogtotal "
            "(id, company_id, export_log_id, metric_key, amount, currency) "
            "VALUES (gen_random_uuid(), %s, %s, 'original_cost', 1, 'CNY')",
            [context["company"].pk, parent.pk],
        ),
    ):
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
    with pytest.raises(ProtectedError):
        attachment.delete()


def test_postgresql_depreciation_entry_source_guard_checks_final_commit_state():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred entry-source guard acceptance")
    company, actor, _management, _admin, _asset, _finance, profile = _profile_context()

    with transaction.atomic():
        batch, _item, entry = _confirmed_entry(
            profile=profile,
            start=date(2024, 2, 1),
            amount=Decimal("190.00"),
            actor=actor,
        )
    batch.refresh_from_db()
    assert batch.status == "confirmed"
    assert entry.__class__.objects.filter(pk=entry.pk).exists()

    with pytest.raises(IntegrityError, match="posted source"):
        with transaction.atomic():
            _confirmed_entry(
                profile=profile,
                start=date(2024, 3, 1),
                amount=Decimal("190.00"),
                actor=actor,
                status="draft",
            )
    assert company.depreciation_batches.count() == 1


def test_postgresql_reversed_batch_requires_complete_entry_reversal_coverage():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred batch reversal coverage acceptance")
    from apps.finance.models import (
        AssetDepreciationProfile,
        AssetFinance,
        DepreciationBatch,
        DepreciationBatchItem,
        DepreciationEntry,
    )

    company, actor, _management, _admin, _asset_one, finance, profile_one = (
        _profile_context()
    )
    asset_two = _asset(company, actor, suffix="S11PARTIAL")
    AssetFinance.objects.create(
        company=company,
        asset=asset_two,
        accounting_treatment="fixed_asset",
        recognition_threshold_snapshot=Decimal("5000.00"),
        fixed_asset_category=finance.fixed_asset_category,
        original_cost=Decimal("12000.00"),
        capitalization_date=date(2024, 1, 1),
        impairment_balance_cache=Decimal("0.00"),
        finance_confirmed_by=actor,
        finance_confirmed_at=timezone.now(),
    )
    profile_two = AssetDepreciationProfile.objects.create(
        company=company,
        asset=asset_two,
        depreciation_policy=profile_one.depreciation_policy,
        version=1,
        method="straight_line",
        posting_period="monthly",
        start_rule="current_month",
        stop_rule="event_date",
        start_date=date(2024, 1, 1),
        useful_life_months=60,
        salvage_mode="rate",
        salvage_rate=Decimal("0.05"),
        opening_book_value=Decimal("12000.00"),
        opening_actual_accumulated_depreciation=Decimal("0.00"),
        effective_from=date(2024, 1, 1),
        status="active",
        created_by=actor,
    )

    with transaction.atomic():
        source = DepreciationBatch.objects.create(
            company=company,
            period_start=date(2024, 2, 1),
            period_end=date(2024, 3, 1),
            generation_no=1,
            batch_type="regular",
            status="draft",
            idempotency_key="s11-partial-source",
            request_hash="a" * 64,
            generated_by=actor,
            generated_at=timezone.now(),
        )
        originals = []
        for profile in (profile_one, profile_two):
            item = DepreciationBatchItem.objects.create(
                company=company,
                batch=source,
                asset=profile.asset,
                depreciation_profile=profile,
                calculation_method="straight_line",
                opening_book_value=Decimal("12000.00"),
                depreciable_floor=Decimal("600.00"),
                eligible_fraction=Decimal("1"),
                calculated_unrounded=Decimal("190.00"),
                planned_amount=Decimal("190.00"),
                closing_book_value=Decimal("11810.00"),
                calculation_snapshot_json={"probe": "partial-reversal"},
                status="ready",
            )
            originals.append(
                DepreciationEntry.objects.create(
                    company=company,
                    asset=profile.asset,
                    depreciation_profile=profile,
                    entry_date=date(2024, 3, 1),
                    period_start=date(2024, 2, 1),
                    period_end=date(2024, 3, 1),
                    source_type="batch",
                    batch_item=item,
                    amount=Decimal("190.00"),
                    accumulated_depreciation_after=Decimal("190.00"),
                    book_value_after=Decimal("11810.00"),
                    posted_by=actor,
                    posted_at=timezone.now(),
                )
            )
        source.status = "confirmed"
        source.confirmed_by = actor
        source.confirmed_at = timezone.now()
        source.save(update_fields=["status", "confirmed_by", "confirmed_at"])

    with pytest.raises(IntegrityError, match="complete entry reversals"):
        with transaction.atomic():
            reversal = DepreciationBatch.objects.create(
                company=company,
                period_start=source.period_start,
                period_end=source.period_end,
                generation_no=source.generation_no,
                batch_type="reversal",
                status="draft",
                idempotency_key="s11-partial-reversal",
                request_hash="b" * 64,
                generated_by=actor,
                generated_at=timezone.now(),
                reverses_batch=source,
                reversal_reason="database coverage probe",
            )
            original = originals[0]
            reversal_item = DepreciationBatchItem.objects.create(
                company=company,
                batch=reversal,
                asset=original.asset,
                depreciation_profile=original.depreciation_profile,
                calculation_method="straight_line",
                opening_book_value=original.book_value_after,
                depreciable_floor=Decimal("600.00"),
                eligible_fraction=Decimal("1"),
                calculated_unrounded=original.amount,
                planned_amount=original.amount,
                closing_book_value=Decimal("12000.00"),
                calculation_snapshot_json={"reversal_of_entry_id": str(original.pk)},
                status="ready",
            )
            DepreciationEntry.objects.create(
                company=company,
                asset=original.asset,
                depreciation_profile=original.depreciation_profile,
                entry_date=timezone.localdate(),
                period_start=source.period_start,
                period_end=source.period_end,
                source_type="batch",
                batch_item=reversal_item,
                amount=-original.amount,
                accumulated_depreciation_after=Decimal("0.00"),
                book_value_after=Decimal("12000.00"),
                reversal_of=original,
                posted_by=actor,
                posted_at=timezone.now(),
            )
            reversal.status = "confirmed"
            reversal.confirmed_by = actor
            reversal.confirmed_at = timezone.now()
            reversal.save(update_fields=["status", "confirmed_by", "confirmed_at"])
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config(%s, %s, true)",
                    ["eam_lite.controlled_finance_batch_reversal", "on"],
                )
            DepreciationBatch.objects.filter(pk=source.pk).update(status="reversed")

    source.refresh_from_db()
    assert source.status == "confirmed"
    assert not DepreciationBatch.objects.filter(
        idempotency_key="s11-partial-reversal"
    ).exists()
