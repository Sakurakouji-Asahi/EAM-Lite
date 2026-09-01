from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import AssetDepreciationProfile
from apps.finance.services import (
    confirm_asset_finance,
    confirm_depreciation_batch,
    generate_depreciation_batch,
    preview_asset_depreciation,
    run_theoretical_depreciation,
)
from apps.supplies.services import durable_management_totals, post_supply_document
from tests.test_sprint4_acceptance import _base_context, _pending_asset
from tests.test_sprint6_support import formal_asset_context
from tests.test_sprint7_support import active_fixed_asset_context
from tests.test_sprint15_support import (
    make_department,
    make_issue_document,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db


def test_controlled_non_fixed_is_rejected_by_preview_direct_services_batch_and_urls(client):
    context = _base_context("S16GUARD")
    pending = _pending_asset(context, "S16GUARD-PENDING")
    with pytest.raises(ValidationError, match="逐件低值耐用品"):
        preview_asset_depreciation(
            actor=context["finance"],
            asset=pending,
            finance_data={
                "accounting_treatment": "controlled_non_fixed",
                "original_cost": Decimal("100.00"),
            },
            profile_data={},
        )

    controlled = confirm_asset_finance(
        actor=context["finance"],
        asset=pending,
        finance_data={
            "accounting_treatment": "controlled_non_fixed",
            "original_cost": Decimal("100.00"),
        },
        code_effective_date=timezone.localdate(),
        idempotency_key="s16-guard-formalize",
        reason="逐件低值耐用品测试",
    )
    assert not AssetDepreciationProfile.objects.filter(asset=controlled).exists()
    with pytest.raises(ValidationError, match="逐件低值耐用品"):
        run_theoretical_depreciation(
            actor=context["finance"],
            asset=controlled,
            as_of_date=date(2026, 8, 31),
            parameters={},
            idempotency_key="s16-controlled-theoretical",
        )
    batch = generate_depreciation_batch(
        actor=context["finance"],
        company=context["company"],
        period_start=date(2026, 8, 1),
        period_end=date(2026, 9, 1),
        idempotency_key="s16-controlled-batch",
    )
    assert not batch.items.exists()

    client.force_login(context["finance"])
    assert client.get(
        reverse("finance:theoretical-run", args=[controlled.pk])
    ).status_code == 403
    assert client.get(
        reverse("finance:value-adjustment", args=[controlled.pk])
    ).status_code == 403


def test_fixed_asset_depreciation_still_generates_and_confirms_normally():
    context, asset, _qr, _profile, _policy = active_fixed_asset_context("S16FIX")
    period_start = timezone.localdate().replace(day=1)
    period_end = (period_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    batch = generate_depreciation_batch(
        actor=context["finance"],
        company=context["company"],
        period_start=period_start,
        period_end=period_end,
        idempotency_key="s16-fixed-batch",
    )
    assert batch.items.filter(asset=asset, status="ready").exists()
    confirm_depreciation_batch(
        actor=context["finance"], batch=batch, reason="固定资产回归"
    )
    assert asset.depreciation_entries.filter(batch_item__batch=batch).exists()


def test_asset_list_four_way_accounting_filter_and_individual_durable_shortcuts(client):
    context, fixed, _qr, _profile, _policy = active_fixed_asset_context("S16LIST")
    controlled_pending = _pending_asset(context, "S16LIST-CONTROLLED")
    controlled = confirm_asset_finance(
        actor=context["finance"],
        asset=controlled_pending,
        finance_data={
            "accounting_treatment": "controlled_non_fixed",
            "original_cost": Decimal("2000.00"),
        },
        code_effective_date=timezone.localdate(),
        idempotency_key="s16-list-controlled",
        reason="逐件低值耐用品",
    )
    unconfirmed = _pending_asset(context, "S16LIST-UNCONFIRMED")
    client.force_login(context["finance"])

    fixed_response = client.get(
        reverse("assets:asset-list"), {"accounting_treatment": "fixed_asset"}
    )
    assert fixed_response.status_code == 200
    assert fixed.asset_name.encode() in fixed_response.content
    assert controlled.asset_name.encode() not in fixed_response.content

    controlled_response = client.get(
        reverse("assets:asset-list"),
        {"accounting_treatment": "controlled_non_fixed"},
    )
    assert controlled.asset_name.encode() in controlled_response.content
    assert "受控非固定资产".encode() in controlled_response.content
    assert fixed.asset_name.encode() not in controlled_response.content

    unconfirmed_response = client.get(
        reverse("assets:asset-list"), {"accounting_treatment": "unconfirmed"}
    )
    assert unconfirmed.asset_name.encode() in unconfirmed_response.content
    assert controlled.asset_name.encode() not in unconfirmed_response.content

    shortcut = client.get(reverse("supplies:individual-durable-list"))
    assert shortcut.status_code == 302
    assert "view=individual_durable" in shortcut["Location"]
    create_shortcut = client.get(reverse("supplies:individual-durable-create"))
    assert create_shortcut.status_code == 302
    create_page = client.get(create_shortcut["Location"])
    assert "每件单独建档".encode() in create_page.content


def test_quantity_managed_amounts_and_individual_asset_original_cost_are_separate():
    context, _asset, _qr = formal_asset_context(
        "S16TOTAL", cost=Decimal("2000.00")
    )
    company = context["company"]
    warehouse_actor = make_user("s16-total-warehouse", "warehouse")
    department = make_department(company, "S16TOTAL-USE")
    category = make_supply_category(company, "S16TOTAL-CAT")
    warehouse = make_supply_warehouse(company, "S16TOTAL-WH")
    durable = make_supply_item(
        company,
        category,
        "S16TOTAL-CHAIR",
        item_type="durable_quantity",
    )
    seed_supply_stock(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="3",
        unit_cost="80",
        key="s16-total-stock",
    )
    issue = make_issue_document(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=department,
        quantity="1",
        key="s16-total-issue",
    )
    post_supply_document(document=issue, actor=warehouse_actor)
    totals = durable_management_totals(company=company)
    assert totals["durable_stock_amount"] == Decimal("160.00")
    assert totals["durable_open_custody_amount"] == Decimal("80.00")
    assert totals["durable_managed_amount"] == Decimal("240.00")
    assert totals["controlled_non_fixed_asset_quantity"] == 1
    assert totals["controlled_non_fixed_original_cost"] == Decimal("2000.00")
    assert totals["durable_managed_amount"] != totals[
        "controlled_non_fixed_original_cost"
    ]
