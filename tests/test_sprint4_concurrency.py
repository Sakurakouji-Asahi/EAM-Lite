from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.finance.models import DepreciationBatch
from apps.finance.models import FinanceFormalizationRequest
from apps.finance.services import confirm_asset_finance, generate_depreciation_batch
from apps.assets.models import AssetQrIdentity
from apps.assets.services import submit_asset_for_finance
from apps.masterdata.models import IssuedCode
from tests.test_sprint3_support import (
    add_photo,
    make_asset,
    make_category,
    make_department,
    make_employee,
    make_location_tree,
)
from tests.test_sprint4_database_acceptance import _pending_asset_context
from tests.test_sprint4_services import _profile_context


pytestmark = [pytest.mark.django_db(transaction=True)]


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock acceptance test")


def test_postgresql_concurrent_generate_same_company_and_key_is_idempotent():
    _postgresql_only()
    company, actor, _management, _admin, _asset, _finance, _profile = _profile_context()
    barrier = Barrier(2)

    def generate():
        close_old_connections()
        try:
            barrier.wait()
            result = generate_depreciation_batch(
                actor=actor,
                company=company,
                period_start=date(2024, 1, 1),
                period_end=date(2024, 2, 1),
                idempotency_key="pg-same-generate",
            )
            return result.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _index: generate(), range(2)))

    assert ids[0] == ids[1]
    assert DepreciationBatch.objects.filter(
        company=company, idempotency_key="pg-same-generate"
    ).count() == 1


def test_postgresql_company_lock_serializes_different_asset_batch_generation():
    _postgresql_only()
    company, actor, _management, _admin, _asset, _finance, _profile = _profile_context()
    # This is deliberately a second Profile in the same company, not a second
    # database.  Both concurrent calls must take Company first and complete
    # without a deadlock or two active batches for the same company/period.
    barrier = Barrier(2)

    def generate(key):
        close_old_connections()
        try:
            barrier.wait()
            result = generate_depreciation_batch(
                actor=actor,
                company=company,
                period_start=date(2024, 1, 1),
                period_end=date(2024, 2, 1),
                idempotency_key=key,
            )
            return result.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(generate, ("pg-company-a", "pg-company-b")))

    assert len(set(results)) == 2
    assert DepreciationBatch.objects.filter(
        company=company, period_start=date(2024, 1, 1), status="draft"
    ).count() == 2


def _formalize_nonfixed(*, actor, asset, key):
    return confirm_asset_finance(
        actor=actor,
        asset=asset,
        finance_data={
            "accounting_treatment": "controlled_non_fixed",
            "original_cost": Decimal("100.00"),
        },
        code_effective_date=timezone.localdate(),
        idempotency_key=key,
        reason="PostgreSQL 并发正式化",
    )


def _second_pending_asset(context, suffix):
    company = context["company"]
    actor = context["equipment"]
    department = make_department(company, f"{suffix}-D")
    employee = make_employee(company, department, f"{suffix}-E")
    category = make_category(company, f"{suffix}-CAT")
    _site, _area, location = make_location_tree(company, f"{suffix}-L")
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
        commissioning_date=date(2026, 8, 1),
        asset_name=f"{suffix} 并发资产",
    )
    add_photo(actor, asset)
    return submit_asset_for_finance(actor=actor, asset=asset)


def test_postgresql_same_asset_double_formalization_is_persistently_idempotent():
    _postgresql_only()
    context = _pending_asset_context(prefix="S4CSAME")
    barrier = Barrier(2)

    def formalize():
        close_old_connections()
        try:
            barrier.wait()
            return _formalize_nonfixed(
                actor=context["finance"],
                asset=context["asset"],
                key="pg-same-formalization",
            ).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: formalize(), range(2)))

    assert results[0] == results[1]
    assert FinanceFormalizationRequest.objects.filter(asset=context["asset"]).count() == 1
    assert IssuedCode.objects.filter(company=context["company"]).count() == 1
    assert AssetQrIdentity.objects.filter(asset=context["asset"], status="active").count() == 1


def test_postgresql_different_assets_get_unique_codes_under_concurrency():
    _postgresql_only()
    context = _pending_asset_context(prefix="S4CMULTI")
    second = _second_pending_asset(context, "S4CMULTI2")
    work = (
        (context["asset"], "pg-formalize-a"),
        (second, "pg-formalize-b"),
    )
    barrier = Barrier(2)

    def formalize(item):
        close_old_connections()
        try:
            asset, key = item
            barrier.wait()
            result = _formalize_nonfixed(
                actor=context["finance"], asset=asset, key=key
            )
            return result.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(formalize, work))

    codes = list(
        IssuedCode.objects.filter(company=context["company"]).values_list(
            "display_code", flat=True
        )
    )
    assert len(set(results)) == 2
    assert len(codes) == 2
    assert len(set(codes)) == 2
    assert FinanceFormalizationRequest.objects.filter(company=context["company"]).count() == 2
