from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import AssetFinance
from apps.offboarding.services import (
    complete_clearance,
    initiate_clearance,
    return_clearance_item,
    transfer_clearance_item,
    upload_clearance_attachment,
)
from tests.test_sprint3_support import (
    JPEG_BYTES,
    PDF_BYTES,
    complete_initialization,
    make_company,
    make_user,
)
from tests.test_sprint10_support import (
    additional_employee,
    formal_asset,
    offboarding_context,
)
from django.core.files.uploadedfile import SimpleUploadedFile


pytestmark = pytest.mark.django_db


def _detail_url(clearance):
    return reverse("offboarding:clearance-detail", args=[clearance.pk])


def test_unauthenticated_clearance_urls_redirect_to_login(client):
    context = offboarding_context("S10HLOGIN")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HLOGIN-init",
    )
    urls = (
        reverse("offboarding:clearance-list"),
        reverse("offboarding:clearance-initiate"),
        _detail_url(clearance),
        reverse("offboarding:clearance-refresh", args=[clearance.pk]),
        reverse("offboarding:clearance-complete", args=[clearance.pk]),
    )
    for url in urls:
        response = client.get(url)
        assert response.status_code == 302
        assert response.url.startswith("/login/?next=")


def test_http_direct_view_and_action_matrix_for_hr_finance_management_employee_admin(client):
    context = offboarding_context("S10HMAT")
    asset, _ = formal_asset(context, "S10HMAT-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HMAT-init",
    )
    item = clearance.items.get(asset=asset)
    list_url = reverse("offboarding:clearance-list")
    detail_url = _detail_url(clearance)
    refresh_url = reverse("offboarding:clearance-refresh", args=[clearance.pk])
    complete_url = reverse("offboarding:clearance-complete", args=[clearance.pk])
    return_url = reverse(
        "offboarding:item-return", args=[clearance.pk, item.pk]
    )
    transfer_url = reverse(
        "offboarding:item-transfer", args=[clearance.pk, item.pk]
    )

    for actor in (
        context["hr"],
        context["finance"],
        context["management"],
        context["employee_user"],
    ):
        client.force_login(actor)
        assert client.get(list_url).status_code == 200
        assert client.get(detail_url).status_code == 200

    client.force_login(context["admin"])
    assert client.get(list_url).status_code == 403
    assert client.get(detail_url).status_code == 404

    client.force_login(context["hr"])
    assert client.get(refresh_url).status_code == 200
    assert client.get(complete_url).status_code == 200
    assert client.get(return_url).status_code == 403
    assert client.get(transfer_url).status_code == 403

    for actor in (
        context["finance"],
        context["management"],
        context["employee_user"],
    ):
        client.force_login(actor)
        assert client.get(refresh_url).status_code in {403, 404}
        assert client.get(complete_url).status_code in {403, 404}

    client.force_login(context["finance"])
    assert client.get(return_url).status_code == 200
    assert client.get(transfer_url).status_code == 200
    client.force_login(context["management"])
    assert client.get(return_url).status_code == 403
    assert client.get(transfer_url).status_code == 403


def test_warehouse_http_can_receive_pending_item_and_keeps_history_scope(client):
    context = offboarding_context("S10HWH")
    receiver = additional_employee(context, "S10HWH-R")
    warehouse_employee = additional_employee(
        context, "S10HWH-W", user=context["warehouse"]
    )
    asset, _ = formal_asset(context, "S10HWH-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HWH-init",
    )
    item = clearance.items.get()
    return_url = reverse("offboarding:item-return", args=[clearance.pk, item.pk])
    transfer_url = reverse(
        "offboarding:item-transfer", args=[clearance.pk, item.pk]
    )

    client.force_login(context["warehouse"])
    page = client.get(_detail_url(clearance))
    assert page.status_code == 200
    assert return_url in page.content.decode()
    assert client.get(return_url).status_code == 200
    assert client.get(transfer_url).status_code == 403
    response = client.post(
        return_url,
        {
            "returned_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "received_by_employee": warehouse_employee.pk,
            "return_department": receiver.department_id,
            "return_responsible_employee": receiver.pk,
            "return_location": context["location"].pk,
            "return_asset_status": "idle",
            "idempotency_key": "S10HWH-return",
            "remark": "仓库接收入库",
        },
    )
    assert response.status_code == 302
    assert response.url == _detail_url(clearance)
    item.refresh_from_db()
    assert item.resolution == "returned"
    assert client.get(response.url).status_code == 200


def test_warehouse_http_and_service_reject_other_company_item(client):
    context = offboarding_context("S10HWHX")
    asset, _ = formal_asset(context, "S10HWHX-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HWHX-init",
    )
    item = clearance.items.get(asset=asset)
    other = make_company("S10HWHX-OTHER", active=False)
    complete_initialization(other, context["admin"])
    context["company"].is_active = False
    context["company"].save(update_fields=["is_active", "updated_at"])
    other.is_active = True
    other.save(update_fields=["is_active", "updated_at"])

    client.force_login(context["warehouse"])
    assert client.get(_detail_url(clearance)).status_code == 404
    with pytest.raises(PermissionDenied, match="不属于当前公司"):
        return_clearance_item(
            actor=context["warehouse"],
            item=item,
            returned_at=timezone.now(),
            received_by_employee=context["employee"],
            return_department=context["department"],
            return_responsible_employee=context["employee"],
            return_location=context["location"],
            return_asset_status="idle",
            idempotency_key="S10HWHX-return",
        )

def test_hr_detail_is_nonfinancial_summary_and_has_no_lifecycle_execution_links(client):
    context = offboarding_context("S10HF1")
    asset, _ = formal_asset(
        context, "S10HF1-A", cost=Decimal("987654.32")
    )
    finance = AssetFinance.objects.get(asset=asset)
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HF1-init",
    )
    item = clearance.items.get()
    client.force_login(context["hr"])
    response = client.get(_detail_url(clearance))
    html = response.content.decode()
    assert response.status_code == 200
    assert item.asset_code_snapshot in html
    assert item.asset_name_snapshot in html
    assert item.original_department_snapshot in html
    assert item.original_employee_snapshot in html
    assert item.original_location_path_snapshot in html
    assert item.asset.department.name in html
    assert item.asset.responsible_employee.name in html
    assert "987654.32" not in html
    assert str(finance.original_cost) not in html
    for forbidden_label in (
        "原值",
        "账面净值",
        "累计折旧",
        "减值",
        "处置收入",
    ):
        assert forbidden_label not in html
    assert reverse(
        "offboarding:item-return", args=[clearance.pk, item.pk]
    ) not in html
    assert reverse(
        "offboarding:item-transfer", args=[clearance.pk, item.pk]
    ) not in html
    assert reverse("assets:disposal-start", args=[asset.pk]) not in html


def test_hr_http_initiate_then_second_confirmation_completion_sets_date(client):
    context = offboarding_context("S10HWF")
    client.force_login(context["hr"])
    initiate_url = reverse("offboarding:clearance-initiate")
    page = client.get(initiate_url, {"employee": context["employee"].pk})
    assert page.status_code == 200
    key = page.context["form"]["idempotency_key"].value()
    response = client.post(
        initiate_url,
        {
            "employee": context["employee"].pk,
            "idempotency_key": key,
            "remark": "HTTP 发起备注",
            "confirm": "on",
        },
    )
    clearance = context["employee"].asset_clearances.get()
    assert response.status_code == 302
    assert response.url == _detail_url(clearance)

    complete_url = reverse(
        "offboarding:clearance-complete", args=[clearance.pk]
    )
    first = client.post(
        complete_url,
        {"termination_date": timezone.localdate().isoformat()},
    )
    assert first.status_code == 200
    clearance.refresh_from_db()
    assert clearance.status == "open"
    second = client.post(
        complete_url,
        {
            "termination_date": timezone.localdate().isoformat(),
            "confirm": "on",
        },
    )
    assert second.status_code == 302
    clearance.refresh_from_db()
    context["employee"].refresh_from_db()
    assert clearance.status == "completed"
    assert context["employee"].termination_date == timezone.localdate()
    assert context["employee"].employment_status == "resigned"


def test_employee_edit_page_does_not_expose_editable_offboarding_fields_or_accept_forgery(client):
    context = offboarding_context("S10HEDIT")
    employee = context["employee"]
    client.force_login(context["hr"])
    edit_url = reverse("masterdata:employee-edit", args=[employee.pk])
    page = client.get(edit_url)
    html = page.content.decode()
    assert page.status_code == 200
    assert 'name="employment_status"' in html
    assert 'name="termination_date"' in html
    assert 'name="employment_status"' in html and "disabled" in html
    assert 'name="termination_date"' in html and "disabled" in html

    forged = client.post(
        edit_url,
        {
            "employee_no": employee.employee_no,
            "name": employee.name,
            "department": employee.department_id,
            "employment_status": "resigned",
            "hire_date": employee.hire_date.isoformat(),
            "termination_date": timezone.localdate().isoformat(),
            "mobile": employee.mobile,
            "remark": employee.remark,
        },
    )
    assert forged.status_code == 302
    employee.refresh_from_db()
    assert employee.employment_status == "active"
    assert employee.termination_date is None


def test_employee_detail_links_hr_to_initiate_and_then_to_active_clearance(client):
    context = offboarding_context("S10HENTRY")
    client.force_login(context["hr"])
    detail_url = reverse(
        "masterdata:employee-detail", args=[context["employee"].pk]
    )
    first = client.get(detail_url)
    initiate_url = (
        reverse("offboarding:clearance-initiate")
        + f"?employee={context['employee'].pk}"
    )
    assert first.status_code == 200
    assert initiate_url in first.content.decode()

    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HENTRY-init",
    )
    second = client.get(detail_url)
    assert _detail_url(clearance) in second.content.decode()


@pytest.mark.django_db(transaction=True)
def test_home_unresolved_count_uses_clearance_scope(client):
    context = offboarding_context("S10HOME")
    formal_asset(context, "S10HOME-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HOME-init",
    )
    assert clearance.unresolved_assets == 1
    for actor, expected in (
        (context["hr"], 1),
        (context["finance"], 1),
        (context["employee_user"], 1),
        (context["admin"], 0),
    ):
        client.force_login(actor)
        page = client.get(reverse("home"))
        assert page.status_code == 200
        assert page.context["offboarding_unresolved_count"] == expected


@override_settings(MEDIA_ROOT="var/test-sprint10-http-download")
def test_http_attachment_download_is_permission_checked_private_no_store(client):
    context = offboarding_context("S10HDL")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10HDL-init",
    )
    a0 = upload_clearance_attachment(
        actor=context["hr"],
        target=clearance,
        uploaded_file=SimpleUploadedFile(
            "ordinary.jpg", JPEG_BYTES, content_type="image/jpeg"
        ),
        security_class="A0",
    )
    a1 = upload_clearance_attachment(
        actor=context["finance"],
        target=clearance,
        uploaded_file=SimpleUploadedFile(
            "finance.pdf", PDF_BYTES, content_type="application/pdf"
        ),
        security_class="A1",
    )
    a0_url = reverse(
        "offboarding:attachment-download", args=[clearance.pk, a0.pk]
    )
    a1_url = reverse(
        "offboarding:attachment-download", args=[clearance.pk, a1.pk]
    )
    client.force_login(context["employee_user"])
    ordinary = client.get(a0_url)
    assert ordinary.status_code == 200
    assert ordinary["Cache-Control"] == "private, no-store"
    assert ordinary["X-Content-Type-Options"] == "nosniff"
    assert ordinary["Content-Disposition"].startswith("attachment;")
    assert client.get(a1_url).status_code == 403

    client.force_login(context["finance"])
    financial = client.get(a1_url)
    assert financial.status_code == 200
    assert financial["Cache-Control"] == "private, no-store"
    client.force_login(context["admin"])
    assert client.get(a0_url).status_code == 404


def test_nullable_historical_actor_renders_safely(client):
    context = offboarding_context("S10HNULL")
    historical_hr = make_user("s10hnull-history", "hr")
    clearance = initiate_clearance(
        actor=historical_hr,
        employee=context["employee"],
        idempotency_key="S10HNULL-init",
    )
    clearance = complete_clearance(
        actor=historical_hr,
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    historical_hr.delete()
    client.force_login(context["hr"])
    page = client.get(_detail_url(clearance))
    assert page.status_code == 200
    assert "历史账号" in page.content.decode()
