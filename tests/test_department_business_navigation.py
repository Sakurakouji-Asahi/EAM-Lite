"""Department summaries must match the existing authorized destination lists."""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.department_workspace import department_business_context
from apps.supplies.services import transfer_custody
from tests.test_sprint16_services import issued_custody
from tests.test_sprint3_support import (
    complete_initialization, grant_scope, make_asset, make_category, make_company,
    make_department, make_employee, make_location_tree, make_user,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def department_context():
    company, warehouse, root, employee, _source, _target, _item, _issue, custody = issued_custody()
    finance = make_user("department-business-finance", "finance")
    complete_initialization(company, finance)
    child = make_department(company, "BUSINESS-CHILD", parent=root)
    child_employee = make_employee(company, child, "BUSINESS-CHILD-EMPLOYEE")
    make_employee(company, child, "BUSINESS-INACTIVE", status="resigned", active=False)
    category = make_category(company, "BUSINESS-CATEGORY")
    _site, _area, location = make_location_tree(company, "BUSINESS-L")
    for department, responsible in ((root, employee), (child, child_employee)):
        make_asset(actor=finance, company=company, category=category, department=department,
            employee=responsible, location=location, asset_name=f"业务资产 {department.code}")
    transfer_custody(actor=warehouse, custody=custody, target_department=child,
        target_employee=child_employee, quantity=Decimal("1"), business_date=date(2026, 8, 26),
        reason="测试部门查阅范围", idempotency_key="department-business-child-transfer")
    foreign_company = make_company("BUSINESS-FOREIGN", active=False)
    foreign = make_department(foreign_company, "BUSINESS-FOREIGN")
    return {"company":company, "root":root, "child":child, "finance":finance, "foreign":foreign}


def assert_destination_counts(client, page, expected):
    cards = {card["key"]:card for card in page.context["department_business"]["cards"]}
    assert {key:card["count"] for key,card in cards.items()} == expected
    for card in cards.values():
        destination = client.get(card["url"])
        assert destination.status_code == 200
        assert destination.context["page_obj"].paginator.count == card["count"]
    assert "仅本部门" in cards["custodies"]["scope"]
    assert "下级" in cards["assets"]["scope"] and "下级" in cards["employees"]["scope"]
    return cards


def test_department_business_links_match_branch_asset_and_employee_but_exact_custody(client, department_context):
    context = department_context
    client.force_login(context["finance"])
    before = AuditLog.objects.count()
    origin = reverse("masterdata:department-list")+"?q=部门&status=all"
    page = client.get(reverse("masterdata:department-detail", args=[context["root"].pk]), {"return_to":origin})
    assert page.status_code == 200 and page.context["back_url"] == origin
    assert_destination_counts(client, page, {"assets":2, "employees":3, "custodies":1})
    html = page.content.decode()
    assert "当前金额" not in html and "单位成本" not in html and "登录账号" not in html
    assert AuditLog.objects.count() == before
    assert client.get(reverse("masterdata:department-detail", args=[context["foreign"].pk])).status_code == 404
    assert department_business_context(context["finance"], context["company"], context["foreign"]) is None


@pytest.mark.parametrize("descendants,expected", [(False,{"assets":1,"employees":1,"custodies":1}),
                                                  (True,{"assets":2,"employees":3,"custodies":1})])
def test_department_manager_counts_do_not_widen_existing_grants(client, department_context, descendants, expected):
    context = department_context
    manager = make_user("department-business-manager", "department_manager")
    grant_scope(manager, context["company"], context["root"], descendants=descendants)
    client.force_login(manager)
    page = client.get(reverse("masterdata:department-detail", args=[context["root"].pk]))
    assert page.status_code == 200
    assert_destination_counts(client, page, expected)
    if not descendants:
        assert client.get(reverse("masterdata:department-detail", args=[context["child"].pk])).status_code == 404
        assert department_business_context(manager, context["company"], context["child"]) is None


def test_empty_employee_scope_and_initialization_gate_do_not_create_dead_links(client):
    company = make_company("BUSINESS-EMPTY")
    department = make_department(company, "BUSINESS-EMPTY-D")
    actor = make_user("department-business-empty-finance", "finance")
    client.force_login(actor)
    page = client.get(reverse("masterdata:department-detail", args=[department.pk]))
    cards = {card["key"]:card for card in page.context["department_business"]["cards"]}
    assert "assets" not in cards
    assert cards["employees"]["count"] == 0 and cards["employees"]["url"] is None
    assert "当前范围内暂无可查阅档案" in page.content.decode()
    assert client.get(cards["custodies"]["url"]).status_code == 200
    outsider = make_user("department-business-employee", "employee")
    assert department_business_context(outsider, company, department) is None
    client.force_login(outsider)
    assert client.get(reverse("masterdata:department-detail", args=[department.pk])).status_code == 403
