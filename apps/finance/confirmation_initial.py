"""Saved values shared by individual and bulk financial confirmation."""
import uuid
from decimal import Decimal
from django.utils import timezone
from apps.finance.models import AssetFinance


def finance_confirmation_initial(asset):
    finance = AssetFinance.objects.filter(asset=asset).first()
    draft_profile = (
        asset.depreciation_profiles.select_related("depreciation_policy")
        .filter(status="draft")
        .order_by("version")
        .first()
    )
    initial = {
        "idempotency_key": uuid.uuid4().hex,
        "code_effective_date": timezone.localdate(),
        "opening_actual_accumulated_depreciation": Decimal("0.00"),
        "opening_impairment": Decimal("0.00"),
    }
    if finance is not None:
        for field in (
            "accounting_treatment",
            "accounting_treatment_reason",
            "original_cost",
            "fixed_asset_category",
            "capitalization_date",
            "finance_remark",
        ):
            initial[field] = getattr(finance, field)
        initial["opening_impairment"] = finance.impairment_balance_cache
    if draft_profile is not None:
        finance_opening_impairment = (
            finance.original_cost
            - draft_profile.opening_actual_accumulated_depreciation
            - draft_profile.opening_book_value
            if finance is not None and finance.original_cost is not None
            else Decimal("0.00")
        )
        initial.update(
            {
                "depreciation_policy": draft_profile.depreciation_policy,
                "useful_life_months": draft_profile.useful_life_months,
                "salvage_mode": draft_profile.salvage_mode,
                "salvage_rate": draft_profile.salvage_rate,
                "salvage_amount": draft_profile.salvage_amount,
                "method": draft_profile.method,
                "posting_period": draft_profile.posting_period,
                "start_rule": draft_profile.start_rule,
                "stop_rule": draft_profile.stop_rule,
                "specified_start_date": (draft_profile.start_date if draft_profile.start_rule in {"specified_date", "specified_month"} else None),
                "actual_continuation_date": draft_profile.actual_continuation_date,
                "expected_total_units": draft_profile.expected_total_units,
                "work_unit": draft_profile.work_unit,
                "annual_posting_month": draft_profile.annual_posting_month,
                "opening_actual_accumulated_depreciation": (
                    draft_profile.opening_actual_accumulated_depreciation
                ),
                "opening_impairment": finance_opening_impairment,
            }
        )
    return initial
