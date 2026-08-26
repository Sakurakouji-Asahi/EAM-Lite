from decimal import Decimal

import pytest
from django.urls import reverse

from apps.masterdata.models import UserDepartmentScope
from apps.supplies.models import SupplyCustody, SupplyDocument
from apps.supplies.services import post_supply_document
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


def http_custodies():
    company = make_company()
    warehouse_actor = make_user("s16-http-warehouse", "warehouse")
    department_a = make_department(company, "A")
    department_b = make_department(company, "B")
    employee_user = make_user("s16-http-employee", "employee")
    employee_a = make_employee(
        company, department_a, "EA", user=employee_user
    )
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
    issues = []
    for key, department, employee in (
        ("a", department_a, employee_a),
        ("b", department_b, employee_b),
    ):
        issue = make_issue_document(
            actor=warehouse_actor,
            company=company,
            warehouse=warehouse,
            item=durable,
            department=department,
            employee=employee,
            quantity="1",
            key=f"s16-http-{key}",
        )
        post_supply_document(document=issue, actor=warehouse_actor)
        issues.append(issue)
    return {
        "company": company,
        "warehouse_actor": warehouse_actor,
        "warehouse": warehouse,
        "department_a": department_a,
        "department_b": department_b,
        "employee_user": employee_user,
        "custody_a": SupplyCustody.objects.get(origin_issue_line=issues[0].lines.get()),
        "custody_b": SupplyCustody.objects.get(origin_issue_line=issues[1].lines.get()),
    }


def test_equipment_can_return_transfer_writeoff_but_not_consumable_or_general_issue(client):
    values = http_custodies()
    equipment = make_user("s16-http-equipment", "equipment")
    target_department = make_department(values["company"], "TARGET")
    target_employee = make_employee(values["company"], target_department, "TARGET-E")
    client.force_login(equipment)
    detail = client.get(
        reverse("supplies:custody-detail", args=[values["custody_a"].pk])
    )
    assert detail.status_code == 200
    assert "归还仓库".encode() in detail.content
    assert "责任转交".encode() in detail.content
    assert "报损".encode() in detail.content

    response = client.post(
        reverse("supplies:durable-return-create", args=[values["custody_a"].pk]),
        {
            "target_warehouse": str(values["warehouse"].pk),
            "quantity": "0.5000",
            "business_date": "2026-08-26",
            "reason": "归还一部分",
            "idempotency_key": "s16-http-return",
        },
    )
    assert response.status_code == 302
    document = SupplyDocument.objects.get(idempotency_key="s16-http-return")
    assert document.lines.get().source_custody_id == values["custody_a"].pk
    post_response = client.post(
        reverse("supplies:document-post", args=[document.pk]),
        {"confirm": "on", "idempotency_key": document.idempotency_key},
    )
    assert post_response.status_code == 302
    posted_detail = client.get(
        reverse("supplies:document-detail", args=[document.pk])
    )
    assert f"来源保管 {values['custody_a'].pk}".encode() in posted_detail.content

    transfer_response = client.post(
        reverse("supplies:custody-transfer", args=[values["custody_b"].pk]),
        {
            "target_department": str(target_department.pk),
            "target_employee": str(target_employee.pk),
            "quantity": "0.5000",
            "business_date": "2026-08-26",
            "reason": "责任调整",
            "idempotency_key": "s16-http-transfer",
        },
    )
    assert transfer_response.status_code == 302
    assert SupplyCustody.objects.filter(
        parent_custody=values["custody_b"], department=target_department
    ).exists()
    assert client.get(reverse("supplies:document-create", args=["issue"])).status_code == 403

    consumable = make_supply_item(
        values["company"], make_supply_category(values["company"], "C2"), "PAPER"
    )
    seed_supply_stock(
        actor=values["warehouse_actor"],
        company=values["company"],
        warehouse=values["warehouse"],
        item=consumable,
        key="s16-http-consumable-stock",
    )
    consumable_issue = make_issue_document(
        actor=values["warehouse_actor"],
        company=values["company"],
        warehouse=values["warehouse"],
        item=consumable,
        department=values["department_a"],
        key="s16-http-consumable-issue",
    )
    post_supply_document(document=consumable_issue, actor=values["warehouse_actor"])
    assert client.get(
        reverse(
            "supplies:consumable-return-create",
            args=[consumable_issue.lines.get().pk],
        )
    ).status_code == 403


def test_employee_my_custodies_is_personal_readonly_and_cost_hidden(client):
    values = http_custodies()
    client.force_login(values["employee_user"])
    page = client.get(reverse("supplies:my-custodies"))
    assert page.status_code == 200
    assert str(values["custody_a"].pk).encode() in page.content
    assert str(values["custody_b"].pk).encode() not in page.content
    assert b"80.00" not in page.content
    for name, args in (
        ("supplies:durable-return-create", [values["custody_a"].pk]),
        ("supplies:custody-transfer", [values["custody_a"].pk]),
        ("supplies:custody-write-off", [values["custody_a"].pk, "loss"]),
    ):
        assert client.get(reverse(name, args=args)).status_code == 403


def test_department_manager_scope_and_management_readonly(client):
    values = http_custodies()
    manager = make_user("s16-http-manager", "department_manager")
    UserDepartmentScope.objects.create(
        company=values["company"],
        user=manager,
        department=values["department_a"],
        include_descendants=False,
        assigned_by=values["warehouse_actor"],
    )
    client.force_login(manager)
    assert client.get(
        reverse("supplies:custody-detail", args=[values["custody_a"].pk])
    ).status_code == 200
    assert client.get(
        reverse("supplies:custody-detail", args=[values["custody_b"].pk])
    ).status_code == 404
    cross = client.post(
        reverse("supplies:custody-transfer", args=[values["custody_a"].pk]),
        {
            "target_department": str(values["department_b"].pk),
            "quantity": "0.5000",
            "business_date": "2026-08-26",
            "reason": "跨范围",
            "idempotency_key": "s16-manager-cross-http",
        },
    )
    assert cross.status_code == 200
    assert not SupplyCustody.objects.filter(
        parent_custody=values["custody_a"], department=values["department_b"]
    ).exists()

    management = make_user("s16-http-management", "management")
    client.force_login(management)
    assert client.get(reverse("supplies:custody-list")).status_code == 200
    assert client.get(
        reverse("supplies:custody-write-off", args=[values["custody_a"].pk, "scrap"])
    ).status_code == 403
