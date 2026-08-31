from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_forms import AssetLoanForm, AssetTransferForm
from apps.offboarding.services import initiate_clearance
from tests.test_sprint3_support import (
    complete_initialization,
    make_company,
    make_department,
    make_employee,
    make_user,
)
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db


def test_asset_transfer_and_loan_forms_prefill_safe_current_values():
    context, asset, _qr = active_asset_context("UXSMALL")

    transfer = AssetTransferForm(
        actor=context["finance"], asset=asset, action="transfer"
    )
    assert transfer.initial["to_department"] == asset.department_id
    assert (
        transfer.initial["to_responsible_employee"]
        == asset.responsible_employee_id
    )
    assert transfer.initial["to_location"] == asset.location_id
    assert "仅在需要调拨时修改" in transfer.fields["to_department"].help_text

    loan = AssetLoanForm(actor=context["finance"], asset=asset, action="loan")
    assert loan.initial["borrower_type"] == "internal_employee"
    assert loan.initial["loan_date"] == timezone.localdate()


def test_offboarding_list_is_paginated_and_keeps_total_count(client):
    company = make_company("UXPAGE")
    admin = make_user("ux-page-admin", "system_admin")
    hr = make_user("ux-page-hr", "hr")
    complete_initialization(company, admin)
    department = make_department(company, "UXPAGE-D")
    for index in range(26):
        employee = make_employee(
            company,
            department,
            f"UXPAGE-{index:02d}",
        )
        initiate_clearance(
            actor=hr,
            employee=employee,
            idempotency_key=f"ux-page-{index:02d}",
            remark="易用性分页测试",
        )
    client.force_login(hr)

    first = client.get(reverse("offboarding:clearance-list"))
    second = client.get(reverse("offboarding:clearance-list"), {"page": 2})

    assert first.status_code == second.status_code == 200
    assert len(first.context["page_obj"]) == 25
    assert len(second.context["page_obj"]) == 1
    assert "共 26 条".encode() in first.content
    assert "第 2 / 2 页".encode() in second.content
