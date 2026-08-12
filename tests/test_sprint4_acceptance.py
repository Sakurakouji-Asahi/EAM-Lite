from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.models import AssetCodeHistory, AssetQrIdentity
from apps.assets.services import submit_asset_for_finance
from apps.audit.models import AuditLog
from apps.coding.services import (
    activate_scheme,
    create_scheme,
    set_default_scheme,
)
from apps.finance.models import (
    AssetDepreciationProfile,
    AssetFinance,
    AssetValueAdjustment,
    DepreciationEntry,
    DepreciationPolicy,
    DepreciationSchedule,
    FinanceFormalizationRequest,
)
from apps.finance.services import (
    activate_depreciation_policy,
    confirm_asset_finance,
    create_depreciation_policy,
    create_fixed_asset_category,
    resolve_depreciation_policy,
    set_category_default_depreciation_policy,
)
from apps.masterdata.models import (
    InitializationSetting,
    IssuedCode,
    SequenceCounter,
)
from apps.masterdata.services import (
    complete_initialization,
    compute_initialization_progress,
    set_system_setting,
)
from tests.test_sprint3_support import (
    add_photo,
    make_active_scheme,
    make_asset,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
)


pytestmark = pytest.mark.django_db


def _policy_data(key: str, *, method: str = "straight_line") -> dict:
    return {
        "policy_key": key,
        "name": f"{key} 折旧政策",
        "method": method,
        "posting_period": "monthly",
        "start_rule": "next_month",
        "stop_rule": "next_month",
        "default_useful_life_months": 60,
        "default_salvage_mode": "rate",
        "default_salvage_rate": Decimal("0.05"),
        "default_salvage_amount": None,
        "annual_posting_month": None,
        "work_unit": "",
        "effective_from": date(2024, 1, 1),
        "effective_to": None,
    }


def _create_active_policy(
    *, company, actor, key: str, make_default: bool = False
) -> DepreciationPolicy:
    draft = create_depreciation_policy(
        actor=actor,
        company=company,
        data=_policy_data(key),
    )
    return activate_depreciation_policy(
        actor=actor,
        policy=draft,
        make_default=make_default,
        reason=f"启用 {key} 验收政策",
    )


def _mark_initialized(company, admin) -> InitializationSetting:
    setting, _ = InitializationSetting.objects.get_or_create(company=company)
    for field in (
        "company_configured",
        "departments_configured",
        "employees_configured",
        "categories_configured",
        "locations_configured",
        "coding_scheme_configured",
        "finance_rules_configured",
        "permissions_configured",
        "users_configured",
    ):
        setattr(setting, field, True)
    setting.initialization_completed = True
    setting.completed_by = admin
    setting.completed_at = timezone.now()
    setting.save()
    return setting


def _base_context(
    prefix: str,
    *,
    coding: str = "default",
    include_policy: bool = True,
    initialize: bool = True,
) -> dict:
    company = make_company(prefix)
    admin = make_user(f"{prefix.lower()}-admin", "system_admin")
    finance = make_user(f"{prefix.lower()}-finance", "finance")
    equipment = make_user(f"{prefix.lower()}-equipment", "equipment")
    department = make_department(company, f"{prefix}-D")
    employee = make_employee(company, department, f"{prefix}-E")
    category = make_category(company, f"{prefix}-CAT")
    _site, _area, location = make_location_tree(company, f"{prefix}-L")

    scheme = None
    if coding == "default":
        scheme = make_active_scheme(
            actor=admin, company=company, key=f"{prefix}-CODE"
        )
        set_default_scheme(actor=admin, scheme=scheme)
    elif coding == "missing_minor":
        draft = create_scheme(
            actor=admin,
            company=company,
            data={
                "scheme_key": f"{prefix}-CODE",
                "name": f"{prefix} 缺少来源方案",
                "description": "一级分类无法提供二级分类编码",
                "reset_mode": "never",
                "sequence_start": 1,
                "category_scope_level": None,
                "effective_from": timezone.localdate(),
                "effective_to": None,
            },
            segments=[
                {
                    "sequence_order": 1,
                    "segment_type": "minor_category_code",
                    "fixed_value": None,
                    "format_string": None,
                    "sequence_length": None,
                    "zero_pad": None,
                },
                {
                    "sequence_order": 2,
                    "segment_type": "sequence",
                    "fixed_value": None,
                    "format_string": None,
                    "sequence_length": 4,
                    "zero_pad": True,
                },
            ],
        )
        scheme = activate_scheme(actor=admin, scheme=draft)
        set_default_scheme(actor=admin, scheme=scheme)

    policy = None
    if include_policy:
        set_system_setting(
            actor=finance,
            company=company,
            key="fixed_asset_warning_amount",
            value="5000.00",
        )
        policy = _create_active_policy(
            company=company,
            actor=finance,
            key=f"{prefix}-POLICY",
            make_default=True,
        )
    if initialize:
        _mark_initialized(company, admin)
    return {
        "company": company,
        "admin": admin,
        "finance": finance,
        "equipment": equipment,
        "department": department,
        "employee": employee,
        "category": category,
        "location": location,
        "scheme": scheme,
        "policy": policy,
    }


def _pending_asset(context: dict, suffix: str):
    asset = make_asset(
        actor=context["equipment"],
        company=context["company"],
        category=context["category"],
        department=context["department"],
        employee=context["employee"],
        location=context["location"],
        asset_name=f"{suffix} 验收资产",
        serial_number=f"SN-{suffix}",
        factory_number=f"FN-{suffix}",
        commissioning_date=timezone.localdate(),
    )
    add_photo(context["equipment"], asset)
    return submit_asset_for_finance(actor=context["equipment"], asset=asset)


def _confirm_nonfixed(
    context: dict,
    asset,
    *,
    cost,
    key: str,
    treatment_reason: str = "",
    actor=None,
):
    return confirm_asset_finance(
        actor=actor or context["finance"],
        asset=asset,
        finance_data={
            "accounting_treatment": "controlled_non_fixed",
            "accounting_treatment_reason": treatment_reason,
            "original_cost": cost,
        },
        code_effective_date=timezone.localdate(),
        idempotency_key=key,
        reason="Sprint 4 最低验收正式化",
    )


def _artifact_counts() -> dict[str, int]:
    return {
        "finance": AssetFinance.objects.count(),
        "profile": AssetDepreciationProfile.objects.count(),
        "entry": DepreciationEntry.objects.count(),
        "counter": SequenceCounter.objects.count(),
        "issued": IssuedCode.objects.count(),
        "history": AssetCodeHistory.objects.count(),
        "qr": AssetQrIdentity.objects.count(),
        "request": FinanceFormalizationRequest.objects.count(),
    }


def test_fixed_asset_below_warning_threshold_creates_complete_formalization_artifacts():
    context = _base_context("S4ACCFIX")
    asset = _pending_asset(context, "fixed-below-threshold")
    fixed_category = create_fixed_asset_category(
        actor=context["finance"],
        company=context["company"],
        data={
            "code": "MACHINE",
            "name": "机器设备",
            "useful_life_months_default": 60,
        },
    )

    asset = confirm_asset_finance(
        actor=context["finance"],
        asset=asset,
        finance_data={
            "accounting_treatment": "fixed_asset",
            "fixed_asset_category": fixed_category,
            "original_cost": "100.00",
            "capitalization_date": timezone.localdate(),
        },
        code_effective_date=timezone.localdate(),
        idempotency_key="fixed-below-warning-threshold",
        reason="财务明确认定为固定资产",
    )

    finance = AssetFinance.objects.get(asset=asset)
    profile = AssetDepreciationProfile.objects.get(asset=asset)
    issued = IssuedCode.objects.get(pk=asset.current_issued_code_id)
    qr = AssetQrIdentity.objects.get(asset=asset, status="active")
    request = FinanceFormalizationRequest.objects.get(asset=asset)
    assert asset.asset_status == "pending_label"
    assert finance.accounting_treatment == "fixed_asset"
    assert finance.recognition_threshold_snapshot == Decimal("5000.00")
    assert profile.depreciation_policy_id == context["policy"].pk
    assert DepreciationSchedule.objects.filter(
        asset=asset, depreciation_profile=profile
    ).exists()
    assert issued.display_code == asset.asset_code
    assert AssetCodeHistory.objects.filter(
        asset=asset, event_type="issued", new_issued_code=issued
    ).count() == 1
    assert qr.label_status == "ready_to_print"
    assert request.result_issued_code_id == issued.pk
    assert request.result_finance_id == finance.pk

    audits = AuditLog.objects.filter(
        company=context["company"],
        action__in=(
            "asset_finance_confirm",
            "asset_code_issue",
            "asset_qr_identity_create",
        ),
    )
    assert set(audits.values_list("action", flat=True)) == {
        "asset_finance_confirm",
        "asset_code_issue",
        "asset_qr_identity_create",
    }
    audit_payload = json.dumps(
        list(audits.values("old_data_json", "new_data_json")),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    assert qr.public_token not in audit_payload
    assert asset.asset_name not in qr.public_token
    assert asset.asset_code not in qr.public_token
    assert len(qr.public_token) >= 22


@pytest.mark.parametrize("treatment", ("fixed_asset", "controlled_non_fixed"))
@pytest.mark.parametrize("cost", (None, "-0.01"))
def test_formalization_rejects_empty_or_negative_cost_for_both_treatments(
    treatment, cost
):
    context = _base_context(f"S4COST{treatment[0]}{str(cost)[0]}")
    asset = _pending_asset(context, f"{treatment}-{cost}")
    finance_data = {"accounting_treatment": treatment, "original_cost": cost}
    if treatment == "fixed_asset":
        finance_data.update(
            {
                "fixed_asset_category": create_fixed_asset_category(
                    actor=context["finance"],
                    company=context["company"],
                    data={
                        "code": "FA",
                        "name": "固定资产类别",
                        "useful_life_months_default": 60,
                    },
                ),
                "capitalization_date": timezone.localdate(),
            }
        )

    before = _artifact_counts()
    with pytest.raises(ValidationError):
        confirm_asset_finance(
            actor=context["finance"],
            asset=asset,
            finance_data=finance_data,
            code_effective_date=timezone.localdate(),
            idempotency_key=f"reject-{treatment}-{cost}",
            reason="成本边界验收",
        )
    asset.refresh_from_db()
    assert asset.asset_status == "pending_finance"
    assert asset.asset_code is None
    assert _artifact_counts() == before


def test_high_value_nonfixed_requires_reason_and_zero_cost_has_no_depreciation_facts():
    context = _base_context("S4ACCNF")
    high = _pending_asset(context, "high-nonfixed")
    before = _artifact_counts()
    with pytest.raises(ValidationError, match="必须填写说明"):
        _confirm_nonfixed(
            context,
            high,
            cost="6000.00",
            key="high-nonfixed-no-reason",
        )
    high.refresh_from_db()
    assert high.asset_status == "pending_finance"
    assert _artifact_counts() == before

    high = _confirm_nonfixed(
        context,
        high,
        cost="6000.00",
        key="high-nonfixed-with-reason",
        treatment_reason="金额较高但按受控周转工具管理",
    )
    high_finance = AssetFinance.objects.get(asset=high)
    assert high_finance.accounting_treatment_reason == "金额较高但按受控周转工具管理"
    assert high_finance.recognition_threshold_snapshot == Decimal("5000.00")

    zero = _pending_asset(context, "zero-nonfixed")
    zero = _confirm_nonfixed(
        context,
        zero,
        cost="0",
        key="zero-cost-nonfixed",
    )
    zero_finance = AssetFinance.objects.get(asset=zero)
    assert zero_finance.original_cost == Decimal("0.00")
    assert zero_finance.impairment_balance_cache == Decimal("0.00")
    assert not AssetDepreciationProfile.objects.filter(asset=zero).exists()
    assert not DepreciationEntry.objects.filter(asset=zero).exists()
    assert not AssetValueAdjustment.objects.filter(asset=zero).exists()


@pytest.mark.parametrize("coding", ("none", "missing_minor"))
def test_missing_coding_scheme_or_source_rolls_back_all_formalization_side_effects(
    coding
):
    context = _base_context(f"S4CODE{coding.replace('_', '')}", coding=coding)
    asset = _pending_asset(context, f"coding-{coding}")
    before = _artifact_counts()

    with pytest.raises(ValidationError):
        _confirm_nonfixed(
            context,
            asset,
            cost="100.00",
            key=f"formalize-{coding}",
        )

    asset.refresh_from_db()
    assert asset.asset_status == "pending_finance"
    assert asset.asset_code is None
    assert asset.current_issued_code_id is None
    assert _artifact_counts() == before


def test_policy_resolution_precedence_and_illegal_explicit_source():
    context = _base_context("S4POLICY")
    asset = _pending_asset(context, "policy-resolution")
    category_policy = _create_active_policy(
        company=context["company"],
        actor=context["finance"],
        key="CATEGORY-POLICY",
    )
    explicit_policy = _create_active_policy(
        company=context["company"],
        actor=context["finance"],
        key="EXPLICIT-POLICY",
    )
    set_category_default_depreciation_policy(
        actor=context["finance"],
        category=context["category"],
        policy=category_policy,
        reason="配置分类默认政策",
    )
    asset.refresh_from_db()

    assert resolve_depreciation_policy(asset=asset) == category_policy
    assert (
        resolve_depreciation_policy(
            asset=asset, requested_policy=explicit_policy
        )
        == explicit_policy
    )
    context["category"].default_depreciation_policy = None
    context["category"].save(update_fields=["default_depreciation_policy"])
    asset.refresh_from_db()
    assert resolve_depreciation_policy(asset=asset) == context["policy"]

    draft = create_depreciation_policy(
        actor=context["finance"],
        company=context["company"],
        data=_policy_data("DRAFT-NOT-EFFECTIVE"),
    )
    with pytest.raises(ValidationError, match="不会静默回退"):
        resolve_depreciation_policy(asset=asset, requested_policy=draft)


def test_policy_resolution_rejects_missing_company_default():
    context = _base_context("S4NOPOL", include_policy=False)
    asset = _pending_asset(context, "missing-policy")
    with pytest.raises(ValidationError, match="必须且只能"):
        resolve_depreciation_policy(asset=asset)


def test_setup_step7_depends_on_real_finance_data_and_is_finance_only():
    company = make_company("S4SETUP7")
    admin = make_user("s4setup7-admin", "system_admin")
    finance = make_user("s4setup7-finance", "finance")
    setting = InitializationSetting.objects.create(company=company)

    with pytest.raises(PermissionDenied):
        set_system_setting(
            actor=admin,
            company=company,
            key="fixed_asset_warning_amount",
            value="5000.00",
        )
    set_system_setting(
        actor=finance,
        company=company,
        key="fixed_asset_warning_amount",
        value="5000.00",
    )
    setting.refresh_from_db()
    assert setting.finance_rules_configured is False

    policy = _create_active_policy(
        company=company,
        actor=finance,
        key="SETUP7-DEFAULT",
        make_default=True,
    )
    setting.refresh_from_db()
    assert policy.default_salvage_rate == Decimal("0.05")
    assert policy.start_rule == "next_month"
    assert policy.posting_period == "monthly"
    assert setting.finance_rules_configured is True
    assert compute_initialization_progress(company)["finance_rules_configured"] is True


def _setup9_context(prefix: str, *, include_employee: bool = True):
    company = make_company(prefix)
    admin = make_user(f"{prefix.lower()}-admin", "system_admin")
    finance = make_user(f"{prefix.lower()}-finance", "finance")
    department = make_department(company, f"{prefix}-D")
    if include_employee:
        make_employee(company, department, f"{prefix}-E")
    make_category(company, f"{prefix}-CAT")
    make_location_tree(company, f"{prefix}-L")
    scheme = make_active_scheme(actor=admin, company=company, key=f"{prefix}-CODE")
    set_default_scheme(actor=admin, scheme=scheme)
    set_system_setting(
        actor=finance,
        company=company,
        key="fixed_asset_warning_amount",
        value="5000.00",
    )
    _create_active_policy(
        company=company,
        actor=finance,
        key=f"{prefix}-POLICY",
        make_default=True,
    )
    return company, admin


def test_setup_step9_rechecks_nine_real_conditions_and_completes_atomically():
    company, admin = _setup9_context("S4SETUP9")
    progress = compute_initialization_progress(company)
    assert len(progress) == 9
    assert all(progress.values())

    setting = complete_initialization(actor=admin, company=company)
    setting.refresh_from_db()
    assert setting.initialization_completed is True
    assert setting.completed_by_id == admin.pk
    assert setting.completed_at is not None
    assert all(getattr(setting, field) for field in progress)
    assert AuditLog.objects.filter(
        company=company,
        action="initialization_complete",
        object_type="InitializationSetting",
        object_id=str(setting.pk),
    ).count() == 1


def test_setup_step9_missing_condition_does_not_partially_complete_or_audit():
    company, admin = _setup9_context("S4SETUP9MISS", include_employee=False)
    setting = InitializationSetting.objects.get(company=company)
    fields = (
        "company_configured",
        "departments_configured",
        "employees_configured",
        "categories_configured",
        "locations_configured",
        "coding_scheme_configured",
        "finance_rules_configured",
        "permissions_configured",
        "users_configured",
        "initialization_completed",
        "completed_by_id",
        "completed_at",
    )
    before = tuple(getattr(setting, field) for field in fields)

    with pytest.raises(ValidationError, match="employees_configured"):
        complete_initialization(actor=admin, company=company)

    setting.refresh_from_db()
    assert tuple(getattr(setting, field) for field in fields) == before
    assert not AuditLog.objects.filter(
        company=company, action="initialization_complete"
    ).exists()


def test_non_finance_direct_service_call_is_denied_without_side_effects():
    context = _base_context("S4DENY")
    asset = _pending_asset(context, "equipment-direct-call")
    before = _artifact_counts()

    with pytest.raises(PermissionDenied):
        _confirm_nonfixed(
            context,
            asset,
            cost="100.00",
            key="equipment-direct-formalization",
            actor=context["equipment"],
        )

    asset.refresh_from_db()
    assert asset.asset_status == "pending_finance"
    assert asset.asset_code is None
    assert _artifact_counts() == before
