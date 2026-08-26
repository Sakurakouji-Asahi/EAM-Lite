"""Read-only bulk financial truth used by Sprint 11 reports.

This module only aggregates the append-only Finance ledger.  It deliberately
does not reproduce depreciation formulas or mutate accounting state.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def approved_depreciation_entries(queryset=None):
    """Return only posted facts whose authoritative source is approved.

    Depreciation services intentionally create entries before closing their
    parent batch inside one transaction.  Read-side reporting must inspect the
    final source state instead of treating every physically present entry as
    posted accounting history.  Original entries remain approved after their
    source is marked ``reversed``; the linked reversal contributes separately.
    """
    from apps.finance.models import DepreciationEntry

    queryset = queryset if queryset is not None else DepreciationEntry.objects.all()
    return queryset.filter(
        asset__finance__accounting_treatment="fixed_asset",
        asset__finance__finance_confirmed_at__isnull=False,
    ).filter(
        Q(source_type="opening")
        | Q(
            source_type="batch",
            batch_item__batch__status__in=("confirmed", "reversed"),
        )
        | Q(
            source_type="adjustment",
            value_adjustment__adjustment_type="depreciation_adjustment",
            value_adjustment__status__in=("confirmed", "reversed"),
        )
    )


def money(value):
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def depreciation_entry_business_date(entry):
    if entry.reversal_of_id:
        return entry.entry_date
    if entry.source_type == "batch":
        return entry.period_start
    if entry.source_type == "adjustment":
        return entry.value_adjustment.effective_date
    return entry.entry_date


@dataclass(frozen=True, slots=True)
class FinancialBalance:
    original_cost: Decimal = ZERO
    accumulated_depreciation: Decimal = ZERO
    impairment: Decimal = ZERO
    book_value: Decimal = ZERO


def _entry_effective_before(entry, boundary: date) -> bool:
    if entry.reversal_of_id:
        return entry.entry_date < boundary
    if entry.source_type == "batch":
        return entry.period_end <= boundary
    if entry.source_type == "adjustment":
        return entry.value_adjustment.effective_date < boundary
    return entry.entry_date < boundary


def balances_by_asset(*, company, asset_ids, boundary: date):
    """Return actual balances immediately before a half-open date boundary."""

    from apps.finance.models import (
        AssetFinance,
        AssetValueAdjustment,
        DepreciationEntry,
    )

    ids = tuple(asset_ids)
    if not ids:
        return {}
    finances = {
        row.asset_id: row
        for row in AssetFinance.objects.filter(
            company=company,
            asset_id__in=ids,
            finance_confirmed_at__isnull=False,
        )
    }
    cost_later = defaultdict(lambda: ZERO)
    impairment = defaultdict(lambda: ZERO)
    adjustments = AssetValueAdjustment.objects.filter(
        company=company,
        asset_id__in=ids,
        status__in=("confirmed", "reversed"),
    ).only("asset_id", "adjustment_type", "effective_date", "amount")
    for item in adjustments.iterator(chunk_size=1000):
        if item.adjustment_type == "cost_correction" and item.effective_date >= boundary:
            cost_later[item.asset_id] += item.amount
        if item.effective_date >= boundary:
            continue
        if item.adjustment_type in {"opening_impairment", "impairment"}:
            impairment[item.asset_id] += item.amount
        elif item.adjustment_type == "impairment_reversal":
            impairment[item.asset_id] -= item.amount

    accumulated = defaultdict(lambda: ZERO)
    entries = approved_depreciation_entries(
        DepreciationEntry.objects.filter(company=company, asset_id__in=ids)
    ).select_related("value_adjustment").only(
        "asset_id", "source_type", "entry_date", "period_end", "amount",
        "reversal_of_id", "value_adjustment__effective_date",
    )
    for entry in entries.iterator(chunk_size=1000):
        if _entry_effective_before(entry, boundary):
            accumulated[entry.asset_id] += entry.amount

    result = {}
    for asset_id, finance in finances.items():
        original = money(finance.original_cost - cost_later[asset_id])
        ad = money(accumulated[asset_id])
        imp = money(impairment[asset_id])
        result[asset_id] = FinancialBalance(
            original_cost=original,
            accumulated_depreciation=ad,
            impairment=imp,
            book_value=money(original - ad - imp),
        )
    return result


def tplus_period_components(*, company, asset_ids, period_start, period_end):
    """Return approved signed actual components for one T+ period."""

    from apps.finance.models import DepreciationEntry

    ids = tuple(asset_ids)
    components = defaultdict(
        lambda: {
            "opening_accumulated_depreciation": ZERO,
            "automatic_depreciation": ZERO,
            "manual_depreciation": ZERO,
            "adjustment_net": ZERO,
            "reversal_net": ZERO,
        }
    )
    if not ids:
        return components
    entries = approved_depreciation_entries(
        DepreciationEntry.objects.filter(company=company, asset_id__in=ids)
    ).select_related(
        "batch_item__batch", "value_adjustment"
    ).only(
        "asset_id", "source_type", "entry_date", "period_start", "period_end",
        "amount", "reversal_of_id", "batch_item__calculation_method",
        "batch_item__batch__batch_type", "value_adjustment__adjustment_type",
        "value_adjustment__effective_date",
    )
    for entry in entries.iterator(chunk_size=1000):
        bucket = components[entry.asset_id]
        if entry.reversal_of_id:
            if period_start <= entry.entry_date < period_end:
                bucket["reversal_net"] += entry.amount
            elif entry.entry_date < period_start:
                bucket["opening_accumulated_depreciation"] += entry.amount
            continue
        if entry.source_type == "opening":
            if entry.entry_date < period_end:
                bucket["opening_accumulated_depreciation"] += entry.amount
            continue
        if entry.source_type == "batch":
            if entry.period_end <= period_start:
                bucket["opening_accumulated_depreciation"] += entry.amount
            elif period_start <= entry.period_start < period_end:
                key = (
                    "manual_depreciation"
                    if entry.batch_item.calculation_method == "manual"
                    else "automatic_depreciation"
                )
                bucket[key] += entry.amount
            continue
        if entry.source_type == "adjustment":
            effective = entry.value_adjustment.effective_date
            if effective < period_start:
                bucket["opening_accumulated_depreciation"] += entry.amount
            elif effective < period_end and entry.value_adjustment.adjustment_type == "depreciation_adjustment":
                bucket["adjustment_net"] += entry.amount
    for bucket in components.values():
        for key, value in bucket.items():
            bucket[key] = money(value)
    return components


def confirmed_entries_for_period(*, company, asset_ids, period_start, period_end):
    """Materialize actual ledger entries relevant to a selected period."""

    from apps.finance.models import DepreciationEntry

    return list(
        approved_depreciation_entries(
            DepreciationEntry.objects.filter(
                company=company, asset_id__in=tuple(asset_ids)
            )
        )
        .filter(
            Q(reversal_of__isnull=True, source_type="batch", period_start__gte=period_start, period_start__lt=period_end)
            | Q(reversal_of__isnull=True, source_type="opening", entry_date__gte=period_start, entry_date__lt=period_end)
            | Q(reversal_of__isnull=True, source_type="adjustment", value_adjustment__effective_date__gte=period_start, value_adjustment__effective_date__lt=period_end)
            | Q(reversal_of__isnull=False, entry_date__gte=period_start, entry_date__lt=period_end)
        )
        .select_related(
            "asset", "posted_by", "reversal_of", "value_adjustment",
            "batch_item__batch__confirmed_by", "batch_item__batch__generated_by",
        )
        .order_by("asset__asset_code", "entry_date", "created_at", "id")
    )


__all__ = [
    "FinancialBalance", "ZERO", "approved_depreciation_entries", "balances_by_asset",
    "confirmed_entries_for_period", "depreciation_entry_business_date", "money",
    "tplus_period_components",
]
