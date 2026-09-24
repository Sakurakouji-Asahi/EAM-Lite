"""Historical opening balances, subsequent posting, and displayed progress."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset
from apps.assets.services import submit_asset_for_finance
from apps.finance.domain import (
    DepreciationError, ScheduleInput, depreciation_position, generate_schedule,
)
from apps.finance.models import AssetDepreciationProfile, DepreciationEntry
from apps.finance.services import (
    _create_opening_effects, _profile_spec, confirm_asset_finance, confirm_depreciation_batch,
    generate_depreciation_batch, get_asset_depreciation_status,
    record_work_usage, review_profile_actual_continuation_date, reverse_depreciation_batch,
)
from apps.imports.services import confirm_import_batch, upload_and_validate_import
from apps.masterdata.services import set_system_setting
from tests.test_correction_finance_services import _custom_profile_context
from tests.test_sprint3_support import (
    add_photo, make_structurally_valid_active_scheme, make_user,
)
from tests.test_sprint5_support import (
    add_finance_row, asset_workbook_upload, finance_configuration,
    physical_row, sprint5_context,
)


@pytest.mark.parametrize("method", [
    "straight_line", "double_declining_balance", "sum_of_years_digits",
    "manual", "units_of_production", "no_depreciation",
])
@pytest.mark.parametrize("cost,accumulated,residual", [
    ("2880.00", "2736.00", "144.00"),
    ("20160.00", "20160.00", "0.00"),
])
def test_historical_settled_balance_has_no_future_depreciation(method, cost, accumulated, residual):
    # Amounts are from source rows 95 and 7; the 60-month life is a test input.
    spec = ScheduleInput(
        original_cost=cost, method=method, posting_period="monthly",
        commissioning_date=date(2005, 7, 1), start_rule="current_month",
        useful_life_months=60, salvage_mode="amount", salvage_amount=residual,
        opening_actual_accumulated_depreciation=accumulated,
        opening_book_value=residual, actual_continuation_date=date(2026, 9, 1),
        expected_total_units="100" if method == "units_of_production" else None,
        work_unit="台时" if method == "units_of_production" else None,
    )
    result = generate_schedule(spec)
    assert result.start_date == date(2005, 7, 1)
    assert result.natural_end_date == date(2010, 7, 1)
    assert result.actual_continuation_date == date(2026, 9, 1)
    assert result.opening_book_value == Decimal(residual)
    assert result.depreciable_amount == result.planned_total == Decimal("0.00")
    assert result.lines == ()
    with pytest.raises(DepreciationError, match="不得晚于原预计寿命终点"):
        generate_schedule(replace(
            spec, opening_actual_accumulated_depreciation=Decimal(accumulated) - 1,
            opening_book_value=Decimal(residual) + 1,
        ))
    with pytest.raises(DepreciationError, match="不得早于原折旧起算日"):
        generate_schedule(replace(spec, actual_continuation_date=date(2004, 1, 1)))


@pytest.mark.parametrize("accumulated,impairment,state,remaining", [
    ("950", "0", "fully_depreciated", "0.00"),
    ("800", "150", "no_depreciable_balance", "0.00"),
    ("800", "0", "not_fully_depreciated", "150.00"),
])
def test_progress_separates_depreciation_from_impairment(accumulated, impairment, state, remaining):
    position = depreciation_position(
        original_cost="1000", accumulated_depreciation=accumulated,
        impairment_balance=impairment, salvage_value="50",
    )
    assert position.status == state
    assert position.remaining_amount == Decimal(remaining)


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["manual", "units_of_production"])
def test_input_based_methods_accept_late_settled_opening_without_period_inputs(method):
    company, actor, _, _, asset, finance, profile = _custom_profile_context(method=method)
    asset.commissioning_date = date(2005, 7, 1)
    data = {
        "start_rule": "current_month",
        "opening_actual_accumulated_depreciation": "11400.00",
        "opening_book_value": "600.00", "actual_continuation_date": date(2026, 9, 1),
        "expected_total_units": "100" if method == "units_of_production" else None,
        "work_unit": "台时" if method == "units_of_production" else "",
    }
    _, result, _ = _profile_spec(
        asset=asset, finance_data={"original_cost": finance.original_cost,
                                  "fixed_asset_category": finance.fixed_asset_category},
        profile_data=data, policy=profile.depreciation_policy,
    )
    assert result["depreciable_amount"] == Decimal("0.00")
    assert result["lines"] == ()
    with pytest.raises(ValidationError, match="不得晚于原预计寿命终点"):
        _profile_spec(
            asset=asset, finance_data={"original_cost": finance.original_cost},
            profile_data={**data, "opening_actual_accumulated_depreciation": "11399",
                          "opening_book_value": "601"},
            policy=profile.depreciation_policy,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("method,with_usage", [
    ("manual", False), ("units_of_production", False), ("units_of_production", True),
])
def test_settled_batch_does_not_require_manual_amount_or_work_usage(method, with_usage):
    company, actor, _, _, asset, finance, profile = _custom_profile_context(
        method=method, opening_ad=Decimal("11400"), opening_book=Decimal("600"),
        actual_continuation_date=date(2026, 9, 1),
    )
    _create_opening_effects(actor=actor, asset=asset, finance=finance, profile=profile, resolved={
        "opening_actual_accumulated_depreciation": Decimal("11400"),
        "opening_impairment": Decimal("0"), "opening_book_value": Decimal("600"),
    })
    if with_usage:
        record_work_usage(
            actor=actor, profile=profile, period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1), current_units="1", work_unit="台时",
        )
    batch = generate_depreciation_batch(
        actor=actor, company=company, period_start=date(2026, 9, 1),
        period_end=date(2026, 10, 1), idempotency_key="settled-no-inputs",
    )
    item = batch.items.get(asset=asset)
    assert item.status == "skipped"
    assert item.planned_amount == Decimal("0.00")
    assert item.calculation_snapshot_json["skip_reason"] == "已提足折旧"
    confirm_depreciation_batch(actor=actor, batch=batch, reason="已提足无需本期输入")
    assert not asset.depreciation_entries.filter(source_type="batch").exists()


@pytest.mark.django_db
def test_legacy_continuation_review_accepts_settled_balance_after_life_end():
    company, actor, _, _, asset, finance, profile = _custom_profile_context(
        method="straight_line", start_date=date(2005, 7, 1),
        opening_ad=Decimal("11400"), opening_book=Decimal("600"), review_required=True,
    )
    reviewed = review_profile_actual_continuation_date(
        actor=actor, profile=profile, actual_continuation_date=date(2026, 9, 1),
        reason="核定历史已提足资产接续日",
    )
    assert reviewed.actual_continuation_date == date(2026, 9, 1)
    assert reviewed.opening_actual_accumulated_depreciation == Decimal("11400")
    assert reviewed.opening_book_value == Decimal("600")


@pytest.mark.django_db
def test_mixed_import_preserves_opening_and_only_posts_unfinished_asset(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "imports"
    company, actor, category, department, employee, location = sprint5_context(
        role="finance", prefix="INIT-FULL",
    )
    admin = make_user("init-full-admin", "system_admin")
    scheme = make_structurally_valid_active_scheme(actor=admin, company=company, key="INIT-CODE")
    type(scheme).objects.filter(pk=scheme.pk).update(is_default=True)
    set_system_setting(actor=actor, company=company, key="fixed_asset_warning_amount", value="5000.00")
    fixed, policy = finance_configuration(company, actor, key="INIT-POLICY")
    # The ongoing asset is constructed: one 190.00 period remains at 2026-09-01.
    cases = [
        ("settled_residual", "2005-07-01", "2880.00", "2736.00", "144.00", "144.00"),
        ("settled_zero", "1995-05-01", "20160.00", "20160.00", "0.00", "0.00"),
        ("continuing", "2021-10-01", "12000.00", "11210.00", "790.00", "600.00"),
    ]
    rows = []
    for name, start, cost, accumulated, book, salvage in cases:
        row = physical_row(company, category, department, employee, location, **{
            "资产名称": name, "序列号": name, "出厂编号": name, "历史参考编号": name,
            "购置日期": start, "达到可使用状态日期": start,
        })
        add_finance_row(
            row, fixed_category=fixed, policy=policy, cost=cost,
            opening_ad=accumulated, opening_book=book,
            **{"资本化日期": start, "指定起算日期": start,
               "实际接续日": "2026-09-01", "残值方式": "amount",
               "残值率": "", "残值金额": salvage},
        )
        rows.append(row)
    imported = upload_and_validate_import(
        actor=actor, company=company, import_type="asset_initialization",
        uploaded_file=asset_workbook_upload(company, rows), idempotency_key="init-mixed",
    )
    assert imported.status == "validated", [r.errors_json for r in imported.rows.all()]
    confirm_import_batch(actor=actor, batch=imported)
    confirm_import_batch(actor=actor, batch=imported)
    assert Asset.objects.filter(company=company).count() == 3

    assets = {}
    for name, start, cost, accumulated, book, salvage in cases:
        asset = Asset.objects.get(company=company, asset_name=name)
        profile = AssetDepreciationProfile.objects.get(asset=asset)
        assert profile.actual_continuation_date == date(2026, 9, 1)
        assert profile.opening_actual_accumulated_depreciation == Decimal(accumulated)
        assert profile.opening_book_value == Decimal(book)
        add_photo(actor, asset)
        asset = submit_asset_for_finance(actor=actor, asset=asset)
        payload = dict(
            actor=actor, asset=asset,
            finance_data={"accounting_treatment": "fixed_asset", "fixed_asset_category": fixed,
                          "original_cost": cost, "capitalization_date": date.fromisoformat(start)},
            profile_data={"depreciation_policy": policy, "specified_start": date.fromisoformat(start),
                          "actual_continuation_date": date(2026, 9, 1),
                          "salvage_mode": "amount", "salvage_amount": salvage,
                          "opening_actual_accumulated_depreciation": accumulated, "opening_book_value": book},
            code_effective_date=timezone.localdate(), idempotency_key=f"confirm-{name}",
            reason="复核期初余额并确认",
        )
        asset = confirm_asset_finance(**payload)
        confirm_asset_finance(**payload)
        profile.refresh_from_db()
        assert asset.asset_status == "pending_label"
        assert AssetDepreciationProfile.objects.filter(asset=asset).count() == 1
        assert asset.depreciation_entries.filter(source_type="opening").count() == 1
        assert asset.depreciation_entries.aggregate(total=Sum("amount"))["total"] == Decimal(accumulated)
        state = get_asset_depreciation_status(actor=actor, asset=asset)
        assert state["code"] == ("not_fully_depreciated" if name == "continuing" else "fully_depreciated")
        if name != "continuing":
            assert not profile.schedules.exists()
        assets[name] = asset

    september = generate_depreciation_batch(
        actor=actor, company=company, period_start=date(2026, 9, 1),
        period_end=date(2026, 10, 1), idempotency_key="september",
    )
    for name, asset in assets.items():
        item = september.items.get(asset=asset)
        assert item.planned_amount == (Decimal("190.00") if name == "continuing" else Decimal("0.00"))
        assert item.status == ("ready" if name == "continuing" else "skipped")
        if name != "continuing":
            assert item.calculation_snapshot_json["skip_reason"] == "已提足折旧"
    confirm_depreciation_batch(actor=actor, batch=september, reason="确认9月折旧")
    confirm_depreciation_batch(actor=actor, batch=september, reason="重复确认不重记")
    assert DepreciationEntry.objects.filter(batch_item__batch=september).count() == 1
    assert get_asset_depreciation_status(actor=actor, asset=assets["continuing"])["code"] == "fully_depreciated"
    for name in ("settled_residual", "settled_zero"):
        assert not assets[name].depreciation_entries.filter(source_type="batch").exists()

    client.force_login(actor)
    for route in ("assets:asset-detail", "finance:asset-finance-detail"):
        response = client.get(reverse(route, kwargs={"pk": assets["settled_residual"].pk}))
        assert response.status_code == 200
        assert "已提足折旧" in response.content.decode()
    outsider = make_user("init-full-equipment", "equipment")
    with pytest.raises(PermissionDenied):
        get_asset_depreciation_status(actor=outsider, asset=assets["settled_residual"])
    with pytest.raises(PermissionDenied):
        get_asset_depreciation_status(actor=actor, asset=SimpleNamespace(company_id=-1))
    client.force_login(outsider)
    response = client.get(reverse("assets:asset-detail", kwargs={"pk": assets["settled_residual"].pk}))
    assert response.status_code == 200
    assert "折旧状态" not in response.content.decode()

    october = generate_depreciation_batch(
        actor=actor, company=company, period_start=date(2026, 10, 1),
        period_end=date(2026, 11, 1), idempotency_key="october",
    )
    assert {item.status for item in october.items.all()} == {"skipped"}
    assert sum(item.planned_amount for item in october.items.all()) == Decimal("0.00")
    reverse_depreciation_batch(actor=actor, batch=september, reason="验证冲销后状态恢复", idempotency_key="undo-september")
    assert get_asset_depreciation_status(actor=actor, asset=assets["continuing"])["code"] == "not_fully_depreciated"
    assert get_asset_depreciation_status(actor=actor, asset=assets["settled_residual"])["code"] == "fully_depreciated"
