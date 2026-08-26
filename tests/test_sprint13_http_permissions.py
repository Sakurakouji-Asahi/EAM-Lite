import pytest
from django.urls import reverse

from apps.supplies.models import SupplyItem
from tests.test_sprint13_support import (
    make_company,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
)


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "role",
    ["system_admin", "finance", "warehouse", "equipment", "management"],
)
def test_authorized_roles_can_view_supplies_dashboard_and_lists(client, role):
    make_company()
    actor = make_user(f"viewer-{role}", role)
    client.force_login(actor)
    for name in (
        "supplies:dashboard",
        "supplies:category-list",
        "supplies:warehouse-list",
        "supplies:item-list",
    ):
        assert client.get(reverse(name)).status_code == 200


@pytest.mark.parametrize("role", ["employee", "hr", "department_manager"])
def test_unrelated_roles_are_denied_direct_urls(client, role):
    make_company()
    actor = make_user(f"denied-{role}", role)
    client.force_login(actor)
    assert client.get(reverse("supplies:dashboard")).status_code == 403
    assert client.get(reverse("supplies:item-list")).status_code == 403


def test_management_is_read_only_and_equipment_only_manages_durables(client):
    company = make_company()
    category = make_supply_category(company)
    management = make_user("management-readonly", "management")
    client.force_login(management)
    assert client.get(reverse("supplies:item-create")).status_code == 403
    assert client.post(
        reverse("supplies:category-create"),
        {"code": "NO", "name": "无权"},
    ).status_code == 403

    equipment = make_user("equipment-durable", "equipment")
    client.force_login(equipment)
    durable_response = client.post(
        reverse("supplies:item-create"),
        {
            "item_code": "CHAIR",
            "name": "普通办公椅",
            "category": str(category.pk),
            "item_type": "durable_quantity",
            "unit": "把",
            "minimum_stock_quantity": "0",
        },
    )
    assert durable_response.status_code == 302
    assert SupplyItem.objects.filter(company=company, item_code="CHAIR").exists()

    consumable_response = client.post(
        reverse("supplies:item-create"),
        {
            "item_code": "PAPER",
            "name": "复印纸",
            "category": str(category.pk),
            "item_type": "consumable",
            "unit": "箱",
            "minimum_stock_quantity": "0",
        },
    )
    assert consumable_response.status_code == 200
    assert not SupplyItem.objects.filter(company=company, item_code="PAPER").exists()
    assert client.get(reverse("supplies:category-create")).status_code == 403
    assert client.get(reverse("supplies:warehouse-create")).status_code == 403


def test_individual_asset_shortcut_waits_for_existing_asset_initialization(client):
    make_company()
    warehouse = make_user("warehouse-no-asset-init", "warehouse")
    client.force_login(warehouse)

    response = client.get(reverse("supplies:dashboard"))

    assert response.status_code == 200
    assert reverse("assets:asset-create").encode() not in response.content
    assert "请先完成现有资产初始化".encode() in response.content


def test_direct_cross_company_post_is_rejected_and_lists_are_paginated(client):
    company = make_company()
    actor = make_user("warehouse-http", "warehouse")
    category = make_supply_category(company)
    other = make_company("OTHER", active=False)
    foreign_category = make_supply_category(other, "FOREIGN")
    client.force_login(actor)

    response = client.post(
        reverse("supplies:item-create"),
        {
            "item_code": "CROSS",
            "name": "跨公司",
            "category": str(foreign_category.pk),
            "item_type": "consumable",
            "unit": "个",
            "minimum_stock_quantity": "0",
        },
    )
    assert response.status_code == 200
    assert not SupplyItem.objects.filter(item_code="CROSS").exists()

    warehouse = make_supply_warehouse(company)
    for index in range(30):
        make_supply_item(
            company,
            category,
            f"ITEM-{index:02d}",
            item_type="durable_quantity" if index % 2 else "consumable",
            default_warehouse=warehouse,
        )
    page = client.get(reverse("supplies:item-list"))
    assert page.status_code == 200
    assert len(page.context["page_obj"].object_list) == 25
    filtered = client.get(
        reverse("supplies:item-list"),
        {"q": "ITEM-01", "item_type": "durable_quantity", "status": "all"},
    )
    assert filtered.status_code == 200
    assert [item.item_code for item in filtered.context["page_obj"]] == ["ITEM-01"]
    invalid_category = client.get(
        reverse("supplies:item-list"), {"category": "not-a-uuid"}
    )
    assert invalid_category.status_code == 200
    assert list(invalid_category.context["page_obj"]) == []
