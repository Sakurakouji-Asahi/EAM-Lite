from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, timedelta
from decimal import Decimal
from threading import Event

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.finance.domain import ScheduleInput, generate_schedule
from apps.finance.models import (
    AssetDepreciationProfile,
    AssetFinance,
    AssetWorkUsage,
    DepreciationBatch,
    DepreciationBatchItem,
    DepreciationEntry,
    DepreciationPolicy,
)
from apps.finance.services import (
    _profile_spec,
    clone_asset_depreciation_profile,
    confirm_depreciation_batch,
    create_profile_event,
    generate_depreciation_batch,
    record_work_usage,
    review_profile_actual_continuation_date,
    reverse_depreciation_batch,
    run_theoretical_depreciation,
)
from apps.imports.services import (
    _asset_theoretical_summary,
    upload_and_validate_import,
)
from apps.masterdata.models import FixedAssetCategory
from tests.test_sprint4_services import _asset, _profile_context, _users_and_company
from tests.test_sprint5_support import (
    add_finance_row,
    asset_workbook_upload,
    finance_configuration,
    physical_row,
    sprint5_context,
)


pytestmark = pytest.mark.django_db


def _custom_profile_context(
    *,
    method,
    posting_period="monthly",
    start_date=date(2024, 1, 1),
    annual_posting_month=None,
    actual_continuation_date=None,
    opening_ad=Decimal("0.00"),
    opening_book=Decimal("12000.00"),
    review_required=False,
):
    company, actor, management, admin = _users_and_company()
    suffix = "UOP" if method == "units_of_production" else "YEARLY"
    asset = _asset(company, actor, suffix=suffix)
    fixed_category = FixedAssetCategory.objects.create(
        company=company,
        code=f"{suffix}-FA",
        normalized_code=f"{suffix.lower()}-fa",
        name="纠正测试资产",
        useful_life_months_default=60,
    )
    finance = AssetFinance.objects.create(
        company=company,
        asset=asset,
        accounting_treatment="fixed_asset",
        recognition_threshold_snapshot=Decimal("5000.00"),
        fixed_asset_category=fixed_category,
        original_cost=Decimal("12000.00"),
        capitalization_date=start_date,
        impairment_balance_cache=Decimal("0.00"),
        finance_confirmed_by=actor,
        finance_confirmed_at=timezone.now(),
    )
    policy = DepreciationPolicy.objects.create(
        company=company,
        policy_key=f"P-{suffix}-60",
        version=1,
        name="纠正测试政策",
        method=method,
        posting_period=posting_period,
        start_rule="specified_date",
        stop_rule="event_date",
        default_useful_life_months=60,
        default_salvage_mode="rate",
        default_salvage_rate=Decimal("0.05"),
        annual_posting_month=annual_posting_month,
        work_unit="台时" if method == "units_of_production" else "",
        status="active",
        is_default=True,
        effective_from=start_date,
        created_by=actor,
    )
    continuation = (
        None
        if review_required
        else actual_continuation_date or start_date
    )
    profile = AssetDepreciationProfile.objects.create(
        company=company,
        asset=asset,
        depreciation_policy=policy,
        version=1,
        method=method,
        posting_period=posting_period,
        start_rule="specified_date",
        stop_rule="event_date",
        start_date=start_date,
        actual_continuation_date=continuation,
        actual_continuation_review_required=review_required,
        useful_life_months=60,
        salvage_mode="rate",
        salvage_rate=Decimal("0.05"),
        opening_book_value=opening_book,
        opening_actual_accumulated_depreciation=opening_ad,
        expected_total_units=(
            Decimal("100.000000") if method == "units_of_production" else None
        ),
        work_unit="台时" if method == "units_of_production" else "",
        annual_posting_month=annual_posting_month,
        effective_from=continuation or start_date,
        status="active",
        created_by=actor,
    )
    return company, actor, management, admin, asset, finance, profile


def _direct_work_usage(
    *, profile, actor, period_start, period_end, opening, current
):
    return AssetWorkUsage.objects.create(
        company_id=profile.company_id,
        asset_id=profile.asset_id,
        depreciation_profile_id=profile.pk,
        period_start=period_start,
        period_end=period_end,
        work_unit=profile.work_unit,
        opening_accumulated_units=opening,
        current_units=current,
        closing_accumulated_units=opening + current,
        entered_by_id=actor.pk,
        entered_at=timezone.now(),
    )


def test_opening_balance_requires_explicit_actual_continuation_date():
    _company, _actor, _management, _admin, asset, finance, profile = (
        _profile_context()
    )
    profile_data = {
        "opening_actual_accumulated_depreciation": Decimal("4560.00"),
        "opening_book_value": Decimal("7440.00"),
    }

    with pytest.raises(ValidationError) as exc_info:
        _profile_spec(
            asset=asset,
            finance_data={
                "original_cost": finance.original_cost,
                "fixed_asset_category": finance.fixed_asset_category,
            },
            profile_data=profile_data,
            policy=profile.depreciation_policy,
        )

    assert "actual_continuation_date" in exc_info.value.message_dict


def test_legacy_profile_requires_one_time_review_before_batch_generation():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="straight_line", review_required=True)
    )
    with pytest.raises(ValidationError, match="尚待财务复核"):
        generate_depreciation_batch(
            actor=actor,
            company=company,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 2, 1),
            idempotency_key="pending-continuation-block",
        )

    reviewed = review_profile_actual_continuation_date(
        actor=actor,
        profile=profile,
        actual_continuation_date=date(2024, 1, 16),
        reason="核对原系统折旧承接台账",
    )
    assert reviewed.actual_continuation_date == date(2024, 1, 16)
    assert reviewed.actual_continuation_review_required is False
    assert AuditLog.objects.filter(
        action="depreciation_profile_continuation_review",
        object_id=str(profile.pk),
    ).exists()
    assert generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="reviewed-continuation-generate",
    ).items.get().status == "ready"


def test_continuation_review_rejects_confirmed_period_crossing_selected_date():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="straight_line", review_required=True)
    )
    now = timezone.now()
    batch = DepreciationBatch.objects.create(
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        generation_no=1,
        batch_type="regular",
        status="draft",
        idempotency_key="pre-review-crossing-period",
        request_hash="c" * 64,
        generated_by=actor,
        generated_at=now,
    )
    item = DepreciationBatchItem.objects.create(
        company=company,
        batch=batch,
        asset=profile.asset,
        depreciation_profile=profile,
        calculation_method="straight_line",
        opening_book_value=Decimal("12000.00"),
        depreciable_floor=Decimal("600.00"),
        eligible_fraction=Decimal("1"),
        calculated_unrounded=Decimal("190.00"),
        planned_amount=Decimal("190.00"),
        closing_book_value=Decimal("11810.00"),
        status="ready",
    )
    DepreciationEntry.objects.create(
        company=company,
        asset=profile.asset,
        depreciation_profile=profile,
        entry_date=date(2024, 2, 1),
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        source_type="batch",
        batch_item=item,
        amount=Decimal("190.00"),
        accumulated_depreciation_after=Decimal("190.00"),
        book_value_after=Decimal("11810.00"),
        posted_by=actor,
        posted_at=now,
    )
    batch.status = "confirmed"
    batch.confirmed_by = actor
    batch.confirmed_at = now
    batch.save(update_fields=("status", "confirmed_by", "confirmed_at"))

    with pytest.raises(ValidationError, match="起始早于实际接续日"):
        review_profile_actual_continuation_date(
            actor=actor,
            profile=profile,
            actual_continuation_date=date(2024, 1, 16),
            reason="核对旧台账接续边界",
        )

    profile.refresh_from_db()
    assert profile.actual_continuation_date is None
    assert profile.actual_continuation_review_required is True


def test_legacy_profile_blocks_confirmation_of_pre_upgrade_draft_batch():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="straight_line", review_required=True)
    )
    batch = DepreciationBatch.objects.create(
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        generation_no=1,
        batch_type="regular",
        status="draft",
        idempotency_key="pre-upgrade-pending-confirm",
        request_hash="a" * 64,
        generated_by=actor,
        generated_at=timezone.now(),
    )
    DepreciationBatchItem.objects.create(
        company=company,
        batch=batch,
        asset=profile.asset,
        depreciation_profile=profile,
        calculation_method="straight_line",
        opening_book_value=Decimal("12000.00"),
        depreciable_floor=Decimal("600.00"),
        eligible_fraction=Decimal("1"),
        calculated_unrounded=Decimal("190.00"),
        planned_amount=Decimal("190.00"),
        closing_book_value=Decimal("11810.00"),
        calculation_snapshot_json={},
        status="ready",
    )

    with pytest.raises(ValidationError, match="尚待财务复核"):
        confirm_depreciation_batch(actor=actor, batch=batch, reason="月度确认")

    batch.refresh_from_db()
    assert batch.status == "draft"
    assert not DepreciationEntry.objects.filter(batch_item__batch=batch).exists()


def test_zero_opening_historical_asset_still_requires_continuation_date():
    _company, _actor, _management, _admin, asset, finance, profile = (
        _profile_context()
    )

    with pytest.raises(ValidationError) as exc_info:
        _profile_spec(
            asset=asset,
            finance_data={
                "original_cost": finance.original_cost,
                "fixed_asset_category": finance.fixed_asset_category,
            },
            profile_data={
                "start_rule": "specified_date",
                "specified_start": date(2020, 1, 1),
                "allow_historical_start": True,
                "opening_actual_accumulated_depreciation": Decimal("0.00"),
                "opening_impairment": Decimal("0.00"),
                "opening_book_value": Decimal("12000.00"),
            },
            policy=profile.depreciation_policy,
        )

    assert "actual_continuation_date" in exc_info.value.message_dict


def test_import_preview_and_saved_theoretical_run_share_inclusive_as_of_date():
    _company, actor, _management, _admin, asset, _finance, _profile = (
        _profile_context()
    )
    parameters = {
        "original_cost": Decimal("12000.00"),
        "method": "straight_line",
        "posting_period": "monthly",
        "commissioning_date": date(2024, 1, 1),
        "start_rule": "specified_date",
        "specified_start": date(2024, 1, 1),
        "useful_life_months": 60,
        "salvage_mode": "rate",
        "salvage_rate": Decimal("0.05"),
    }
    as_of_date = date(2025, 12, 31)
    preview = _asset_theoretical_summary(
        generate_schedule(ScheduleInput(**parameters)), as_of_date=as_of_date
    )

    run = run_theoretical_depreciation(
        actor=actor,
        asset=asset,
        as_of_date=as_of_date,
        parameters=parameters,
        idempotency_key="correction-inclusive-as-of",
    )
    saved = run.lines.order_by("period_end").last()

    assert run.lines.count() == preview["period_count"] == 24
    assert saved.period_end == date(2026, 1, 1)
    assert saved.theoretical_accumulated == Decimal(
        preview["planned_accumulated_depreciation"]
    ) == Decimal("4560.00")
    assert saved.theoretical_book_value == Decimal(
        preview["theoretical_book_value"]
    ) == Decimal("7440.00")


def test_work_usage_rejects_backfill_across_gaps_and_never_exceeds_total():
    _company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="units_of_production")
    )

    record_work_usage(
        actor=actor,
        profile=profile,
        period_start=date(2024, 3, 1),
        period_end=date(2024, 4, 1),
        current_units=Decimal("60.000000"),
        work_unit="台时",
    )
    capped = record_work_usage(
        actor=actor,
        profile=profile,
        period_start=date(2024, 5, 1),
        period_end=date(2024, 6, 1),
        current_units=Decimal("100.000000"),
        work_unit="台时",
    )
    assert capped.current_units == Decimal("40.000000")
    assert capped.closing_accumulated_units == Decimal("100.000000")

    with pytest.raises(ValidationError, match="倒序"):
        record_work_usage(
            actor=actor,
            profile=profile,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 2, 1),
            current_units=Decimal("50.000000"),
            work_unit="台时",
        )

    total = profile.work_usages.aggregate(total=Sum("current_units"))["total"]
    assert total == Decimal("100.000000")


def test_uop_midmonth_continuation_uses_full_month_units_without_day_proration():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(
            method="units_of_production", start_date=date(2024, 1, 16)
        )
    )

    usage = record_work_usage(
        actor=actor,
        profile=profile,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        current_units=Decimal("10.000000"),
        work_unit="台时",
    )
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="correction-uop-midmonth-continuation",
    )
    item = batch.items.get(depreciation_profile=profile)

    assert (usage.period_start, usage.period_end) == (
        date(2024, 1, 1),
        date(2024, 2, 1),
    )
    assert item.eligible_fraction == Decimal("0.5161290323")
    assert item.usage_units == Decimal("10.000000")
    assert item.calculated_unrounded == Decimal("1140.0000000000")
    assert item.planned_amount == Decimal("1140.00")
    assert batch.items.filter(asset=profile.asset).count() == 1


def test_uop_midmonth_opening_is_included_in_generation_and_confirmation_cap():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(
            method="units_of_production",
            start_date=date(2024, 1, 1),
            actual_continuation_date=date(2024, 1, 16),
            opening_ad=Decimal("10000.00"),
            opening_book=Decimal("2000.00"),
        )
    )
    DepreciationEntry.objects.create(
        company=company,
        asset=profile.asset,
        depreciation_profile=profile,
        entry_date=date(2024, 1, 16),
        period_start=date(2024, 1, 16),
        period_end=date(2024, 1, 17),
        source_type="opening",
        opening_profile=profile,
        amount=Decimal("10000.00"),
        accumulated_depreciation_after=Decimal("10000.00"),
        book_value_after=Decimal("2000.00"),
        posted_by=actor,
        posted_at=timezone.now(),
    )
    record_work_usage(
        actor=actor,
        profile=profile,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        current_units=Decimal("20.000000"),
        work_unit="台时",
    )

    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="correction-uop-midmonth-opening-cap",
    )
    item = batch.items.get(depreciation_profile=profile)
    assert item.opening_book_value == Decimal("2000.00")
    assert item.calculated_unrounded == Decimal("2280.0000000000")
    assert item.planned_amount == Decimal("1400.00")
    assert item.calculation_snapshot_json["source_snapshot"][
        "balance_as_of"
    ] == "2024-01-16"

    confirm_depreciation_batch(actor=actor, batch=batch, reason="月度确认")
    entry = DepreciationEntry.objects.get(batch_item=item)
    actual_ad = profile.asset.depreciation_entries.aggregate(total=Sum("amount"))[
        "total"
    ]
    assert entry.accumulated_depreciation_after == actual_ad == Decimal("11400.00")
    assert entry.book_value_after == Decimal("600.00")


@pytest.mark.django_db(transaction=True)
def test_uop_midmonth_suspend_resume_does_not_prorate_recorded_units():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="units_of_production")
    )
    record_work_usage(
        actor=actor,
        profile=profile,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        current_units=Decimal("10.000000"),
        work_unit="台时",
    )
    create_profile_event(
        actor=actor,
        profile=profile,
        event_type="suspend",
        effective_date=date(2024, 1, 11),
        reason="月中暂停",
    )
    create_profile_event(
        actor=actor,
        profile=profile,
        event_type="resume",
        effective_date=date(2024, 1, 21),
        reason="月中恢复",
    )

    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="correction-uop-midmonth-suspension",
    )
    item = batch.items.get(depreciation_profile=profile)

    assert item.eligible_fraction == Decimal("0.6774193548")
    assert item.calculated_unrounded == Decimal("1140.0000000000")
    assert item.planned_amount == Decimal("1140.00")


def test_yearly_profile_posts_only_annual_month_with_partial_year_amount():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(
            method="straight_line",
            posting_period="yearly",
            start_date=date(2026, 7, 1),
            annual_posting_month=12,
        )
    )

    november = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2026, 11, 1),
        period_end=date(2026, 12, 1),
        idempotency_key="correction-yearly-november",
    ).items.get(depreciation_profile=profile)
    december = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2026, 12, 1),
        period_end=date(2027, 1, 1),
        idempotency_key="correction-yearly-december",
    ).items.get(depreciation_profile=profile)

    assert november.status == "skipped"
    assert november.planned_amount == Decimal("0.00")
    assert december.status == "ready"
    assert december.planned_amount == Decimal("1149.37")


def test_yearly_uop_sums_all_natural_month_usages_in_posting_window():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(
            method="units_of_production",
            posting_period="yearly",
            annual_posting_month=12,
        )
    )
    for period_start, period_end in (
        (date(2024, 1, 1), date(2024, 2, 1)),
        (date(2024, 12, 1), date(2025, 1, 1)),
    ):
        record_work_usage(
            actor=actor,
            profile=profile,
            period_start=period_start,
            period_end=period_end,
            current_units=Decimal("10.000000"),
            work_unit="台时",
        )

    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 12, 1),
        period_end=date(2025, 1, 1),
        idempotency_key="correction-yearly-uop-window",
    )
    item = batch.items.get(depreciation_profile=profile)
    assert item.usage_units == Decimal("20.000000")
    assert item.calculated_unrounded == Decimal("2280.0000000000")
    assert item.planned_amount == Decimal("2280.00")
    assert len(item.calculation_snapshot_json["source_snapshot"]["usages"]) == 2

    confirm_depreciation_batch(actor=actor, batch=batch, reason="年度确认")
    assert DepreciationEntry.objects.get(batch_item=item).amount == Decimal("2280.00")


def test_yearly_uop_excludes_usage_before_actual_continuation_date():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(
            method="units_of_production",
            posting_period="yearly",
            annual_posting_month=12,
            actual_continuation_date=date(2024, 7, 1),
        )
    )
    _direct_work_usage(
        profile=profile,
        actor=actor,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        opening=Decimal("0.000000"),
        current=Decimal("10.000000"),
    )
    for period_start, period_end in (
        (date(2024, 7, 1), date(2024, 8, 1)),
        (date(2024, 12, 1), date(2025, 1, 1)),
    ):
        record_work_usage(
            actor=actor,
            profile=profile,
            period_start=period_start,
            period_end=period_end,
            current_units=Decimal("10.000000"),
            work_unit="台时",
        )

    item = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 12, 1),
        period_end=date(2025, 1, 1),
        idempotency_key="correction-yearly-uop-continuation-window",
    ).items.get(depreciation_profile=profile)

    assert item.usage_units == Decimal("20.000000")
    assert item.planned_amount == Decimal("2280.00")
    assert [
        usage["period_start"]
        for usage in item.calculation_snapshot_json["source_snapshot"]["usages"]
    ] == ["2024-07-01", "2024-12-01"]


def test_reversal_rejects_later_profile_built_from_confirmed_balance():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _profile_context()
    )
    effective_from = timezone.localdate().replace(day=1)
    period_start = (effective_from - timedelta(days=1)).replace(day=1)
    source = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=period_start,
        period_end=effective_from,
        idempotency_key="correction-profile-dependency-source",
    )
    confirm_depreciation_batch(actor=actor, batch=source, reason="月度确认")
    later = clone_asset_depreciation_profile(
        actor=actor,
        profile=profile,
        data={},
        effective_from=effective_from,
        reason="前瞻估计调整",
    )

    with pytest.raises(ValidationError, match="后续折旧 Profile"):
        reverse_depreciation_batch(
            actor=actor,
            batch=source,
            reason="发现前期错误",
            idempotency_key="correction-profile-dependency-reversal",
        )

    assert later.version == 2
    assert not source.reversal_batches.exists()


def test_controlled_non_fixed_import_rejects_depreciation_and_opening_fields(
    settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path / "media"
    company, actor, category, department, employee, location = sprint5_context(
        role="finance", prefix="CORRNF"
    )
    row = physical_row(company, category, department, employee, location)
    row.update(
        {
            "会计认定": "controlled_non_fixed",
            "会计认定说明": "财务明确认定为受控非固定资产",
            "原值": "1000.00",
            "折旧方法": "straight_line",
            "实际期初累计折旧": "100.00",
        }
    )

    batch = upload_and_validate_import(
        actor=actor,
        company=company,
        import_type="asset_initialization",
        uploaded_file=asset_workbook_upload(company, [row]),
        idempotency_key="correction-controlled-non-fixed",
    )
    error_fields = {item["field"] for item in batch.rows.get().errors_json}

    assert batch.status == "invalid"
    assert {"折旧方法", "实际期初累计折旧"} <= error_fields


def test_historical_import_pinpoints_missing_continuation_date(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    company, actor, category, department, employee, location = sprint5_context(
        role="finance", prefix="CORRHIST"
    )
    fixed_category, policy = finance_configuration(company, actor)
    row = physical_row(company, category, department, employee, location)
    add_finance_row(
        row,
        fixed_category=fixed_category,
        policy=policy,
        opening_ad="0.00",
        opening_impairment="0.00",
        opening_book="12000.00",
    )

    batch = upload_and_validate_import(
        actor=actor,
        company=company,
        import_type="asset_initialization",
        uploaded_file=asset_workbook_upload(company, [row]),
        idempotency_key="correction-historical-continuation",
    )

    assert batch.status == "invalid"
    assert "实际接续日" in {
        item["field"] for item in batch.rows.get().errors_json
    }


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PostgreSQL trigger boundary; SQLite is covered through the service test",
)
def test_postgresql_work_usage_trigger_rejects_out_of_order_direct_insert():
    _company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="units_of_production")
    )
    _direct_work_usage(
        profile=profile,
        actor=actor,
        period_start=date(2024, 3, 1),
        period_end=date(2024, 4, 1),
        opening=Decimal("0.000000"),
        current=Decimal("60.000000"),
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        _direct_work_usage(
            profile=profile,
            actor=actor,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 2, 1),
            opening=Decimal("0.000000"),
            current=Decimal("50.000000"),
        )


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PostgreSQL work-usage history trigger boundary",
)
def test_postgresql_work_usage_rejects_deleting_middle_of_chain():
    _company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="units_of_production")
    )
    first = _direct_work_usage(
        profile=profile,
        actor=actor,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        opening=Decimal("0.000000"),
        current=Decimal("10.000000"),
    )
    middle = _direct_work_usage(
        profile=profile,
        actor=actor,
        period_start=date(2024, 2, 1),
        period_end=date(2024, 3, 1),
        opening=Decimal("10.000000"),
        current=Decimal("20.000000"),
    )
    last = _direct_work_usage(
        profile=profile,
        actor=actor,
        period_start=date(2024, 3, 1),
        period_end=date(2024, 4, 1),
        opening=Decimal("30.000000"),
        current=Decimal("30.000000"),
    )

    with pytest.raises(
        IntegrityError,
        match="work usage with following history cannot be deleted or changed",
    ), transaction.atomic():
        AssetWorkUsage.objects.filter(pk=middle.pk).delete()

    with pytest.raises(
        IntegrityError,
        match="work usage with following history cannot be deleted or changed",
    ), transaction.atomic():
        AssetWorkUsage.objects.filter(pk=middle.pk).update(
            period_start=date(2024, 4, 1),
            period_end=date(2024, 5, 1),
            opening_accumulated_units=Decimal("60.000000"),
            current_units=Decimal("20.000000"),
            closing_accumulated_units=Decimal("80.000000"),
        )

    assert list(
        AssetWorkUsage.objects.filter(depreciation_profile=profile)
        .order_by("period_start")
        .values_list(
            "pk", "opening_accumulated_units", "closing_accumulated_units"
        )
    ) == [
        (first.pk, Decimal("0.000000"), Decimal("10.000000")),
        (middle.pk, Decimal("10.000000"), Decimal("30.000000")),
        (last.pk, Decimal("30.000000"), Decimal("60.000000")),
    ]


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PostgreSQL confirmed work-usage history boundary",
)
def test_postgresql_confirmed_work_usage_allows_only_entered_by_set_null():
    company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="units_of_production")
    )
    entered_by = get_user_model().objects.create_user(
        username="correction-work-usage-entered-by"
    )
    usage = _direct_work_usage(
        profile=profile,
        actor=entered_by,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        opening=Decimal("0.000000"),
        current=Decimal("10.000000"),
    )
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="correction-confirmed-usage-history",
    )
    confirm_depreciation_batch(
        actor=actor,
        batch=batch,
        reason="验证已确认工作量历史保护",
    )

    with pytest.raises(
        IntegrityError, match="work usage used by confirmed depreciation is immutable"
    ), transaction.atomic():
        AssetWorkUsage.objects.filter(pk=usage.pk).update(
            current_units=Decimal("11.000000"),
            closing_accumulated_units=Decimal("11.000000"),
        )

    entered_by.delete()
    usage.refresh_from_db()
    assert usage.entered_by_id is None
    assert usage.current_units == Decimal("10.000000")


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PostgreSQL work-usage Profile lock concurrency boundary",
)
def test_postgresql_delete_last_serializes_with_following_insert():
    _company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="units_of_production")
    )
    last = _direct_work_usage(
        profile=profile,
        actor=actor,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        opening=Decimal("0.000000"),
        current=Decimal("10.000000"),
    )
    delete_ready = Event()
    allow_delete_commit = Event()
    insert_started = Event()

    def delete_last():
        close_old_connections()
        try:
            with transaction.atomic():
                AssetWorkUsage.objects.filter(pk=last.pk).delete()
                delete_ready.set()
                if not allow_delete_commit.wait(5):
                    raise AssertionError("delete transaction was not released")
            return "deleted"
        finally:
            close_old_connections()

    def insert_following():
        close_old_connections()
        try:
            if not delete_ready.wait(5):
                raise AssertionError("delete transaction did not acquire its lock")
            insert_started.set()
            try:
                with transaction.atomic():
                    _direct_work_usage(
                        profile=profile,
                        actor=actor,
                        period_start=date(2024, 2, 1),
                        period_end=date(2024, 3, 1),
                        opening=Decimal("10.000000"),
                        current=Decimal("10.000000"),
                    )
            except IntegrityError as exc:
                return str(exc)
            return "inserted"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        delete_future = pool.submit(delete_last)
        assert delete_ready.wait(5)
        insert_future = pool.submit(insert_following)
        assert insert_started.wait(5)
        try:
            with pytest.raises(FutureTimeoutError):
                insert_future.result(timeout=1)
        finally:
            allow_delete_commit.set()
        assert delete_future.result(timeout=5) == "deleted"
        assert "cannot move backwards or disconnect" in insert_future.result(timeout=5)

    assert not AssetWorkUsage.objects.filter(depreciation_profile=profile).exists()
