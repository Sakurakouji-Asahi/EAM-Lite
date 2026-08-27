from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.masterdata.models import UserDepartmentScope
from apps.offboarding.services import initiate_clearance
from apps.supplies.models import SupplyCustody
from apps.supplies.services import (
    create_supply_count_task,
    post_supply_document,
    publish_supply_count_task,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_department,
    make_employee,
    make_issue_document,
    make_user,
    seed_supply_stock,
)
from tests.test_sprint17_services import make_count
from tests.test_sprint3_support import complete_initialization


pytestmark = pytest.mark.django_db


def test_warehouse_count_pages_create_publish_record_and_management_is_readonly(client):
    company, warehouse_actor, department, _, warehouse, _, item, _ = supply_context()
    seed_supply_stock(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=item,
        quantity="5",
        unit_cost="10",
        key="s17-http-stock",
    )
    client.force_login(warehouse_actor)
    assert client.get(reverse("supplies:count-task-list")).status_code == 200
    create_page = client.get(reverse("supplies:count-task-create"))
    assert create_page.status_code == 200
    response = client.post(
        reverse("supplies:count-task-create"),
        {
            "name": "HTTP 仓库盘点",
            "count_domain": "warehouse_stock",
            "warehouse": str(warehouse.pk),
            "planned_start": "2026-08-27",
            "planned_end": "2026-08-28",
            "remark": "页面验收",
            "idempotency_key": "s17-http-count-create",
        },
    )
    assert response.status_code == 302
    task = company.supply_count_tasks.get(idempotency_key="s17-http-count-create")
    assert response.url == reverse("supplies:count-task-detail", args=[task.pk])
    publish = client.post(reverse("supplies:count-task-publish", args=[task.pk]))
    assert publish.status_code == 302
    line = task.lines.get(item=item)
    entry = client.get(
        reverse("supplies:count-line-record", args=[task.pk, line.pk])
    )
    assert entry.status_code == 200
    assert b"5.0000" in entry.content
    saved = client.post(
        reverse("supplies:count-line-record", args=[task.pk, line.pk]),
        {"counted_quantity": "5", "remark": ""},
    )
    assert saved.status_code == 302

    management = make_user("s17-http-management", "management")
    client.force_login(management)
    assert client.get(reverse("supplies:count-task-list")).status_code == 200
    assert client.get(reverse("supplies:count-task-detail", args=[task.pk])).status_code == 200
    assert client.get(reverse("supplies:count-task-close", args=[task.pk])).status_code == 403
    assert client.post(reverse("supplies:count-task-cancel", args=[task.pk]), {"reason": "越权"}).status_code == 403


def test_employee_sees_and_records_only_own_custody_line_without_cost(client):
    company, warehouse_actor, department, employee, warehouse, _, _, durable = supply_context()
    employee_user = make_user("s17-http-employee", "employee")
    employee.user = employee_user
    employee.save(update_fields=["user", "updated_at"])
    other = make_employee(company, department, "S17-OTHER")
    seed_supply_stock(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="4",
        unit_cost="80",
        key="s17-http-custody-stock",
    )
    for target, key in ((employee, "mine"), (other, "other")):
        document = make_issue_document(
            actor=warehouse_actor,
            company=company,
            warehouse=warehouse,
            item=durable,
            department=department,
            employee=target,
            quantity="1",
            key=f"s17-http-custody-{key}",
        )
        post_supply_document(document=document, actor=warehouse_actor)
    equipment = make_user("s17-http-equipment", "equipment")
    task = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=department,
        key="s17-http-custody-count",
    )
    publish_supply_count_task(task=task, actor=equipment)
    mine = task.lines.get(custody__employee=employee)
    other_line = task.lines.get(custody__employee=other)
    client.force_login(employee_user)
    detail = client.get(reverse("supplies:count-task-detail", args=[task.pk]))
    assert detail.status_code == 200
    assert mine.item_name_snapshot.encode() in detail.content
    assert other.name.encode() not in detail.content
    assert b"80.00" not in detail.content
    assert client.get(
        reverse("supplies:count-line-record", args=[task.pk, mine.pk])
    ).status_code == 200
    assert client.get(
        reverse("supplies:count-line-record", args=[task.pk, other_line.pk])
    ).status_code == 403
    assert client.get(reverse("supplies:count-task-close", args=[task.pk])).status_code == 403


def test_department_manager_cannot_cross_department_and_clearance_detail_shows_supply_block(client):
    company, warehouse_actor, department, employee, warehouse, _, _, durable = supply_context()
    other_department = make_department(company, "S17-OUT")
    manager = make_user("s17-http-manager", "department_manager")
    UserDepartmentScope.objects.create(
        company=company,
        user=manager,
        department=department,
        include_descendants=False,
        assigned_by=warehouse_actor,
    )
    own = make_count(
        actor=manager,
        company=company,
        domain="custody",
        department=department,
        key="s17-http-manager-own",
    )
    equipment = make_user("s17-http-equipment-2", "equipment")
    cross = make_count(
        actor=equipment,
        company=company,
        domain="custody",
        department=other_department,
        key="s17-http-manager-cross",
    )
    client.force_login(manager)
    assert client.get(reverse("supplies:count-task-detail", args=[own.pk])).status_code == 200
    assert client.get(reverse("supplies:count-task-detail", args=[cross.pk])).status_code == 404

    seed_supply_stock(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="1",
        unit_cost="80",
        key="s17-http-clearance-stock",
    )
    issue = make_issue_document(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=department,
        employee=employee,
        quantity="1",
        key="s17-http-clearance-issue",
    )
    post_supply_document(document=issue, actor=warehouse_actor)
    hr = make_user("s17-http-hr", "hr")
    clearance = initiate_clearance(
        actor=hr,
        employee=employee,
        idempotency_key="s17-http-clearance",
    )
    complete_initialization(company, hr)
    client.force_login(hr)
    page = client.get(reverse("offboarding:clearance-detail", args=[clearance.pk]))
    assert page.status_code == 200
    assert "数量型低值耐用品".encode() in page.content
    assert durable.name.encode() in page.content
