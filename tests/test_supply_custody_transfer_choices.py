from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.masterdata.models import UserDepartmentScope
from apps.supplies.forms import SupplyCustodyTransferForm
from apps.supplies.models import SupplyCustody, SupplyCustodyMovement, SupplyStockBalance, SupplyStockLedger
from tests.test_sprint16_services import issued_custody
from tests.test_sprint15_support import make_company, make_department, make_employee, make_user


pytestmark = pytest.mark.django_db


@pytest.fixture
def transfer_choice_context(client):
    company, actor, department, employee, warehouse, target, chair, original, custody = issued_custody(quantity="3.4567", unit_cost="10")
    receiving_department = make_department(company, "RECEIVING")
    receiving_employee = make_employee(company, receiving_department, "RECEIVER-01", name="同名接收员工")
    same_name_employee = make_employee(company, department, "RECEIVER-02", name="同名接收员工")
    inactive = make_employee(company, receiving_department, "RECEIVER-INACTIVE", is_active=False)
    foreign_company = make_company("TRANSFER-CHOICE-OTHER", active=False)
    foreign = make_employee(foreign_company, make_department(foreign_company, "SECRET-FOREIGN-DEPT"), "SECRET-FOREIGN-EMP")
    client.force_login(actor)
    return {"company":company, "actor":actor, "department":department, "employee":employee,
        "receiving_department":receiving_department, "receiving_employee":receiving_employee,
        "same_name_employee":same_name_employee, "inactive":inactive, "foreign":foreign, "custody":custody}


def test_transfer_choices_keep_existing_sets_and_attach_department_metadata_with_distinct_same_name_labels(client, transfer_choice_context):
    c = transfer_choice_context
    form = SupplyCustodyTransferForm(actor=c["actor"], company=c["company"], custody=c["custody"])
    assert set(form.fields["target_department"].queryset.values_list("pk", flat=True)) == {c["department"].pk, c["receiving_department"].pk}
    assert set(form.fields["target_employee"].queryset.values_list("pk", flat=True)) == {c["employee"].pk, c["receiving_employee"].pk, c["same_name_employee"].pk}
    assert form.fields["target_employee"].widget.attrs["data-department-field"] == "target_department"
    assert "data-choice-search" in form.fields["target_department"].widget.attrs
    assert form.fields["target_employee"].label_from_instance(c["receiving_employee"]) == "RECEIVER-01 · 同名接收员工 · RECEIVING / RECEIVING 部门"
    html = form["target_employee"].as_widget()
    assert f'data-department="{c["receiving_department"].pk}"' in html
    assert "RECEIVER-02 · 同名接收员工 · USE / USE 部门" in html
    assert "INACTIVE" not in html and "SECRET-FOREIGN" not in html
    page = client.get(reverse("supplies:custody-transfer", args=[c["custody"].pk]))
    assert page.status_code == 200
    assert "data-custody-transfer-review" in page.content.decode()
    assert "asset-choice-search.js" in page.content.decode() and "部门保管" in page.content.decode()
    assert page.context["form"]["quantity"].value() == Decimal("3.4567")


def test_transfer_receiving_lookup_stays_within_existing_manager_scope(client, transfer_choice_context):
    c = transfer_choice_context
    manager = make_user("transfer-choice-manager", "department_manager")
    UserDepartmentScope.objects.create(user=manager, company=c["company"], department=c["department"], include_descendants=False)
    form = SupplyCustodyTransferForm(actor=manager, company=c["company"], custody=c["custody"])
    assert list(form.fields["target_department"].queryset.values_list("pk", flat=True)) == [c["department"].pk]
    assert set(form.fields["target_employee"].queryset.values_list("pk", flat=True)) == {c["employee"].pk, c["same_name_employee"].pk}
    assert "RECEIVER-01" not in form["target_employee"].as_widget()
    client.force_login(manager)
    assert client.get(reverse("supplies:custody-transfer", args=[c["custody"].pk])).status_code == 200
    client.force_login(make_user("transfer-choice-hr", "hr"))
    assert client.get(reverse("supplies:custody-transfer", args=[c["custody"].pk])).status_code == 404


def test_transfer_native_validation_retains_quantity_reason_key_and_mismatch_without_business_writes(client, transfer_choice_context):
    c = transfer_choice_context
    url = reverse("supplies:custody-transfer", args=[c["custody"].pk])
    before = (list(SupplyCustody.objects.order_by("pk").values()), SupplyCustodyMovement.objects.count(),
        list(SupplyStockBalance.objects.order_by("pk").values()), SupplyStockLedger.objects.count())
    data = {"business_date":date(2026,8,27).isoformat(), "quantity":"1.2345", "reason":"保留本次原因",
        "idempotency_key":"transfer-choice-invalid-once", "target_department":str(c["receiving_department"].pk),
        "target_employee":str(c["same_name_employee"].pk)}
    page = client.post(url, data)
    assert page.status_code == 200 and page.context["form"].errors["target_employee"]
    for name in ("business_date","quantity","reason","idempotency_key","target_department","target_employee"):
        assert page.context["form"][name].value() == data[name]
    assert "目标员工必须属于目标责任部门" in page.content.decode()
    assert before == (list(SupplyCustody.objects.order_by("pk").values()), SupplyCustodyMovement.objects.count(),
        list(SupplyStockBalance.objects.order_by("pk").values()), SupplyStockLedger.objects.count())
