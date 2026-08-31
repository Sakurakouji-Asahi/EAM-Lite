from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.finance.models import (
    DepreciationBatch,
    DepreciationBatchItem,
    DepreciationEntry,
)
from apps.finance.reporting import balances_by_asset, tplus_period_components
from apps.finance.services import (
    confirm_depreciation_batch,
    create_value_adjustment,
    generate_depreciation_batch,
    reverse_value_adjustment,
)
from apps.reports.queries import (
    ReportValidationError,
    build_dashboard,
    build_report_dataset,
    build_tplus_dataset,
)
from apps.reports.schemas import TPLUS_TOTAL_METRICS
from tests.test_sprint4_acceptance import _base_context, _pending_asset
from tests.test_sprint4_services import _confirmed_entry, _profile_context
from tests.test_sprint7_disposal_services import (
    _add_disposal_evidence,
    _initiate,
    _record_and_lock,
)
from tests.test_sprint7_support import active_fixed_asset_context
from apps.assets.lifecycle_services import complete_disposal


pytestmark = pytest.mark.django_db(transaction=True)


def _manual_entry(
    *, profile, start, amount, actor, key, calculation_method="manual"
):
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    batch = DepreciationBatch.objects.create(
        company=profile.company,
        period_start=start,
        period_end=end,
        generation_no=1,
        batch_type="regular",
        status="draft",
        idempotency_key=key,
        request_hash="b" * 64,
        generated_by=actor,
        generated_at=timezone.now(),
        confirmed_by=None,
        confirmed_at=None,
    )
    manual_fields = {}
    if calculation_method == "manual":
        manual_fields = {
            "manual_amount": amount,
            "manual_reason": "Sprint11 手工折旧口径",
            "manual_entered_by": actor,
            "manual_entered_at": timezone.now(),
        }
    item = DepreciationBatchItem.objects.create(
        company=profile.company,
        batch=batch,
        asset=profile.asset,
        depreciation_profile=profile,
        calculation_method=calculation_method,
        opening_book_value=Decimal("12000.00"),
        depreciable_floor=Decimal("600.00"),
        eligible_fraction=Decimal("1"),
        calculated_unrounded=amount,
        planned_amount=amount,
        closing_book_value=Decimal("12000.00") - amount,
        calculation_snapshot_json={"engine_version": "test"},
        status="ready",
        **manual_fields,
    )
    entry = DepreciationEntry.objects.create(
        company=profile.company,
        asset=profile.asset,
        depreciation_profile=profile,
        entry_date=end,
        period_start=start,
        period_end=end,
        source_type="batch",
        batch_item=item,
        amount=amount,
        accumulated_depreciation_after=amount,
        book_value_after=Decimal("12000.00") - amount,
        posted_by=actor,
        posted_at=timezone.now(),
    )
    batch.status = "confirmed"
    batch.confirmed_by = actor
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    return entry


def test_tplus_components_distinguish_automatic_from_manual_entries():
    company, actor, _management, _admin, asset, _finance, profile = _profile_context()
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    # Match the production confirmation boundary so PostgreSQL's deferred
    # source guard observes the entry and confirmed batch together.
    with transaction.atomic():
        _manual_entry(
            profile=profile,
            start=period_start,
            amount=Decimal("11.00"),
            actor=actor,
            key="s11-manual",
        )
    component = tplus_period_components(
        company=company,
        asset_ids=[asset.pk],
        period_start=period_start,
        period_end=period_end,
    )[asset.pk]
    assert component["automatic_depreciation"] == Decimal("0.00")
    assert component["manual_depreciation"] == Decimal("11.00")


@pytest.mark.parametrize(
    ("original_amount", "expected_reversal"),
    ((Decimal("9.00"), Decimal("-9.00")), (Decimal("-4.00"), Decimal("4.00"))),
)
def test_tplus_adjustment_reversal_keeps_signed_direction(
    original_amount, expected_reversal
):
    company, actor, _management, _admin, asset, _finance, profile = _profile_context()
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    if original_amount < 0:
        DepreciationEntry.objects.create(
            company=company,
            asset=asset,
            depreciation_profile=profile,
            entry_date=period_start - timedelta(days=1),
            period_start=period_start - timedelta(days=2),
            period_end=period_start - timedelta(days=1),
            source_type="opening",
            opening_profile=profile,
            amount=Decimal("10.00"),
            accumulated_depreciation_after=Decimal("10.00"),
            book_value_after=Decimal("11990.00"),
            posted_by=actor,
            posted_at=timezone.now(),
        )
    adjustment = create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type="depreciation_adjustment",
        amount=original_amount,
        effective_date=period_start,
        reason="Sprint11 折旧调整",
    )
    reversal = reverse_value_adjustment(
        actor=actor,
        adjustment=adjustment,
        reason="Sprint11 验证冲销代数方向",
    )
    original_component = tplus_period_components(
        company=company,
        asset_ids=[asset.pk],
        period_start=period_start,
        period_end=period_end,
    )[asset.pk]
    reversal_entry = DepreciationEntry.objects.get(value_adjustment=reversal)
    reversal_start = reversal_entry.entry_date.replace(day=1)
    reversal_end = (
        reversal_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1)
    reversal_component = tplus_period_components(
        company=company,
        asset_ids=[asset.pk],
        period_start=reversal_start,
        period_end=reversal_end,
    )[asset.pk]
    assert original_component["adjustment_net"] == original_amount
    assert original_component["reversal_net"] == (
        expected_reversal if reversal_start == period_start else Decimal("0.00")
    )
    assert reversal_component["reversal_net"] == expected_reversal


@pytest.mark.parametrize("adjustment_type", ("cost_correction", "impairment"))
def test_cost_and_impairment_adjustments_never_enter_depreciation_adjustment_net(
    adjustment_type,
):
    company, actor, _management, _admin, asset, _finance, profile = _profile_context(
        method="no_depreciation"
    )
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type=adjustment_type,
        amount=Decimal("50.00"),
        effective_date=period_start,
        reason="Sprint11 非折旧调整分流",
    )
    component = tplus_period_components(
        company=company,
        asset_ids=[asset.pk],
        period_start=period_start,
        period_end=period_end,
    )[asset.pk]
    assert component["adjustment_net"] == Decimal("0.00")


def test_yearly_batch_is_attributed_once_to_its_annual_posting_month():
    company, actor, _management, _admin, asset, _finance, profile = _profile_context()
    with transaction.atomic():
        entry = _manual_entry(
            profile=profile,
            start=date(2026, 8, 1),
            amount=Decimal("2280.00"),
            actor=actor,
            key="s11-yearly",
            calculation_method="straight_line",
        )
    july = tplus_period_components(
        company=company,
        asset_ids=[asset.pk],
        period_start=date(2026, 7, 1),
        period_end=date(2026, 8, 1),
    )[asset.pk]
    august = tplus_period_components(
        company=company,
        asset_ids=[asset.pk],
        period_start=date(2026, 8, 1),
        period_end=date(2026, 9, 1),
    )[asset.pk]
    september = tplus_period_components(
        company=company,
        asset_ids=[asset.pk],
        period_start=date(2026, 9, 1),
        period_end=date(2026, 10, 1),
    )[asset.pk]
    assert july["automatic_depreciation"] == Decimal("0.00")
    assert august["automatic_depreciation"] == Decimal("2280.00")
    assert september["automatic_depreciation"] == Decimal("0.00")
    assert september["opening_accumulated_depreciation"] == Decimal("2280.00")


def test_tplus_blocks_draft_batch():
    context, asset, _qr, profile, _policy = active_fixed_asset_context("S11BLOCK")
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    DepreciationBatch.objects.create(
        company=context["company"],
        period_start=period_start,
        period_end=period_end,
        generation_no=1,
        batch_type="regular",
        status="draft",
        idempotency_key="s11-block-draft",
        request_hash="c" * 64,
        generated_by=context["finance"],
        generated_at=timezone.now(),
    )
    with pytest.raises(ReportValidationError, match="未确认折旧批次"):
        build_tplus_dataset(
            actor=context["finance"],
            company=context["company"],
            period_start=period_start,
            period_end=period_end,
        )


def test_sqlite_reports_exclude_entry_from_draft_batch():
    if connection.vendor != "sqlite":
        pytest.skip("SQLite read-side regression; PostgreSQL rejects the commit")
    context, asset, _qr, profile, _policy = active_fixed_asset_context("S11DRAFTREAD")
    company, actor = context["company"], context["finance"]
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    next_period_end = (period_end.replace(day=28) + timedelta(days=4)).replace(day=1)
    _confirmed_entry(
        profile=profile,
        start=period_start,
        amount=Decimal("190.00"),
        actor=actor,
        status="draft",
    )

    balance = balances_by_asset(
        company=company, asset_ids=[asset.pk], boundary=period_end
    )[asset.pk]
    assert balance.accumulated_depreciation == Decimal("0.00")
    assert balance.book_value == Decimal("12000.00")

    fixed = build_report_dataset(
        actor=actor,
        company=company,
        report_key="fixed_asset_detail",
        filters={"as_of_date": period_end - timedelta(days=1)},
    )
    fixed_row = next(row for row in fixed.rows if row["asset_code"] == asset.asset_code)
    assert fixed_row["actual_accumulated_depreciation"] == Decimal("0.00")
    assert fixed_row["actual_book_value"] == Decimal("12000.00")

    period_filters = {
        "period_start": period_start,
        "period_end": period_end - timedelta(days=1),
    }
    for report_key in ("depreciation_detail", "monthly_depreciation"):
        report = build_report_dataset(
            actor=actor,
            company=company,
            report_key=report_key,
            filters=period_filters,
        )
        assert asset.asset_code not in {row["asset_code"] for row in report.rows}

    dashboard = build_dashboard(actor=actor, company=company)
    assert dashboard["financial"] == {
        "original_cost": Decimal("12000.00"),
        "accumulated_depreciation": Decimal("0.00"),
        "book_value": Decimal("12000.00"),
        "current_month_depreciation": Decimal("0.00"),
    }

    tplus = build_tplus_dataset(
        actor=actor,
        company=company,
        period_start=period_end,
        period_end=next_period_end,
    )
    tplus_row = next(row for row in tplus.asset_rows if row["asset_code"] == asset.asset_code)
    assert tplus_row["opening_accumulated_depreciation"] == Decimal("0.00")
    assert tplus_row["ending_accumulated_depreciation"] == Decimal("0.00")
    assert tplus.entry_rows == ()

def test_completed_disposal_uses_locked_snapshot_and_tplus_registered_totals():
    context, asset, _qr = active_fixed_asset_context(
        "S11DISPSNAP", stop_rule="next_month"
    )[:3]
    disposal = _initiate(
        context,
        asset,
        "S11DISPSNAP-start",
        disposal_type="sale",
    )
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    batch = generate_depreciation_batch(
        actor=context["finance"],
        company=context["company"],
        period_start=period_start,
        period_end=period_end,
        idempotency_key="S11DISPSNAP-batch",
    )
    confirm_depreciation_batch(
        actor=context["finance"],
        batch=batch,
        reason="Sprint11 处置前确认当月折旧",
    )
    disposal = _record_and_lock(context, disposal, "S11DISPSNAP", income="88.00")
    _add_disposal_evidence(context, disposal, "S11DISPSNAP")
    disposal = complete_disposal(
        actor=context["equipment"],
        disposal=disposal,
        idempotency_key="S11DISPSNAP-complete",
    )
    disposal_report = build_report_dataset(
        actor=context["finance"],
        company=context["company"],
        report_key="disposal_list",
        filters={"period_start": period_start, "period_end": period_end},
    )
    disposal_row = next(
        item for item in disposal_report.rows if item["asset_code"] == asset.asset_code
    )
    assert disposal_row["original_cost_snapshot"] == disposal.original_cost_snapshot
    assert disposal_row["accumulated_depreciation_snapshot"] == (
        disposal.actual_accumulated_depreciation_snapshot
    )
    assert disposal_row["impairment_snapshot"] == disposal.impairment_snapshot
    assert disposal_row["book_value_snapshot"] == disposal.book_value_snapshot

    report = build_tplus_dataset(
        actor=context["finance"],
        company=context["company"],
        period_start=period_start,
        period_end=period_end,
    )
    row = next(item for item in report.asset_rows if item["asset_code"] == asset.asset_code)
    assert row["disposal_date"] == disposal.actual_disposal_date
    assert row["disposal_type"] == disposal.get_disposal_type_display()
    assert row["disposal_income"] == Decimal("88.00")
    assert set(report.totals) == set(TPLUS_TOTAL_METRICS)
    assert report.totals["disposal_income"] == Decimal("88.00")


def test_dashboard_and_monthly_report_exclude_current_month_disposed_fixed_asset():
    context, asset, _qr = active_fixed_asset_context(
        "S11DASHDISP", stop_rule="next_month"
    )[:3]
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    batch = generate_depreciation_batch(
        actor=context["finance"],
        company=context["company"],
        period_start=period_start,
        period_end=period_end,
        idempotency_key="S11DASHDISP-batch",
    )
    confirm_depreciation_batch(
        actor=context["finance"],
        batch=batch,
        reason="Sprint11 Dashboard 终态口径验收",
    )
    disposal = _initiate(context, asset, "S11DASHDISP-start")
    disposal = _record_and_lock(context, disposal, "S11DASHDISP")
    _add_disposal_evidence(context, disposal, "S11DASHDISP")
    complete_disposal(
        actor=context["equipment"],
        disposal=disposal,
        idempotency_key="S11DASHDISP-complete",
    )

    dashboard = build_dashboard(
        actor=context["finance"], company=context["company"]
    )
    assert dashboard["financial"] == {
        "original_cost": Decimal("0.00"),
        "accumulated_depreciation": Decimal("0.00"),
        "book_value": Decimal("0.00"),
        "current_month_depreciation": Decimal("0.00"),
    }
    monthly = build_report_dataset(
        actor=context["finance"],
        company=context["company"],
        report_key="monthly_depreciation",
        filters={
            "period_start": period_start,
            "period_end": period_end - timedelta(days=1),
            "include_disposed": False,
        },
    )
    assert asset.asset_code not in {row["asset_code"] for row in monthly.rows}
