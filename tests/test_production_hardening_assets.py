from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import (
    complete_disposal,
    initiate_disposal,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
    reverse_disposal,
    transfer_asset,
)
from apps.assets.models import (
    AssetDisposalReversal,
    AssetMovement,
    AttachmentLink,
)
from apps.masterdata.models import Department, Location
from apps.masterdata.services import update_department, update_location
from apps.offboarding.domain import business_date as offboarding_business_date
from apps.finance.models import (
    AssetDepreciationProfile,
    AssetFinance,
    DepreciationEntry,
)
from apps.finance.services import (
    confirm_asset_finance,
    confirm_depreciation_batch,
    create_fixed_asset_category,
    deactivate_fixed_asset_category,
    generate_depreciation_batch,
    save_asset_finance_draft,
)
from tests.test_sprint3_support import direct_attachment, make_location_tree
from tests.test_sprint4_acceptance import _base_context, _pending_asset
from tests.test_sprint7_support import (
    active_asset_context,
    active_fixed_asset_context,
    add_target_assignment,
)


pytestmark = pytest.mark.django_db


def _next_month_start(value):
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def test_offboarding_business_date_uses_shanghai_timezone_and_rejects_naive():
    assert offboarding_business_date(
        datetime(2026, 8, 31, 16, 30, tzinfo=ZoneInfo("UTC"))
    ) == date(2026, 9, 1)

    with pytest.raises(ValidationError, match="时区"):
        offboarding_business_date(datetime(2026, 8, 31, 16, 30))


def test_unconfirmed_finance_draft_is_not_exposed_as_confirmed_detail(client):
    context = _base_context("HARDFINNULL")
    asset = _pending_asset(context, "HARDFINNULL")
    AssetFinance.objects.create(company=context["company"], asset=asset)

    client.force_login(context["finance"])
    response = client.get(
        reverse("finance:asset-finance-detail", kwargs={"pk": asset.pk})
    )

    assert response.status_code == 404


def test_transfer_reloads_department_and_location_before_using_stale_targets():
    context, asset, _qr = active_asset_context("HARDSTALE")
    department, employee, location = add_target_assignment(
        context, "HARDSTALE"
    )
    stale_department = Department.objects.get(pk=department.pk)
    stale_location = Location.objects.get(pk=location.pk)

    update_department(
        actor=context["admin"],
        department=department,
        data={"is_active": False},
    )
    update_location(
        actor=context["admin"],
        location=location,
        data={"is_active": False},
    )

    with pytest.raises(ValidationError) as exc_info:
        transfer_asset(
            actor=context["equipment"],
            asset=asset,
            to_department=stale_department,
            to_responsible_employee=employee,
            to_location=stale_location,
            effective_at=timezone.now(),
            reason="陈旧页面调拨请求",
            idempotency_key="HARDSTALE-transfer",
        )

    assert "已停用" in str(exc_info.value)
    asset.refresh_from_db()
    assert asset.department_id == context["department"].pk
    assert asset.location_id == context["location"].pk
    assert not AssetMovement.objects.filter(
        asset=asset, idempotency_key="HARDSTALE-transfer"
    ).exists()


def test_finance_confirmation_reloads_stale_fixed_asset_category():
    context = _base_context("HARDFASTALE")
    asset = _pending_asset(context, "HARDFASTALE")
    category = create_fixed_asset_category(
        actor=context["finance"],
        company=context["company"],
        data={
            "code": "HARDFASTALE-FA",
            "name": "生产加固固定资产类别",
            "useful_life_months_default": 60,
        },
    )
    stale_category = type(category).objects.get(pk=category.pk)
    deactivate_fixed_asset_category(
        actor=context["finance"],
        category=category,
        reason="停用不再使用的类别",
    )

    with pytest.raises(ValidationError, match="已停用"):
        save_asset_finance_draft(
            actor=context["finance"],
            asset=asset,
            data={
                "accounting_treatment": "fixed_asset",
                "fixed_asset_category": stale_category,
                "original_cost": Decimal("10000.00"),
                "capitalization_date": timezone.localdate(),
            },
        )

    with pytest.raises(ValidationError, match="固定资产"):
        confirm_asset_finance(
            actor=context["finance"],
            asset=asset,
            finance_data={
                "accounting_treatment": "fixed_asset",
                "fixed_asset_category": stale_category,
                "original_cost": Decimal("10000.00"),
                "capitalization_date": timezone.localdate(),
            },
            code_effective_date=timezone.localdate(),
            idempotency_key="HARDFASTALE-confirm",
            reason="陈旧页面财务确认请求",
        )

    asset.refresh_from_db()
    assert asset.asset_status == "pending_finance"
    assert not AssetDepreciationProfile.objects.filter(asset=asset).exists()


def test_completed_disposal_stop_prevents_future_depreciation_batches():
    context, asset, _qr, profile, _policy = active_fixed_asset_context(
        "HARDSTOP", stop_rule="next_month"
    )
    today = timezone.localdate()
    current_month = today.replace(day=1)
    next_month = _next_month_start(today)

    disposal = initiate_disposal(
        actor=context["equipment"],
        asset=asset,
        disposal_type="scrap",
        application_date=today,
        planned_disposal_date=today,
        reason="生产加固回归测试处置",
        idempotency_key="HARDSTOP-disposal-start",
        expected_status="in_use",
    )
    disposal = record_disposal_actual_details(
        actor=context["equipment"],
        disposal=disposal,
        actual_disposal_date=today,
        handled_by=context["equipment"],
        idempotency_key="HARDSTOP-disposal-actual",
    )

    current_batch = generate_depreciation_batch(
        actor=context["finance"],
        company=context["company"],
        period_start=current_month,
        period_end=next_month,
        idempotency_key="HARDSTOP-current-batch",
    )
    confirm_depreciation_batch(
        actor=context["finance"],
        batch=current_batch,
        reason="确认处置月应计折旧",
    )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"],
        disposal=disposal,
        disposal_income=Decimal("0.00"),
        idempotency_key="HARDSTOP-disposal-lock",
    )
    attachment = direct_attachment(
        context["company"],
        context["equipment"],
        key="private/disposals/HARDSTOP.jpg",
        filename="HARDSTOP.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"],
        attachment=attachment,
        asset_disposal=disposal,
        role="disposal",
        security_class="A0",
        created_by=context["equipment"],
    )
    complete_disposal(
        actor=context["equipment"],
        disposal=disposal,
        idempotency_key="HARDSTOP-disposal-complete",
    )

    following_month = _next_month_start(next_month)
    future_batch = generate_depreciation_batch(
        actor=context["finance"],
        company=context["company"],
        period_start=next_month,
        period_end=following_month,
        idempotency_key="HARDSTOP-future-batch",
    )
    future_item = future_batch.items.get(
        asset=asset, depreciation_profile=profile
    )

    assert future_item.status == "skipped"
    assert future_item.planned_amount == Decimal("0.00")
    assert (
        future_item.calculation_snapshot_json["skip_reason"]
        == "折旧事件/寿命规则下当期无资格"
    )

    confirm_depreciation_batch(
        actor=context["finance"],
        batch=future_batch,
        reason="验证处置停止后无后续折旧分录",
    )
    assert not DepreciationEntry.objects.filter(
        asset=asset,
        period_start=next_month,
        period_end=following_month,
    ).exists()


def test_midmonth_event_date_disposal_fails_closed_before_snapshot_lock():
    context, asset, _qr, _profile, _policy = active_fixed_asset_context(
        "HARDMIDSTOP", stop_rule="event_date"
    )
    actual_date = timezone.localdate()
    if actual_date.day == 1:
        pytest.skip("月中 event_date 处置回归需要非月初业务日。")
    disposal = initiate_disposal(
        actor=context["equipment"],
        asset=asset,
        disposal_type="scrap",
        application_date=actual_date,
        planned_disposal_date=actual_date,
        reason="月中按事件日停止测试",
        idempotency_key="HARDMIDSTOP-start",
        expected_status="in_use",
    )
    disposal = record_disposal_actual_details(
        actor=context["equipment"],
        disposal=disposal,
        actual_disposal_date=actual_date,
        handled_by=context["equipment"],
        idempotency_key="HARDMIDSTOP-actual",
    )

    with pytest.raises(ValidationError, match="部分月折旧"):
        lock_disposal_financial_snapshot(
            actor=context["finance"],
            disposal=disposal,
            disposal_income=Decimal("0.00"),
            idempotency_key="HARDMIDSTOP-lock",
        )

    disposal.refresh_from_db()
    assert disposal.status == "draft"
    assert disposal.original_cost_snapshot is None
    assert disposal.book_value_snapshot is None


def test_yearly_profile_disposal_fails_closed_before_snapshot_lock():
    context, asset, _qr, _profile, _policy = active_fixed_asset_context(
        "HARDYEARSTOP",
        stop_rule="next_month",
        posting_period="yearly",
        annual_posting_month=timezone.localdate().month,
    )
    today = timezone.localdate()
    disposal = initiate_disposal(
        actor=context["equipment"],
        asset=asset,
        disposal_type="scrap",
        application_date=today,
        planned_disposal_date=today,
        reason="年度折旧处置测试",
        idempotency_key="HARDYEARSTOP-start",
        expected_status="in_use",
    )
    disposal = record_disposal_actual_details(
        actor=context["equipment"],
        disposal=disposal,
        actual_disposal_date=today,
        handled_by=context["equipment"],
        idempotency_key="HARDYEARSTOP-actual",
    )

    with pytest.raises(ValidationError, match="年度折旧 Profile"):
        lock_disposal_financial_snapshot(
            actor=context["finance"],
            disposal=disposal,
            disposal_income=Decimal("0.00"),
            idempotency_key="HARDYEARSTOP-lock",
        )

    disposal.refresh_from_db()
    assert disposal.status == "draft"
    assert disposal.original_cost_snapshot is None
    assert disposal.book_value_snapshot is None


def test_no_depreciation_profile_keeps_safe_disposal_snapshot_path():
    context, asset, _qr, _profile, _policy = active_fixed_asset_context(
        "HARDNODEP",
        stop_rule="event_date",
        method="no_depreciation",
        posting_period="yearly",
        annual_posting_month=timezone.localdate().month,
    )
    actual_date = timezone.localdate()
    disposal = initiate_disposal(
        actor=context["equipment"],
        asset=asset,
        disposal_type="scrap",
        application_date=actual_date,
        planned_disposal_date=actual_date,
        reason="不计提折旧安全处置测试",
        idempotency_key="HARDNODEP-start",
        expected_status="in_use",
    )
    disposal = record_disposal_actual_details(
        actor=context["equipment"],
        disposal=disposal,
        actual_disposal_date=actual_date,
        handled_by=context["equipment"],
        idempotency_key="HARDNODEP-actual",
    )
    locked = lock_disposal_financial_snapshot(
        actor=context["finance"],
        disposal=disposal,
        disposal_income=Decimal("0.00"),
        idempotency_key="HARDNODEP-lock",
    )

    assert locked.status == "finance_locked"
    assert locked.actual_accumulated_depreciation_snapshot == Decimal("0.00")
    assert locked.book_value_snapshot == locked.original_cost_snapshot


def test_disposal_reversal_rejects_inactive_original_location():
    context, asset, _qr = active_asset_context("HARDREVLOC")
    today = timezone.localdate()
    disposal = initiate_disposal(
        actor=context["equipment"],
        asset=asset,
        disposal_type="scrap",
        application_date=today,
        planned_disposal_date=today,
        reason="生产加固冲销边界测试",
        idempotency_key="HARDREVLOC-start",
        expected_status="in_use",
    )
    disposal = record_disposal_actual_details(
        actor=context["equipment"],
        disposal=disposal,
        actual_disposal_date=today,
        handled_by=context["equipment"],
        idempotency_key="HARDREVLOC-actual",
    )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"],
        disposal=disposal,
        disposal_income=Decimal("0.00"),
        idempotency_key="HARDREVLOC-lock",
    )
    attachment = direct_attachment(
        context["company"],
        context["equipment"],
        key="private/disposals/HARDREVLOC.jpg",
        filename="HARDREVLOC.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"],
        attachment=attachment,
        asset_disposal=disposal,
        role="disposal",
        security_class="A0",
        created_by=context["equipment"],
    )
    complete_disposal(
        actor=context["equipment"],
        disposal=disposal,
        idempotency_key="HARDREVLOC-complete",
    )
    make_location_tree(context["company"], "HARDREVLOC-BACKUP")
    update_location(
        actor=context["admin"],
        location=asset.location,
        data={"is_active": False},
    )

    with pytest.raises(ValidationError, match="原位置"):
        reverse_disposal(
            actor=context["finance"],
            disposal=disposal,
            reason="尝试恢复到已停用位置",
            idempotency_key="HARDREVLOC-reverse",
        )

    asset.refresh_from_db()
    disposal.refresh_from_db()
    assert asset.asset_status == "disposed"
    assert disposal.status == "confirmed"
    assert not AssetDisposalReversal.objects.filter(
        asset_disposal=disposal
    ).exists()
