"""Management mode navigation must follow physical records, not accounting labels."""
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.finance.services import confirm_asset_finance
from apps.supplies.models import SupplyItem
from tests.test_navigation_information_architecture import parse_navigation
from tests.test_sprint13_support import make_supply_category, make_supply_item
from tests.test_sprint3_support import make_company, make_user
from tests.test_unified_asset_identity import context, registered


pytestmark = pytest.mark.django_db(transaction=True)


def test_individual_durables_and_accounting_filter_have_distinct_populations(client, context):
    lv = registered(context, "navigation-lv", management_attribute="LV", asset_name="逐件工具")
    fa = registered(context, "navigation-fa", management_attribute="FA", asset_name="财务非固定设备")
    confirm_asset_finance(
        actor=context["finance"], asset=fa,
        finance_data={"accounting_treatment": "controlled_non_fixed", "original_cost": Decimal("1200.00")},
        profile_data={}, idempotency_key="navigation-finance", reason="根据资料确认",
    )
    client.force_login(context["finance"])
    all_assets = client.get(reverse("assets:asset-list"))
    assert {item.pk for item in all_assets.context["page"]} == {lv.pk, fa.pk}

    individual = client.get(reverse("supplies:individual-durable-list"), follow=True)
    assert [item.pk for item in individual.context["page"]] == [lv.pk]
    assert parse_navigation(individual).active_items == ["individual_durables"]
    assert "低值耐用品（逐件）" in individual.content.decode()

    accounting = client.get(reverse("assets:asset-list"), {"accounting_treatment": "controlled_non_fixed"})
    assert [item.pk for item in accounting.context["page"]] == [fa.pk]
    assert parse_navigation(accounting).active_items == ["asset_ledger"]
    assert accounting.context["app_navigation"]["page_label"] == "全部逐件资产"


def test_dashboard_individual_count_matches_lv_list_before_finance_confirmation(client, context):
    first = registered(context, "dashboard-lv-1", management_attribute="LV")
    second = registered(context, "dashboard-lv-2", management_attribute="LV")
    fa = registered(context, "dashboard-fa", management_attribute="FA")
    confirm_asset_finance(
        actor=context["finance"], asset=fa,
        finance_data={"accounting_treatment": "controlled_non_fixed", "original_cost": Decimal("1200.00")},
        profile_data={}, idempotency_key="dashboard-finance", reason="根据资料确认",
    )
    client.force_login(context["finance"])
    dashboard = client.get(reverse("supplies:dashboard"))
    assert dashboard.status_code == 200
    assert dashboard.context["dashboard"]["individual_durable_count"] == 2
    assert dashboard.context["dashboard"]["controlled_non_fixed_count"] == 1
    assert 'data-individual-durable-count="2"' in dashboard.content.decode()
    listing = client.get(reverse("supplies:individual-durable-list"), follow=True)
    assert {item.pk for item in listing.context["page"]} == {first.pk, second.pk}


@pytest.mark.parametrize(
    "mode,nav_item,title",
    [("durable_quantity", "supply_durable_items", "低值耐用品（按数量）"),
     ("consumable", "supply_consumables", "低值易耗品")],
)
def test_quantity_mode_links_filter_actual_records_and_keep_creation_mode(client, context, mode, nav_item, title):
    company = context["company"]
    category = make_supply_category(company)
    durable = make_supply_item(company, category, "DURABLE", item_type="durable_quantity")
    consumable = make_supply_item(company, category, "CONSUMABLE", item_type="consumable")
    other_company = make_company("NAVOTHER", active=False)
    other_category = make_supply_category(other_company)
    make_supply_item(other_company, other_category, "OTHER", item_type=mode)
    client.force_login(context["finance"])
    response = client.get(reverse("supplies:item-list"), {"item_type": mode})
    assert response.status_code == 200
    expected = durable if mode == "durable_quantity" else consumable
    assert [item.pk for item in response.context["page_obj"]] == [expected.pk]
    assert response.context["page_title"] == title
    assert parse_navigation(response).active_items == [nav_item]
    assert f'{reverse("supplies:item-create")}?item_type={mode}' in response.content.decode()
    create = client.get(reverse("supplies:item-create"), {"item_type": mode})
    assert create.status_code == 200
    assert create.context["form"]["item_type"].value() == mode
    assert parse_navigation(create).active_items == [nav_item]


def test_quantity_mode_links_preserve_equipment_and_employee_permissions(client, context):
    category = make_supply_category(context["company"])
    client.force_login(context["equipment"])
    durable = client.get(reverse("supplies:item-list"), {"item_type": "durable_quantity"})
    assert durable.context["can_create_selected"] is True
    consumables = client.get(reverse("supplies:item-list"), {"item_type": "consumable"})
    assert consumables.status_code == 200
    assert consumables.context["can_create_selected"] is False
    assert client.get(reverse("supplies:item-create"), {"item_type": "consumable"}).status_code == 403
    denied = client.post(reverse("supplies:item-create"), {
        "item_code": "DENIED", "name": "不可越权新增的易耗品", "category": category.pk,
        "item_type": "consumable", "unit": "个", "minimum_stock_quantity": "0",
    })
    assert denied.status_code in {200, 403}
    assert not SupplyItem.objects.filter(item_code="DENIED").exists()
    client.force_login(make_user("mode-employee", "employee"))
    home = client.get(reverse("home"))
    assert "supply_consumables" not in parse_navigation(home).items
    for mode in ("durable_quantity", "consumable"):
        assert client.get(reverse("supplies:item-list"), {"item_type": mode}).status_code == 403


def test_unknown_quantity_mode_falls_back_to_all_items_without_false_highlight(client, context):
    client.force_login(context["finance"])
    response = client.get(reverse("supplies:item-list"), {"item_type": "unknown"})
    assert response.status_code == 200
    assert response.context["selected_item_type"] == ""
    assert response.context["page_title"] == "全部数量物品"
    assert parse_navigation(response).active_items == ["supply_items"]
