from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.models import Asset
from apps.assets.services import submit_asset_for_finance
from apps.finance.models import (
    AssetDepreciationProfile,
    AssetFinance,
    AssetValueAdjustment,
    DepreciationBatch,
    DepreciationBatchItem,
    DepreciationEntry,
    DepreciationPolicy,
)
from apps.finance.permissions import can_manage_finance, can_view_finance
from apps.finance.services import (
    DEPRECIATION_ENGINE_VERSION,
    _balances_before,
    _event_eligibility,
    _models,
    _next_unconfirmed_profile_month,
    _unimpaired_book_value_ceiling,
    clone_asset_depreciation_profile,
    confirm_depreciation_batch,
    create_fixed_asset_category,
    create_profile_event,
    create_value_adjustment,
    deactivate_fixed_asset_category,
    generate_depreciation_batch,
    reverse_depreciation_batch,
    reverse_value_adjustment,
)
from apps.masterdata.models import FixedAssetCategory
from tests.test_sprint3_support import (
    add_photo,
    complete_initialization,
    make_asset,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
)


pytestmark = pytest.mark.django_db


def _users_and_company():
    company = make_company()
    finance = make_user("finance-s4", "finance")
    management = make_user("management-s4", "management")
    admin = make_user("admin-s4", "system_admin")
    complete_initialization(company, finance)
    return company, finance, management, admin


def _asset(company, actor, *, suffix="1"):
    category = make_category(company, f"EQ{suffix}")
    department = make_department(company, f"D{suffix}")
    employee = make_employee(company, department, f"E{suffix}")
    _site, _area, location = make_location_tree(company, f"L{suffix}")
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
        commissioning_date=date(2024, 1, 1),
        asset_name=f"折旧测试资产 {suffix}",
    )
    add_photo(actor, asset)
    return submit_asset_for_finance(actor=actor, asset=asset)


def _policy(company, actor, *, method="straight_line", life=60):
    return DepreciationPolicy.objects.create(
        company=company,
        policy_key=f"P-{method}-{life}",
        version=1,
        name="服务测试政策",
        method=method,
        posting_period="monthly",
        start_rule="current_month",
        stop_rule="event_date",
        default_useful_life_months=life,
        default_salvage_mode="rate",
        default_salvage_rate=Decimal("0.05"),
        status="active",
        is_default=True,
        effective_from=date(2024, 1, 1),
        created_by=actor,
    )


def _profile_context(*, method="straight_line", life=60, cost="12000.00"):
    company, finance_user, management, admin = _users_and_company()
    asset = _asset(company, finance_user)
    fixed = FixedAssetCategory.objects.create(
        company=company,
        code="FA",
        normalized_code="fa",
        name="设备",
        useful_life_months_default=life,
    )
    finance = AssetFinance.objects.create(
        company=company,
        asset=asset,
        accounting_treatment="fixed_asset",
        recognition_threshold_snapshot=Decimal("5000.00"),
        fixed_asset_category=fixed,
        original_cost=Decimal(cost),
        capitalization_date=date(2024, 1, 1),
        impairment_balance_cache=Decimal("0.00"),
        finance_confirmed_by=finance_user,
        finance_confirmed_at=timezone.now(),
    )
    policy = _policy(company, finance_user, method=method, life=life)
    profile = AssetDepreciationProfile.objects.create(
        company=company,
        asset=asset,
        depreciation_policy=policy,
        version=1,
        method=method,
        posting_period="monthly",
        start_rule="current_month",
        stop_rule="event_date",
        start_date=date(2024, 1, 1),
        useful_life_months=life,
        salvage_mode="rate",
        salvage_rate=Decimal("0.05"),
        opening_book_value=Decimal(cost),
        opening_actual_accumulated_depreciation=Decimal("0.00"),
        effective_from=date(2024, 1, 1),
        status="active",
        created_by=finance_user,
    )
    return company, finance_user, management, admin, asset, finance, profile


def _confirmed_entry(*, profile, start, amount, actor, status="confirmed"):
    batch = DepreciationBatch.objects.create(
        company=profile.company,
        period_start=start,
        period_end=(start.replace(day=28) + timedelta(days=4)).replace(day=1),
        generation_no=1,
        batch_type="regular",
        status="draft",
        idempotency_key=f"batch-{start.isoformat()}-{profile.asset_id}",
        request_hash="a" * 64,
        generated_by=actor,
        generated_at=timezone.now(),
        confirmed_by=None,
        confirmed_at=None,
    )
    item = DepreciationBatchItem.objects.create(
        company=profile.company,
        batch=batch,
        asset=profile.asset,
        depreciation_profile=profile,
        calculation_method=profile.method,
        opening_book_value=Decimal("12000.00"),
        depreciable_floor=Decimal("600.00"),
        eligible_fraction=Decimal("1"),
        calculated_unrounded=amount,
        planned_amount=amount,
        closing_book_value=Decimal("12000.00") - amount,
        calculation_snapshot_json={"engine_version": DEPRECIATION_ENGINE_VERSION},
        status="ready",
    )
    entry = DepreciationEntry.objects.create(
        company=profile.company,
        asset=profile.asset,
        depreciation_profile=profile,
        entry_date=batch.period_end,
        period_start=batch.period_start,
        period_end=batch.period_end,
        source_type="batch",
        batch_item=item,
        amount=amount,
        accumulated_depreciation_after=amount,
        book_value_after=Decimal("12000.00") - amount,
        posted_by=actor,
        posted_at=timezone.now(),
    )
    if status == "confirmed":
        batch.status = "confirmed"
        batch.confirmed_by = actor
        batch.confirmed_at = timezone.now()
        batch.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    return batch, item, entry


def test_finance_permission_matrix_denies_system_admin_write():
    _company, finance, management, admin = _users_and_company()
    assert can_view_finance(finance)
    assert can_manage_finance(finance)
    assert can_view_finance(management)
    assert not can_manage_finance(management)
    assert not can_view_finance(admin)
    assert not can_manage_finance(admin)


def test_fixed_asset_category_is_finance_only_and_deactivation_requires_reason():
    company, finance, management, _admin = _users_and_company()
    with pytest.raises(PermissionDenied):
        create_fixed_asset_category(
            actor=management,
            company=company,
            data={"code": "M", "name": "模具", "useful_life_months_default": 36},
        )
    category = create_fixed_asset_category(
        actor=finance,
        company=company,
        data={"code": "M", "name": "模具", "useful_life_months_default": 36},
    )
    with pytest.raises(ValidationError):
        deactivate_fixed_asset_category(actor=finance, category=category)
    deactivate_fixed_asset_category(
        actor=finance, category=category, reason="会计分类停止新用"
    )
    category.refresh_from_db()
    assert category.is_active is False


def test_business_cutoff_balance_excludes_future_entry_and_adjustment():
    _company, actor, _management, _admin, asset, finance, profile = _profile_context()
    _confirmed_entry(
        profile=profile, start=date(2024, 2, 1), amount=Decimal("190.00"), actor=actor
    )
    AssetValueAdjustment.objects.create(
        company=asset.company,
        asset=asset,
        adjustment_type="impairment",
        status="confirmed",
        effective_date=date(2024, 3, 1),
        amount=Decimal("1000.00"),
        old_values_json={},
        new_values_json={},
        reason="未来期间减值",
        confirmed_by=actor,
        confirmed_at=timezone.now(),
        created_by=actor,
    )
    cost, impairment, accumulated, book = _balances_before(
        asset, date(2024, 2, 1), finance=finance
    )
    assert cost == Decimal("12000.00")
    assert impairment == Decimal("0.00")
    assert accumulated == Decimal("0.00")
    assert book == Decimal("12000.00")


def test_event_half_open_interval_and_open_suspend_extend_life_input():
    _company, _actor, _management, _admin, _asset, _finance, profile = _profile_context()
    events = [
        {"id": "1", "event_type": "suspend", "effective_date": "2024-01-16"},
        {"id": "2", "event_type": "resume", "effective_date": "2024-02-01"},
    ]
    suspensions, stop = _event_eligibility(
        profile, events, through_date=date(2024, 3, 1)
    )
    assert stop is None
    assert [(item.start, item.end) for item in suspensions] == [
        (date(2024, 1, 16), date(2024, 2, 1))
    ]


def test_profile_event_rejects_date_with_confirmed_regular_period():
    _company, actor, _management, _admin, _asset, _finance, profile = _profile_context()
    _confirmed_entry(
        profile=profile, start=date(2024, 2, 1), amount=Decimal("190.00"), actor=actor
    )
    with pytest.raises(ValidationError, match="确认折旧批次"):
        create_profile_event(
            actor=actor,
            profile=profile,
            event_type="suspend",
            effective_date=date(2024, 2, 15),
            reason="设备停产",
        )


def test_adjustment_rejects_future_business_date():
    _company, actor, _management, _admin, asset, _finance, _profile = _profile_context()
    with pytest.raises(ValidationError, match="未来业务日期"):
        create_value_adjustment(
            actor=actor,
            asset=asset,
            adjustment_type="depreciation_adjustment",
            amount="100.00",
            effective_date=timezone.localdate() + timedelta(days=1),
            reason="尚未发生",
        )


def test_clone_blocks_any_confirmed_period_at_or_after_effective_date():
    _company, actor, _management, _admin, _asset, _finance, profile = _profile_context()
    month = timezone.localdate().replace(day=1)
    if month <= profile.effective_from:
        pytest.skip("fixture date does not permit a later profile version")
    later = _confirmed_entry(
        profile=profile, start=month, amount=Decimal("190.00"), actor=actor
    )[0]
    with pytest.raises(ValidationError):
        clone_asset_depreciation_profile(
            actor=actor,
            profile=profile,
            data={},
            effective_from=month,
            reason="剩余寿命调整",
        )
    assert DepreciationBatch.objects.filter(pk=later.pk, status="confirmed").exists()


def test_reverse_rejects_middle_month_when_later_confirmed_exists():
    _company, actor, _management, _admin, _asset, _finance, profile = _profile_context()
    first, _item, _entry = _confirmed_entry(
        profile=profile, start=date(2024, 1, 1), amount=Decimal("190.00"), actor=actor
    )
    _confirmed_entry(
        profile=profile, start=date(2024, 2, 1), amount=Decimal("190.00"), actor=actor
    )
    with pytest.raises(ValidationError, match="后续已确认月份"):
        reverse_depreciation_batch(
            actor=actor,
            batch=first,
            reason="发现错误",
            idempotency_key="reverse-middle",
        )


def test_batch_snapshot_records_engine_version():
    company, actor, _management, _admin, _asset, _finance, _profile = _profile_context()
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="generate-engine-version",
    )
    item = batch.items.get()
    assert item.calculation_snapshot_json["engine_version"] == DEPRECIATION_ENGINE_VERSION
    assert item.calculation_snapshot_json["source_snapshot"]["engine_version"] == DEPRECIATION_ENGINE_VERSION


@pytest.mark.parametrize(
    ("method", "expected"),
    (
        ("double_declining_balance", Decimal("400.00")),
        ("sum_of_years_digits", Decimal("316.67")),
    ),
)
def test_batch_recalculation_keeps_full_life_context(method, expected):
    company, actor, _management, _admin, _asset, _finance, _profile = (
        _profile_context(method=method)
    )
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key=f"full-life-{method}",
    )
    assert batch.items.get().planned_amount == expected


@pytest.mark.django_db(transaction=True)
def test_midmonth_suspend_and_resume_recalculates_eligible_fraction():
    company, actor, _management, _admin, _asset, _finance, profile = _profile_context()
    create_profile_event(
        actor=actor,
        profile=profile,
        event_type="suspend",
        effective_date=date(2024, 1, 16),
        reason="月中停机",
    )
    create_profile_event(
        actor=actor,
        profile=profile,
        event_type="resume",
        effective_date=date(2024, 2, 1),
        reason="次月恢复",
    )
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="midmonth-event",
    )
    item = batch.items.get()
    assert item.eligible_fraction == (Decimal(15) / Decimal(31)).quantize(
        Decimal("0.0000000001")
    )
    assert item.planned_amount == Decimal("91.94")
    assert len(item.calculation_snapshot_json["source_snapshot"]["events"]) == 2


def test_confirm_rejects_event_added_after_batch_generation():
    company, actor, _management, _admin, _asset, _finance, profile = _profile_context()
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="event-snapshot-before",
    )
    create_profile_event(
        actor=actor,
        profile=profile,
        event_type="suspend",
        effective_date=date(2024, 1, 16),
        reason="生成后发生停机",
    )
    with pytest.raises(ValidationError, match="重新生成"):
        confirm_depreciation_batch(
            actor=actor,
            batch=batch,
            reason="尝试确认过期快照",
        )
    batch.refresh_from_db()
    assert batch.status == "draft"
    assert not DepreciationEntry.objects.filter(batch_item__batch=batch).exists()


def test_confirm_batch_requires_reason_before_mutation():
    company, actor, _management, _admin, _asset, _finance, _profile = _profile_context()
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 2, 1),
        idempotency_key="confirm-reason",
    )
    with pytest.raises(ValidationError, match="必须填写原因"):
        confirm_depreciation_batch(actor=actor, batch=batch)
    batch.refresh_from_db()
    assert batch.status == "draft"


@pytest.mark.parametrize(
    ("adjustment_type", "amount", "expected_cost", "expected_impairment", "entry_amount"),
    (
        ("impairment", "100.00", "12000.00", "100.00", None),
        ("cost_correction", "500.00", "12500.00", "0.00", None),
        ("depreciation_adjustment", "75.00", "12000.00", "0.00", "75.00"),
    ),
)
def test_value_adjustment_updates_ledger_and_creates_remaining_life_profile(
    adjustment_type, amount, expected_cost, expected_impairment, entry_amount
):
    _company, actor, _management, _admin, asset, finance, old_profile = _profile_context()
    effective = timezone.localdate().replace(day=1)
    adjustment = create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type=adjustment_type,
        amount=amount,
        effective_date=effective,
        reason=f"测试 {adjustment_type}",
    )
    finance.refresh_from_db()
    old_profile.refresh_from_db()
    new_profile = asset.depreciation_profiles.get(version=2)
    assert adjustment.status == "confirmed"
    assert finance.original_cost == Decimal(expected_cost)
    assert finance.impairment_balance_cache == Decimal(expected_impairment)
    assert old_profile.status == "completed"
    assert old_profile.effective_to == new_profile.effective_from - timedelta(days=1)
    assert new_profile.status == "active"
    assert new_profile.useful_life_months < old_profile.useful_life_months
    if entry_amount is None:
        assert not DepreciationEntry.objects.filter(value_adjustment=adjustment).exists()
    else:
        entry = DepreciationEntry.objects.get(value_adjustment=adjustment)
        assert entry.amount == Decimal(entry_amount)
        assert entry.period_end == entry.period_start + timedelta(days=1)


def test_impairment_reversal_updates_cache_and_creates_next_profile_version():
    # Keep this success-path fixture free of an intentionally missing legacy
    # depreciation history.  The separate ceiling tests below exercise a
    # straight-line asset with posted accumulated depreciation.
    _company, actor, _management, _admin, asset, finance, _profile = (
        _profile_context(method="no_depreciation")
    )
    effective = timezone.localdate().replace(day=1)
    create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type="impairment",
        amount="100.00",
        effective_date=effective,
        reason="确认减值",
    )
    active = asset.depreciation_profiles.get(status="active")
    adjustment = create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type="impairment_reversal",
        amount="40.00",
        effective_date=_next_unconfirmed_profile_month(
            asset=asset, profile=active
        ),
        reason="减值因素部分消失",
    )
    finance.refresh_from_db()
    assert adjustment.status == "confirmed"
    assert finance.impairment_balance_cache == Decimal("60.00")
    assert asset.depreciation_profiles.count() == 3
    assert asset.depreciation_profiles.get(version=3).status == "active"


def test_impairment_reversal_rejects_above_unimpaired_ceiling_and_accepts_exact_ceiling():
    company, actor, _management, _admin, asset, finance, profile = _profile_context()
    boundary = timezone.localdate().replace(day=1)
    # Bring the legacy ledger to the no-impairment schedule immediately before
    # the prospective month, then recognize impairment and one lower-
    # depreciation month.  This makes the counterfactual ceiling strictly
    # lower than C - actual AD and exercises the real service path.
    elapsed_months = (boundary.year - 2024) * 12 + boundary.month - 1
    opening_ad = Decimal("190.00") * elapsed_months
    DepreciationEntry.objects.create(
        company=asset.company,
        asset=asset,
        depreciation_profile=profile,
        entry_date=boundary,
        period_start=boundary,
        period_end=boundary + timedelta(days=1),
        source_type="opening",
        opening_profile=profile,
        amount=opening_ad,
        accumulated_depreciation_after=opening_ad,
        book_value_after=Decimal("12000.00") - opening_ad,
        posted_by=actor,
        posted_at=timezone.now(),
    )
    original = create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type="impairment",
        amount="1000.00",
        effective_date=boundary,
        reason="确认较大减值",
    )
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=boundary,
        period_end=_next_unconfirmed_profile_month(
            asset=asset, profile=asset.depreciation_profiles.get(status="active")
        ),
        idempotency_key="reversal-ceiling-period",
    )
    confirm_depreciation_batch(
        actor=actor, batch=batch, reason="形成减值后的实际折旧"
    )
    active = asset.depreciation_profiles.get(status="active")
    next_boundary = _next_unconfirmed_profile_month(asset=asset, profile=active)
    cost, impairment, accumulated, book = _balances_before(
        asset, next_boundary, finance=finance
    )
    ceiling = _unimpaired_book_value_ceiling(
        asset=asset, effective_date=next_boundary, finance=finance
    )
    exact_reversal = (ceiling - book).quantize(Decimal("0.01"))
    assert Decimal("0.00") < exact_reversal < impairment
    before_count = AssetValueAdjustment.objects.filter(asset=asset).count()
    with pytest.raises(ValidationError, match="假设从未发生"):
        create_value_adjustment(
            actor=actor,
            asset=asset,
            adjustment_type="impairment_reversal",
            amount=exact_reversal + Decimal("0.01"),
            effective_date=next_boundary,
            reason="超过反事实上限",
        )
    finance.refresh_from_db()
    assert finance.impairment_balance_cache == Decimal("1000.00")
    assert AssetValueAdjustment.objects.filter(asset=asset).count() == before_count
    accepted = create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type="impairment_reversal",
        amount=exact_reversal,
        effective_date=next_boundary,
        reason="恰好恢复至反事实上限",
    )
    finance.refresh_from_db()
    assert accepted.status == "confirmed"
    assert finance.impairment_balance_cache == Decimal("1000.00") - exact_reversal


def test_reverse_original_impairment_cannot_bypass_unimpaired_ceiling():
    company, actor, _management, _admin, asset, finance, profile = _profile_context()
    boundary = timezone.localdate().replace(day=1)
    elapsed_months = (boundary.year - 2024) * 12 + boundary.month - 1
    opening_ad = Decimal("190.00") * elapsed_months
    DepreciationEntry.objects.create(
        company=asset.company,
        asset=asset,
        depreciation_profile=profile,
        entry_date=boundary,
        period_start=boundary,
        period_end=boundary + timedelta(days=1),
        source_type="opening",
        opening_profile=profile,
        amount=opening_ad,
        accumulated_depreciation_after=opening_ad,
        book_value_after=Decimal("12000.00") - opening_ad,
        posted_by=actor,
        posted_at=timezone.now(),
    )
    original = create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type="impairment",
        amount="1000.00",
        effective_date=boundary,
        reason="确认较大减值",
    )
    active = asset.depreciation_profiles.get(status="active")
    next_boundary = _next_unconfirmed_profile_month(asset=asset, profile=active)
    batch = generate_depreciation_batch(
        actor=actor,
        company=company,
        period_start=boundary,
        period_end=next_boundary,
        idempotency_key="reverse-source-ceiling-period",
    )
    confirm_depreciation_batch(
        actor=actor, batch=batch, reason="形成减值后的实际折旧"
    )
    before_profiles = asset.depreciation_profiles.count()
    with pytest.raises(ValidationError, match="假设从未发生"):
        reverse_value_adjustment(
            actor=actor,
            adjustment=original,
            reason="不得用冲销绕过反事实上限",
        )
    original.refresh_from_db()
    finance.refresh_from_db()
    assert original.status == "confirmed"
    assert finance.impairment_balance_cache == Decimal("1000.00")
    assert asset.depreciation_profiles.count() == before_profiles
    assert not AssetValueAdjustment.objects.filter(reversal_of=original).exists()


def test_historical_adjustment_month_is_rejected_without_side_effects():
    _company, actor, _management, _admin, asset, finance, profile = _profile_context()
    historical = timezone.localdate().replace(day=1) - timedelta(days=1)
    historical = historical.replace(day=1)
    before_profiles = asset.depreciation_profiles.count()
    with pytest.raises(ValidationError, match="下一未确认自然月首"):
        create_value_adjustment(
            actor=actor,
            asset=asset,
            adjustment_type="impairment",
            amount="100.00",
            effective_date=historical,
            reason="不允许追溯造成计划空档",
        )
    finance.refresh_from_db()
    profile.refresh_from_db()
    assert finance.impairment_balance_cache == Decimal("0.00")
    assert profile.status == "active"
    assert asset.depreciation_profiles.count() == before_profiles
    assert not AssetValueAdjustment.objects.filter(asset=asset).exists()


def test_value_adjustment_failure_rolls_back_fact_balance_and_profile(monkeypatch):
    _company, actor, _management, _admin, asset, finance, profile = _profile_context()

    def fail_clone(**_kwargs):
        raise RuntimeError("forced profile failure")

    monkeypatch.setattr(
        "apps.finance.services.clone_asset_depreciation_profile", fail_clone
    )
    with pytest.raises(RuntimeError, match="forced profile failure"):
        create_value_adjustment(
            actor=actor,
            asset=asset,
            adjustment_type="impairment",
            amount="100.00",
            effective_date=timezone.localdate().replace(day=1),
            reason="验证原子回滚",
        )
    finance.refresh_from_db()
    profile.refresh_from_db()
    assert finance.impairment_balance_cache == Decimal("0.00")
    assert profile.status == "active"
    assert not AssetValueAdjustment.objects.filter(asset=asset).exists()


def test_reverse_value_adjustment_appends_opposite_and_reprofiles():
    # This test isolates the append-only reversal/version chain.  A separate
    # straight-line test below proves that reversal cannot bypass the
    # counterfactual no-impairment carrying-amount ceiling.
    _company, actor, _management, _admin, asset, finance, _profile = (
        _profile_context(method="no_depreciation")
    )
    effective = timezone.localdate().replace(day=1)
    original = create_value_adjustment(
        actor=actor,
        asset=asset,
        adjustment_type="impairment",
        amount="100.00",
        effective_date=effective,
        reason="确认减值",
    )
    reversal = reverse_value_adjustment(
        actor=actor, adjustment=original, reason="发现估计依据错误"
    )
    original.refresh_from_db()
    finance.refresh_from_db()
    assert original.status == "reversed"
    assert reversal.reversal_of_id == original.pk
    assert reversal.adjustment_type == "impairment_reversal"
    assert reversal.amount == Decimal("100.00")
    assert finance.impairment_balance_cache == Decimal("0.00")
    assert asset.depreciation_profiles.count() == 3


def test_reverse_confirmed_batch_creates_exact_append_only_pair():
    _company, actor, _management, _admin, _asset, _finance, profile = _profile_context()
    source, _item, original = _confirmed_entry(
        profile=profile,
        start=date(2024, 1, 1),
        amount=Decimal("190.00"),
        actor=actor,
    )
    reversal = reverse_depreciation_batch(
        actor=actor,
        batch=source,
        reason="原批次参数错误",
        idempotency_key="reverse-success",
    )
    source.refresh_from_db()
    reversed_entry = reversal.items.get().entries.get()
    assert source.status == "reversed"
    assert reversal.status == "confirmed"
    assert reversed_entry.amount == -original.amount
    assert reversed_entry.reversal_of_id == original.pk
