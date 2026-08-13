from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import DatabaseError, close_old_connections, connection
from django.utils.functional import empty

from apps.accounts.models import User
from apps.assets.models import Asset, AssetExternalReference
from apps.masterdata.models import Company
from apps.reports.excel import write_report_workbook
from apps.reports.models import ExportLog
from apps.reports.queries import build_report_dataset
from apps.reports.services import (
    create_or_correct_external_reference,
    generate_report_export,
)
from tests.test_sprint4_acceptance import _base_context
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint11 concurrency requires PostgreSQL 18.4")


def test_postgresql_concurrent_same_key_export_returns_one_completed_version(
    settings, tmp_path
):
    _postgresql_only()
    context, _asset, _qr = active_asset_context("S11CONEXPORT")
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "tmp"
    settings.MEDIA_ROOT.mkdir()
    settings.IMPORT_TEMP_ROOT.mkdir()
    default_storage._wrapped = empty
    company_id = context["company"].pk
    actor_id = context["finance"].pk
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", "")
            actor = User.objects.get(pk=actor_id)
            company = Company.objects.get(pk=company_id)
            try:
                result = generate_report_export(
                    actor=actor,
                    company=company,
                    report_key="asset_ledger",
                    filters={},
                    idempotency_key="S11CONEXPORT-same",
                )
            except (ValidationError, DatabaseError) as exc:
                return ("error", str(exc))
            return ("ok", str(result.pk))
        finally:
            close_old_connections()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=90) for future in (pool.submit(worker), pool.submit(worker))]
        assert [kind for kind, _value in results] == ["ok", "ok"]
        assert len({value for _kind, value in results}) == 1
        export_log = ExportLog.objects.get(
            company_id=company_id, idempotency_key="S11CONEXPORT-same"
        )
        assert export_log.status == "completed"
        assert ExportLog.objects.filter(
            company_id=company_id, idempotency_key="S11CONEXPORT-same"
        ).count() == 1
    finally:
        default_storage._wrapped = empty


def test_postgresql_concurrent_external_reference_claim_has_one_winner():
    _postgresql_only()
    context, first_asset, _qr = active_asset_context("S11CONEXT")
    from tests.test_sprint11_services_database import _second_formal_asset

    second_asset = _second_formal_asset(context, "S11CONEXT-B")
    actor_id = context["finance"].pk
    asset_ids = (first_asset.pk, second_asset.pk)
    barrier = Barrier(2)

    def worker(asset_id):
        close_old_connections()
        try:
            try:
                barrier.wait(timeout=20)
            except BrokenBarrierError:
                return ("barrier", "")
            actor = User.objects.get(pk=actor_id)
            asset = Asset.objects.get(pk=asset_id)
            try:
                result = create_or_correct_external_reference(
                    actor=actor,
                    asset=asset,
                    reference_value="000-CONCURRENT",
                    reason="Sprint11 并发认领",
                )
            except (ValidationError, DatabaseError) as exc:
                return ("rejected", str(exc))
            return ("ok", str(result.pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, asset_ids))
    assert sorted(kind for kind, _value in results) == ["ok", "rejected"]
    assert AssetExternalReference.objects.filter(
        company=context["company"], normalized_value="000-concurrent"
    ).count() == 1


@pytest.mark.django_db(transaction=True)
def test_5000_asset_query_and_xlsx_export_smoke_meets_120_seconds():
    context = _base_context("S11PERF")
    drafts = [
        Asset(
            company=context["company"],
            asset_name=f"性能资产 {index:04d}",
            category=context["category"],
            department=context["department"],
            responsible_employee=context["employee"],
            location=context["location"],
            commissioning_date=None,
            created_by=context["equipment"],
            updated_by=context["equipment"],
            initialized_by=context["equipment"],
        )
        for index in range(5000)
    ]
    Asset._base_manager.bulk_create(drafts, batch_size=500)
    started = time.perf_counter()
    dataset = build_report_dataset(
        actor=context["equipment"],
        company=context["company"],
        report_key="asset_ledger",
        filters={"include_drafts": True},
    )
    output = io.BytesIO()
    write_report_workbook(dataset, output)
    elapsed = time.perf_counter() - started
    assert dataset.row_count == 5000
    assert output.getbuffer().nbytes > 0
    assert elapsed <= 120
