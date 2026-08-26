from decimal import Decimal

import pytest
from django.urls import reverse

from apps.masterdata.models import UserDepartmentScope
from apps.supplies.models import SupplyCustody, SupplyDocument, SupplyStockBalance
from apps.supplies.services import post_supply_document
from tests.test_sprint15_services import make_return, supply_context
from tests.test_sprint15_support import (
    make_company,
    make_department,
    make_employee,
    make_issue_document,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db


def test_issue_and_transfer_forms_do_not_expose_cost_and_http_issue_posts(client):
    company = make_company()
    actor = make_user("s15-http-warehouse", "warehouse")
    department = make_department(company)
    employee = make_employee(company, department)
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    target = make_supply_warehouse(company, "TARGET")
    item = make_supply_item(company, category)
    seed_supply_stock(actor=actor, company=company, warehouse=warehouse, item=item)
    client.force_login(actor)

    issue_page = client.get(reverse("supplies:document-create", args=["issue"]))
    transfer_page = client.get(reverse("supplies:document-create", args=["transfer"]))
    assert issue_page.status_code == transfer_page.status_code == 200
    assert b"entered_unit_cost" not in issue_page.content
    assert b"entered_unit_cost" not in transfer_page.content
    assert client.get(reverse("supplies:document-create", args=["return"])).status_code == 404

    response = client.post(
        reverse("supplies:document-create", args=["issue"]),
        {
            "business_date": "2026-08-26",
            "source_warehouse": str(warehouse.pk),
            "department": str(department.pk),
            "employee": str(employee.pk),
            "remark": "HTTP 领用",
            "idempotency_key": "s15-http-issue",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "100",
            "lines-0-item": str(item.pk),
            "lines-0-quantity": "2.0000",
            "lines-0-entered_unit_cost": "0.000001",
            "lines-0-line_remark": "",
        },
    )
    assert response.status_code == 302
    document = SupplyDocument.objects.get(idempotency_key="s15-http-issue")
    assert document.lines.get().entered_unit_cost is None
    post_response = client.post(
        reverse("supplies:document-post", args=[document.pk]),
        {"confirm": "on", "idempotency_key": document.idempotency_key},
    )
    assert post_response.status_code == 302
    assert SupplyStockBalance.objects.get(warehouse=warehouse, item=item).quantity_on_hand == Decimal("8.0000")
    assert target.company_id == company.pk


def test_consumable_return_starts_from_posted_issue_line(client):
    company, actor, department, _, source, target, item, _ = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item)
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=item,
        department=department,
        quantity="3",
    )
    post_supply_document(document=issue, actor=actor)
    line = issue.lines.get()
    client.force_login(actor)
    page = client.get(reverse("supplies:consumable-return-create", args=[line.pk]))
    assert page.status_code == 200
    assert issue.document_no.encode() in page.content
    response = client.post(
        reverse("supplies:consumable-return-create", args=[line.pk]),
        {
            "target_warehouse": str(target.pk),
            "quantity": "1.0000",
            "reason": "未使用",
            "business_date": "2026-08-26",
            "idempotency_key": "s15-http-return",
        },
    )
    assert response.status_code == 302
    returned = SupplyDocument.objects.get(idempotency_key="s15-http-return")
    assert returned.document_type == "return"
    assert returned.department_id == issue.department_id
    assert returned.lines.get().source_issue_line_id == line.pk
    assert returned.lines.get().entered_unit_cost is None


def test_custody_scope_navigation_and_cost_field_isolation(client):
    company = make_company()
    warehouse_actor = make_user("s15-scope-warehouse", "warehouse")
    department_a = make_department(company, "A")
    department_b = make_department(company, "B")
    employee_user_a = make_user("s15-employee-a", "employee")
    employee_a = make_employee(company, department_a, "EA", user=employee_user_a)
    employee_b = make_employee(company, department_b, "EB")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    durable = make_supply_item(
        company, category, "CHAIR", item_type="durable_quantity", unit="把"
    )
    seed_supply_stock(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="4",
        unit_cost="80",
    )
    issue_a = make_issue_document(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=department_a,
        employee=employee_a,
        quantity="1",
        key="scope-a",
    )
    issue_b = make_issue_document(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=department_b,
        employee=employee_b,
        quantity="1",
        key="scope-b",
    )
    post_supply_document(document=issue_a, actor=warehouse_actor)
    post_supply_document(document=issue_b, actor=warehouse_actor)
    custody_a = SupplyCustody.objects.get(origin_issue_line=issue_a.lines.get())
    custody_b = SupplyCustody.objects.get(origin_issue_line=issue_b.lines.get())

    manager = make_user("s15-manager-a", "department_manager")
    UserDepartmentScope.objects.create(
        company=company,
        user=manager,
        department=department_a,
        include_descendants=True,
        assigned_by=warehouse_actor,
    )
    client.force_login(manager)
    manager_list = client.get(reverse("supplies:custody-list"))
    assert manager_list.status_code == 200
    assert str(custody_a.pk).encode() in manager_list.content
    assert str(custody_b.pk).encode() not in manager_list.content
    assert durable.name.encode() in manager_list.content
    assert client.get(reverse("supplies:custody-detail", args=[custody_a.pk])).status_code == 200
    assert client.get(reverse("supplies:custody-detail", args=[custody_b.pk])).status_code == 404
    manager_document_list = client.get(reverse("supplies:document-list"))
    assert issue_a.document_no.encode() in manager_document_list.content
    assert issue_b.document_no.encode() not in manager_document_list.content
    assert b"80.000000" not in client.get(
        reverse("supplies:custody-detail", args=[custody_a.pk])
    ).content

    client.force_login(employee_user_a)
    employee_list = client.get(reverse("supplies:custody-list"))
    assert employee_list.status_code == 200
    assert issue_a.document_no.encode() in employee_list.content
    assert issue_b.document_no.encode() not in employee_list.content
    assert client.get(reverse("supplies:custody-detail", args=[custody_b.pk])).status_code == 404
    dashboard = client.get(reverse("supplies:dashboard"))
    assert dashboard.status_code == 200
    assert reverse("supplies:custody-list").encode() in dashboard.content
    assert client.get(reverse("supplies:stock-balance-list")).status_code == 403

    equipment = make_user("s15-equipment-view", "equipment")
    client.force_login(equipment)
    assert client.get(reverse("supplies:custody-detail", args=[custody_a.pk])).status_code == 200
    assert client.get(reverse("supplies:custody-detail", args=[custody_b.pk])).status_code == 200


@pytest.mark.parametrize("role", ["management", "equipment", "department_manager", "employee"])
def test_readonly_roles_cannot_create_post_or_reverse_directly(client, role):
    company = make_company()
    creator = make_user(f"s15-creator-{role}", "warehouse")
    department = make_department(company)
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category)
    seed_supply_stock(actor=creator, company=company, warehouse=warehouse, item=item)
    issue = make_issue_document(
        actor=creator,
        company=company,
        warehouse=warehouse,
        item=item,
        department=department,
        key=f"readonly-{role}",
    )
    post_supply_document(document=issue, actor=creator)
    actor = make_user(f"s15-readonly-{role}", role)
    client.force_login(actor)
    assert client.get(reverse("supplies:document-create", args=["issue"])).status_code == 403
    assert client.post(
        reverse("supplies:document-reverse", args=[issue.pk]),
        {"reason": "无权", "idempotency_key": f"denied-{role}"},
    ).status_code == 403


def test_foreign_company_custody_and_issue_ids_return_404(client):
    company = make_company()
    actor = make_user("s15-company-scope", "warehouse")
    other = make_company("OTHER", active=False)
    other_department = make_department(other, "OD")
    other_category = make_supply_category(other, "OC")
    other_warehouse = make_supply_warehouse(other, "OW")
    other_item = make_supply_item(other, other_category, "OI", item_type="durable_quantity")
    other_actor = make_user("s15-other-warehouse", "warehouse")
    # Current-company Service correctly rejects the inactive foreign company;
    # build historical rows through the model layer for object-ID scope checks.
    from datetime import date
    from apps.supplies.models import SupplyDocumentLine

    foreign_doc = SupplyDocument.objects.create(
        company=other,
        document_no="LY-2026-999999",
        document_type="issue",
        business_date=date(2026, 8, 26),
        source_warehouse=other_warehouse,
        department=other_department,
        idempotency_key="foreign-issue",
    )
    foreign_line = SupplyDocumentLine.objects.create(
        company=other,
        document=foreign_doc,
        line_no=1,
        item=other_item,
        quantity=Decimal("1"),
    )
    client.force_login(actor)
    assert client.get(reverse("supplies:document-detail", args=[foreign_doc.pk])).status_code == 404
    assert client.get(reverse("supplies:consumable-return-create", args=[foreign_line.pk])).status_code == 404
    assert other_actor.pk and company.is_active
