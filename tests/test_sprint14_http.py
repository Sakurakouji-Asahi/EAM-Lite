from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.supplies.models import SupplyDocument, SupplyDocumentLine, SupplyStockBalance
from apps.supplies.services import post_supply_document
from tests.test_sprint14_support import (
    make_company,
    make_supply_category,
    make_supply_document,
    make_supply_item,
    make_supply_warehouse,
    make_user,
)


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "role", ["system_admin", "finance", "warehouse", "equipment", "management"]
)
def test_stock_read_pages_are_available_to_approved_roles(client, role):
    make_company()
    actor = make_user(f"s14-view-{role}", role)
    client.force_login(actor)
    for name in (
        "supplies:document-list",
        "supplies:stock-balance-list",
        "supplies:stock-ledger-list",
    ):
        assert client.get(reverse(name)).status_code == 200


@pytest.mark.parametrize("role", ["employee", "department_manager", "hr"])
def test_unapproved_roles_cannot_open_stock_pages_or_post_directly(client, role):
    company = make_company()
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    creator = make_user(f"s14-creator-{role}", "warehouse")
    document = make_supply_document(
        actor=creator,
        company=company,
        warehouse=warehouse,
        item=item,
        key=f"denied-{role}",
    )
    actor = make_user(f"s14-denied-{role}", role)
    client.force_login(actor)
    assert client.get(reverse("supplies:document-list")).status_code == 403
    assert client.get(reverse("supplies:document-detail", args=[document.pk])).status_code == 403
    assert client.post(
        reverse("supplies:document-post", args=[document.pk]),
        {"confirm": "on", "idempotency_key": document.idempotency_key},
    ).status_code == 403


@pytest.mark.parametrize("role", ["equipment", "management"])
def test_equipment_and_management_are_read_only_for_direct_get_and_post(client, role):
    company = make_company()
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    creator = make_user(f"s14-readonly-creator-{role}", "warehouse")
    document = make_supply_document(
        actor=creator,
        company=company,
        warehouse=warehouse,
        item=item,
        key=f"readonly-{role}",
    )
    actor = make_user(f"s14-readonly-{role}", role)
    client.force_login(actor)
    assert client.get(reverse("supplies:document-detail", args=[document.pk])).status_code == 200
    assert client.get(reverse("supplies:document-create", args=["opening"])).status_code == 403
    assert client.get(reverse("supplies:document-edit", args=[document.pk])).status_code == 403
    assert client.post(
        reverse("supplies:document-cancel", args=[document.pk]), {"reason": "无权"}
    ).status_code == 403
    assert client.post(
        reverse("supplies:document-post", args=[document.pk]),
        {"confirm": "on", "idempotency_key": document.idempotency_key},
    ).status_code == 403


def test_document_http_create_post_and_posted_edit_rejection(client):
    company = make_company()
    actor = make_user("s14-http-warehouse", "warehouse")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    client.force_login(actor)
    response = client.post(
        reverse("supplies:document-create", args=["opening"]),
        {
            "business_date": "2026-08-26",
            "target_warehouse": str(warehouse.pk),
            "external_reference": "OPEN-001",
            "counterparty_name": "",
            "remark": "HTTP 创建",
            "idempotency_key": "s14-http-create",
            "lines-TOTAL_FORMS": "5",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "100",
            "lines-0-item": str(item.pk),
            "lines-0-quantity": "10.0000",
            "lines-0-entered_unit_cost": "100.000000",
            "lines-0-line_remark": "",
        },
    )
    assert response.status_code == 302
    document = SupplyDocument.objects.get(idempotency_key="s14-http-create")
    assert not SupplyStockBalance.objects.exists()
    edit_page = client.get(reverse("supplies:document-edit", args=[document.pk]))
    assert edit_page.status_code == 200
    assert "新增明细行".encode() in edit_page.content
    assert b"supply-document-form.js" in edit_page.content
    document_page = client.get(reverse("supplies:document-list"))
    assert "日常入库".encode() in document_page.content
    assert "领用出库".encode() not in document_page.content

    post_response = client.post(
        reverse("supplies:document-post", args=[document.pk]),
        {"confirm": "on", "idempotency_key": document.idempotency_key},
    )
    assert post_response.status_code == 302
    document.refresh_from_db()
    assert document.status == "posted"
    assert SupplyStockBalance.objects.get().amount_on_hand == Decimal("1000.00")
    assert client.get(reverse("supplies:document-edit", args=[document.pk])).status_code == 403
    assert client.post(
        reverse("supplies:document-edit", args=[document.pk]), {}
    ).status_code == 403


def test_company_boundary_returns_404_for_foreign_document_id(client):
    company = make_company()
    actor = make_user("s14-company-http", "finance")
    other = make_company("OTHER", active=False)
    other_category = make_supply_category(other, "OTHER-CAT")
    other_warehouse = make_supply_warehouse(other, "OTHER-WH")
    other_item = make_supply_item(other, other_category, "OTHER-ITEM")
    foreign = SupplyDocument.objects.create(
        company=other,
        document_no="QC-2026-999999",
        document_type="opening",
        business_date=date(2026, 8, 26),
        target_warehouse=other_warehouse,
        status="draft",
        idempotency_key="foreign-document",
    )
    SupplyDocumentLine.objects.create(
        company=other,
        document=foreign,
        line_no=1,
        item=other_item,
        quantity=Decimal("1"),
        entered_unit_cost=Decimal("1"),
    )
    client.force_login(actor)
    assert client.get(reverse("supplies:document-detail", args=[foreign.pk])).status_code == 404
    assert client.post(
        reverse("supplies:document-post", args=[foreign.pk]),
        {"confirm": "on", "idempotency_key": foreign.idempotency_key},
    ).status_code == 404
    assert company.is_active


def test_cost_values_are_visible_only_through_approved_stock_scope(client):
    company = make_company()
    creator = make_user("s14-cost-creator", "warehouse")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    document = make_supply_document(
        actor=creator,
        company=company,
        warehouse=warehouse,
        item=item,
        key="cost-http",
    )
    post_supply_document(document=document, actor=creator)

    management = make_user("s14-cost-management", "management")
    client.force_login(management)
    detail = client.get(reverse("supplies:document-detail", args=[document.pk]))
    assert detail.status_code == 200
    assert b"100.000000" in detail.content
    balance = client.get(reverse("supplies:stock-balance-list"))
    assert b"1000.00" in balance.content

    employee = make_user("s14-cost-employee", "employee")
    client.force_login(employee)
    assert client.get(reverse("supplies:document-detail", args=[document.pk])).status_code == 403
