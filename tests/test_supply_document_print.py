from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.masterdata.models import UserDepartmentScope
from apps.supplies.models import SupplyDocument, SupplyStockLedger
from apps.supplies.services import create_supply_document, post_supply_document, cancel_supply_document, reverse_supply_document
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_company, make_department, make_employee, make_supply_warehouse, make_user, seed_supply_stock


pytestmark = pytest.mark.django_db


@pytest.fixture
def print_context():
    company, actor, department, employee, source, target, paper, chair = supply_context()
    paper.unit = "盒"
    paper.specification = "A4 / 500张"
    paper.brand = "核对品牌"
    paper.model = "P-500"
    paper.save()
    chair.specification = "25 × 30"
    chair.save()
    for item in (paper, chair):
        seed_supply_stock(actor=actor, company=company, warehouse=source, item=item,
            quantity="10", unit_cost="30.123456", key=f"print-opening-{item.item_code}")
    document = create_supply_document(actor=actor, company=company, document_type="issue", data={
        "business_date": date(2026, 10, 4), "source_warehouse": source, "department": department,
        "employee": employee, "idempotency_key": "print-issue", "remark": "现场核对后签收"},
        lines=[{"item": paper, "quantity": Decimal("1.2345"), "line_remark": "整盒与余量分别核对"},
               {"item": chair, "quantity": Decimal("2.3456")}])
    post_supply_document(actor=actor, document=document)
    return company, actor, department, employee, source, target, paper, chair, document


def test_print_uses_current_detail_data_exact_units_and_existing_cost_visibility(client, print_context):
    company, actor, _, employee, source, _, _, _, document = print_context
    client.force_login(actor)
    before = (SupplyDocument.objects.count(), SupplyStockLedger.objects.count())
    response = client.get(reverse("supplies:document-detail", args=[document.pk]), {
        "return_to": reverse("supplies:document-list") + "?q=print&status=posted&page=2"})
    assert response.status_code == 200 and response.context["show_cost"]
    html = response.content.decode()
    for value in (document.document_no, company.name, source.name, employee.name, "1.2345 盒", "2.3456 把",
                  "A4 / 500张", "P-500", "25 × 30", "整盒与余量分别核对", "接收 / 签收（适用时）", "打印 / 保存为 PDF"):
        assert value in html
    assert "过账单价" in html and str(document.lines.order_by("line_no").first().posted_unit_cost) in html
    assert "3.5801" not in html
    assert response.context["workflow_return_url"].endswith("?q=print&status=posted&page=2")
    assert (SupplyDocument.objects.count(), SupplyStockLedger.objects.count()) == before


def test_relation_roles_keep_printable_quantities_but_never_get_cost_or_unrelated_documents(client, print_context):
    company, actor, department, employee, source, _, paper, _, document = print_context
    employee_user = make_user("print-employee", "employee")
    employee.user = employee_user
    employee.save(update_fields=["user"])
    manager = make_user("print-manager", "department_manager")
    UserDepartmentScope.objects.create(company=company, user=manager, department=department,
        include_descendants=True, assigned_by=actor)
    other_department = make_department(company, "PRINT-OTHER")
    other_employee = make_employee(company, other_department, "PRINT-OTHER-E")
    unrelated = create_supply_document(actor=actor, company=company, document_type="issue", data={
        "business_date": date(2026, 10, 4), "source_warehouse": source, "department": other_department,
        "employee": other_employee, "idempotency_key": "print-unrelated"},
        lines=[{"item": paper, "quantity": Decimal("1")}])
    for user in (employee_user, manager):
        client.force_login(user)
        response = client.get(reverse("supplies:document-detail", args=[document.pk]))
        assert response.status_code == 200 and not response.context["show_cost"]
        html = response.content.decode()
        assert "1.2345 盒" in html and "A4 / 500张" in html and "打印 / 保存为 PDF" in html
        for financial_value in ("录入单价", "过账单价", "过账金额", "30.123456", "过账金额预估"):
            assert financial_value not in html
        assert all("posted_amount" not in row and "posted_unit_cost" not in row for row in response.context["line_rows"])
        assert client.get(reverse("supplies:document-detail", args=[unrelated.pk])).status_code == 404


def test_draft_cancelled_and_reversed_prints_mark_the_status_and_do_not_offer_receipt_signature(client, print_context):
    company, actor, _, _, _, target, paper, _, _ = print_context
    client.force_login(actor)
    for status, notice in (("draft", "尚未过账，仅供核对，不作为出入库凭证"),
                           ("cancelled", "已取消 · 不作为出入库凭证"),
                           ("reversed", "已冲销 · 原单已撤销业务影响")):
        document = create_supply_document(actor=actor, company=company, document_type="receipt", data={
            "business_date": date(2026, 10, 4), "target_warehouse": target, "idempotency_key": f"print-{status}"},
            lines=[{"item": paper, "quantity": Decimal("1.2345"), "entered_unit_cost": Decimal("2")}])
        if status == "cancelled":
            cancel_supply_document(actor=actor, document=document, reason="签收前取消")
        elif status == "reversed":
            post_supply_document(actor=actor, document=document)
            reverse_supply_document(actor=actor, document=document, idempotency_key="print-reverse", reason="记录核对冲销")
        response = client.get(reverse("supplies:document-detail", args=[document.pk]))
        html = response.content.decode()
        assert response.status_code == 200 and notice in html
        assert "经办 / 核对" in html and "接收 / 签收（适用时）" not in html
        if status == "cancelled":
            assert "签收前取消" in html


def test_print_entry_inherits_company_and_role_access_from_document_detail(client, print_context):
    _, actor, _, _, _, _, _, _, document = print_context
    foreign_company = make_company("PRINT-FOREIGN", active=False)
    foreign = SupplyDocument.objects.create(company=foreign_company, document_no="PRINT-FOREIGN-DOC",
        document_type="receipt", status="draft", business_date=date(2026, 10, 4), created_by=actor,
        target_warehouse=make_supply_warehouse(foreign_company, "PRINT-FOREIGN-WH"), idempotency_key="print-foreign")
    client.force_login(actor)
    assert client.get(reverse("supplies:document-detail", args=[foreign.pk])).status_code == 404
    client.force_login(make_user("print-hr", "hr"))
    assert client.get(reverse("supplies:document-detail", args=[document.pk])).status_code == 403
    client.logout()
    assert client.get(reverse("supplies:document-detail", args=[document.pk])).status_code == 302
