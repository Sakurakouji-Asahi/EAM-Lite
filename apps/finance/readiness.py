"""Financial confirmation is independent from an asset's physical state."""

from django.db.models import Q


FINANCE_CONFIRMABLE_STATES = (
    "pending_label", "in_use", "idle", "loaned", "under_repair", "pending_disposal",
)


def missing_finance_base_fields(asset):
    """Describe missing saved inputs, without claiming a policy or invoice is valid."""
    finance = getattr(asset, "finance", None)
    missing = []
    if finance is None or not finance.accounting_treatment:
        missing.append("会计认定")
    if finance is None or finance.original_cost is None:
        missing.append("原值")
    if finance is not None and finance.accounting_treatment == "fixed_asset":
        if finance.fixed_asset_category_id is None:
            missing.append("固定资产会计类别")
        if finance.capitalization_date is None:
            missing.append("资本化日期")
        if asset.commissioning_date is None:
            missing.append("达到可使用状态日期")
    return missing


def pending_finance_assets(queryset):
    """Filter an already company/permission-scoped queryset."""
    return queryset.filter(
        Q(asset_status="pending_finance")
        | Q(asset_status__in=FINANCE_CONFIRMABLE_STATES, current_issued_code__isnull=False),
        record_status="active",
        finance__finance_confirmed_at__isnull=True,
    )


def filter_pending_finance_assets(queryset, filters):
    """Shared selection rules for the paginated list and all-matching preview."""
    queryset = pending_finance_assets(queryset)
    if filters.get("q"):
        queryset = queryset.filter(Q(asset_code__icontains=filters["q"]) | Q(asset_name__icontains=filters["q"]))
    if filters.get("department"):
        from apps.masterdata.hierarchy import descendant_ids
        from apps.masterdata.models import Department
        department = filters["department"]
        queryset = queryset.filter(department_id__in=descendant_ids(Department, company=department.company, identifier=department.pk))
    if filters.get("data_status"):
        queryset = queryset.filter(finance__isnull=filters["data_status"] == "not_entered")
    if filters.get("import_batch"):
        ids = list(filters["import_batch"].rows.filter(created_object_type="Asset").values_list("created_object_id",flat=True))
        queryset = queryset.filter(pk__in=ids)
    return queryset


def finance_confirmation_pending(asset):
    if asset.record_status != "active":
        return False
    eligible = asset.asset_status == "pending_finance" or (
        asset.asset_status in FINANCE_CONFIRMABLE_STATES and asset.current_issued_code_id is not None
    )
    if not eligible:
        return False
    from apps.finance.models import AssetFinance

    return not AssetFinance.objects.filter(
        asset_id=asset.pk, company_id=asset.company_id,
        finance_confirmed_at__isnull=False,
    ).exists()
