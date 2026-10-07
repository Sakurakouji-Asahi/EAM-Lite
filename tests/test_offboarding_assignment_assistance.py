"""Clearance filling hints keep the existing action validation and scope."""
import pytest
from urllib.parse import urlencode
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset, AssetMovement
from apps.offboarding.models import EmployeeAssetClearance, EmployeeAssetClearanceItem
from apps.offboarding.services import initiate_clearance
from tests.test_sprint3_support import make_department, make_employee
from tests.test_sprint10_support import (
    active_internal_loan, additional_employee, formal_asset, offboarding_context,
)

pytestmark = pytest.mark.django_db


def _state(asset, clearance):
    return (
        list(Asset._base_manager.filter(pk=asset.pk).values()),
        list(EmployeeAssetClearance._base_manager.filter(pk=clearance.pk).values()),
        list(EmployeeAssetClearanceItem._base_manager.filter(clearance=clearance).values()),
        AssetMovement.objects.filter(asset=asset).count(),
    )


@pytest.mark.parametrize("action", ["return", "transfer"])
def test_clearance_assignment_hints_preserve_bound_validation(client, action):
    context = offboarding_context(f"OFA-{action}")
    asset, _ = formal_asset(context, f"OFA-{action}")
    receiver = additional_employee(context, f"OFA-{action}-RECV")
    other_department = make_department(context["company"], f"OFA-{action}-OTHER-D")
    other_employee = make_employee(context["company"], other_department, f"OFA-{action}-OTHER")
    clearance = initiate_clearance(actor=context["hr"], employee=context["employee"],
                                   idempotency_key=f"OFA-{action}-start")
    item = clearance.items.get()
    url = reverse(f"offboarding:item-{action}", args=[clearance.pk, item.pk])
    original = reverse("offboarding:clearance-detail", args=[clearance.pk]) + "?q=OFA&resolution=pending&view=compact"
    client.force_login(context["equipment"])
    baseline = _state(asset, clearance)
    page = client.get(url, {"return_to": original})
    assert page.status_code == 200
    assert page.context["cancel_url"] == original
    assert "本次清退项目" in page.content.decode()
    assert asset.asset_name in page.content.decode()
    assert context["employee"].employee_no in page.content.decode()
    field_prefix = "return" if action == "return" else "to"
    responsible_field = f"{field_prefix}_responsible_employee"
    form = page.context["form"]
    assert form.fields[responsible_field].widget.attrs["data-department-field"] == f"{field_prefix}_department"
    assert f'data-department="{other_department.pk}"' in str(form[responsible_field])
    assert context["location"].name in str(form[f"{field_prefix}_location"])
    payload = {
        "idempotency_key": form.initial["idempotency_key"],
        f"{field_prefix}_department": str(receiver.department_id),
        responsible_field: str(other_employee.pk),
        f"{field_prefix}_location": str(context["location"].pk),
        "remark": "保留这次输入",
    }
    if action == "return":
        payload.update(returned_at=timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                       received_by_employee=str(receiver.pk), return_asset_status="idle")
        assert "data-department-field" not in form.fields["received_by_employee"].widget.attrs
    else:
        payload.update(effective_at=timezone.localtime().strftime("%Y-%m-%dT%H:%M"), reason="清退转交")
    rejected = client.post(url + "?" + urlencode({"return_to": original}), payload)
    assert rejected.status_code == 200
    assert "必须属于" in str(rejected.context["form"].errors[responsible_field])
    assert rejected.context["form"]["remark"].value() == "保留这次输入"
    assert rejected.context["item_context"]["asset"].pk == asset.pk
    assert _state(asset, clearance) == baseline
    client.force_login(context["hr"])
    assert client.get(url).status_code == 403


def test_loan_clearance_context_distinguishes_borrower_from_current_owner(client):
    context = offboarding_context("OFA-LOAN")
    owner = additional_employee(context, "OFA-LOAN-OWNER")
    asset, _ = formal_asset(context, "OFA-LOAN-A", employee=owner)
    loan = active_internal_loan(context, asset, context["employee"], "OFA-LOAN")
    clearance = initiate_clearance(actor=context["hr"], employee=context["employee"],
                                   idempotency_key="OFA-LOAN-start")
    item = clearance.items.get()
    client.force_login(context["equipment"])
    baseline = _state(asset, clearance)
    response = client.get(reverse("offboarding:item-return", args=[clearance.pk, item.pk]))
    assert response.status_code == 200
    display = response.context["item_context"]
    assert display["loan"].pk == loan.pk
    assert display["employee"].pk == context["employee"].pk
    assert display["asset"].responsible_employee_id == owner.pk
    html = response.content.decode()
    assert "内部借用人是离职人员" in html
    assert owner.employee_no in html and context["employee"].employee_no in html
    assert loan.expected_return_date.isoformat() in html
    assert _state(asset, clearance) == baseline
