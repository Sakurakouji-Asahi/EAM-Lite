from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import (
    archive_asset,
    initiate_disposal,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
    complete_disposal,
)
from apps.assets.models import AssetDisposal, AssetMovement, AttachmentLink
from apps.audit.models import AuditLog
from tests.test_sprint3_support import direct_attachment, make_user
from tests.test_sprint7_support import active_asset_context, add_department_manager


pytestmark = pytest.mark.django_db


def _open_disposal(context, asset, key, *, disposal_type="scrap"):
    today = timezone.localdate()
    return initiate_disposal(
        actor=context["equipment"], asset=asset, disposal_type=disposal_type,
        application_date=today, planned_disposal_date=today + timedelta(days=1),
        reason="HTTP 处置验收", recipient_name=(
            "回收客户" if disposal_type != "scrap" else ""
        ), idempotency_key=key, expected_status=asset.asset_status,
    )


def test_unauthenticated_lifecycle_and_disposal_urls_redirect_to_login(client):
    context, asset, _qr = active_asset_context("S7HTTPLOGIN")
    disposal = _open_disposal(context, asset, "S7HTTPLOGIN-start")
    urls = (
        reverse("assets:lifecycle-transfer", args=[asset.pk]),
        reverse("assets:lifecycle-code-correct", args=[asset.pk]),
        reverse("assets:disposal-detail", args=[disposal.pk]),
        reverse("assets:disposal-finance-lock", args=[disposal.pk]),
    )
    for url in urls:
        response = client.get(url)
        assert response.status_code == 302
        assert response.url.startswith("/login/?next=")


def test_direct_urls_enforce_action_role_and_object_scope(client):
    context, asset, _qr = active_asset_context("S7HTTPDENY")
    manager = add_department_manager(
        context, "S7HTTPDENY", context["department"]
    )
    outsider = make_user("s7-http-outsider", "employee")

    client.force_login(context["equipment"])
    assert client.get(
        reverse("assets:lifecycle-code-correct", args=[asset.pk])
    ).status_code == 403
    client.force_login(manager)
    assert client.get(
        reverse("assets:lifecycle-archive", args=[asset.pk])
    ).status_code == 403
    client.force_login(outsider)
    assert client.get(
        reverse("assets:lifecycle-transfer", args=[asset.pk])
    ).status_code == 404


def test_hr_summary_never_exposes_movement_loan_or_disposal_records(client):
    context, asset, _qr = active_asset_context("S7HTTPHR")
    disposal = _open_disposal(context, asset, "S7HTTPHR-start")
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/disposals/S7HTTPHR.jpg", filename="HR-HIDDEN.jpg",
    )
    link = AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        asset_disposal=disposal, role="disposal", security_class="A0",
        created_by=context["equipment"],
    )
    hr = make_user("s7-http-hr", "hr")
    client.force_login(hr)

    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    text = detail.content.decode()
    assert detail.status_code == 200
    assert asset.department.name in text
    assert "生命周期与处置" not in text
    assert "HTTP 处置验收" not in text
    assert client.get(
        reverse("assets:disposal-detail", args=[disposal.pk])
    ).status_code == 404
    assert client.get(
        reverse(
            "assets:disposal-attachment-download",
            args=[disposal.pk, link.pk],
        )
    ).status_code == 404


def test_chinese_transfer_form_post_updates_asset_and_redirects(client):
    context, asset, _qr = active_asset_context("S7HTTPMOVE")
    from tests.test_sprint7_support import add_target_assignment

    department, employee, location = add_target_assignment(
        context, "S7HTTPMOVE"
    )
    client.force_login(context["equipment"])
    response = client.post(
        reverse("assets:lifecycle-transfer", args=[asset.pk]),
        {
            "to_department": department.pk,
            "to_responsible_employee": employee.pk,
            "to_location": location.pk,
            "effective_at": (
                timezone.localtime() - timedelta(minutes=1)
            ).strftime("%Y-%m-%dT%H:%M"),
            "reason": "生产线调整",
            "remark": "中文表单验收",
            "idempotency_key": "S7HTTPMOVE-key",
            "expected_status": "in_use",
            "expected_department_id": asset.department_id,
            "expected_responsible_employee_id": asset.responsible_employee_id,
            "expected_location_id": asset.location_id,
        },
    )
    assert response.status_code == 302
    assert response.url == reverse("assets:asset-detail", args=[asset.pk])
    asset.refresh_from_db()
    assert asset.department_id == department.pk
    assert AssetMovement.objects.filter(
        asset=asset, movement_type="transfer", reason="生产线调整"
    ).count() == 1


@pytest.mark.parametrize(
    "url_name", ("lifecycle-idle", "lifecycle-activate")
)
def test_http_hidden_expected_status_forgery_cannot_escape_under_repair(
    client, url_name
):
    context, asset, _qr = active_asset_context(f"S7HTTPFORGE{url_name}")
    from apps.assets.lifecycle_services import send_asset_for_repair

    send_asset_for_repair(
        actor=context["equipment"], asset=asset,
        effective_at=timezone.now() - timedelta(seconds=1),
        reason="建立维修中前置状态", idempotency_key=f"{url_name}-repair",
        expected_status="in_use",
    )
    asset.refresh_from_db()
    before_movements = AssetMovement.objects.filter(asset=asset).count()
    client.force_login(context["equipment"])
    before_audits = AuditLog.objects.count()
    response = client.post(
        reverse(f"assets:{url_name}", args=[asset.pk]),
        {
            "effective_at": (
                timezone.localtime() - timedelta(minutes=1)
            ).strftime("%Y-%m-%dT%H:%M"),
            "reason": "伪造 expected_status",
            "remark": "",
            "idempotency_key": f"{url_name}-forged",
            "expected_status": "under_repair",
        },
    )
    asset.refresh_from_db()
    assert response.status_code == 200
    assert asset.asset_status == "under_repair"
    assert AssetMovement.objects.filter(asset=asset).count() == before_movements
    assert AuditLog.objects.count() == before_audits


def test_disposal_detail_hides_financial_snapshot_and_a1_evidence(client):
    context, asset, _qr = active_asset_context("S7HTTPF1")
    disposal = _open_disposal(
        context, asset, "S7HTTPF1-start", disposal_type="sale"
    )
    disposal = record_disposal_actual_details(
        actor=context["equipment"], disposal=disposal,
        actual_disposal_date=timezone.localdate(), recipient_name="回收客户",
        handled_by=context["equipment"], idempotency_key="S7HTTPF1-actual",
    )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"], disposal=disposal,
        disposal_income="123.45", idempotency_key="S7HTTPF1-lock",
    )
    attachment = direct_attachment(
        context["company"], context["finance"],
        key="private/disposals/S7HTTPF1.pdf",
        filename="S7HTTPF1-A1-SECRET.pdf", mime="application/pdf",
        data=b"%PDF-1.7\n",
    )
    link = AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        asset_disposal=disposal, role="disposal", security_class="A1",
        created_by=context["finance"],
    )
    detail_url = reverse("assets:disposal-detail", args=[disposal.pk])
    download_url = reverse(
        "assets:disposal-attachment-download", args=[disposal.pk, link.pk]
    )

    for viewer in (context["equipment"], context["admin"]):
        client.force_login(viewer)
        response = client.get(detail_url)
        text = response.content.decode()
        assert response.status_code == 200
        assert "1234.56" not in text and "123.45" not in text
        assert "S7HTTPF1-A1-SECRET" not in text
        assert client.get(download_url).status_code == 403

    client.force_login(context["finance"])
    text = client.get(detail_url).content.decode()
    assert "1234.56" in text and "123.45" in text
    assert "S7HTTPF1-A1-SECRET" in text


def test_disposal_actual_form_shows_chinese_field_error_without_mutation(client):
    context, asset, _qr = active_asset_context("S7HTTPERR")
    disposal = _open_disposal(context, asset, "S7HTTPERR-start")
    client.force_login(context["equipment"])
    response = client.post(
        reverse("assets:disposal-actual", args=[disposal.pk]),
        {
            "actual_disposal_date": (
                timezone.localdate() + timedelta(days=1)
            ).isoformat(),
            "recipient_name": "",
            "expected_status": "pending_disposal",
            "idempotency_key": "S7HTTPERR-actual",
        },
    )
    disposal.refresh_from_db()
    assert response.status_code == 200
    assert "实际日期不得早于申请日或晚于当前业务日" in response.content.decode()
    assert disposal.actual_disposal_date is None


def test_archived_qr_is_read_only_and_restore_keeps_terminal_status(client):
    context, asset, qr = active_asset_context("S7HTTPARCH")
    disposal = _open_disposal(context, asset, "S7HTTPARCH-start")
    disposal = record_disposal_actual_details(
        actor=context["equipment"], disposal=disposal,
        actual_disposal_date=timezone.localdate(),
        handled_by=context["equipment"], idempotency_key="S7HTTPARCH-actual",
    )
    disposal = lock_disposal_financial_snapshot(
        actor=context["finance"], disposal=disposal,
        disposal_income="0.00", idempotency_key="S7HTTPARCH-lock",
    )
    attachment = direct_attachment(
        context["company"], context["equipment"],
        key="private/disposals/S7HTTPARCH.jpg",
        filename="S7HTTPARCH.jpg",
    )
    AttachmentLink.objects.create(
        company=context["company"], attachment=attachment,
        asset_disposal=disposal, role="disposal", security_class="A0",
        created_by=context["equipment"],
    )
    complete_disposal(
        actor=context["equipment"], disposal=disposal,
        idempotency_key="S7HTTPARCH-complete",
    )
    asset.refresh_from_db()
    archived = archive_asset(
        actor=context["admin"], asset=asset, reason="HTTP 归档验收",
        idempotency_key="S7HTTPARCH-archive",
    )
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:qr-scan", args=[qr.public_token]))
    text = response.content.decode()
    assert response.status_code == 200
    assert "此资产已归档" in text
    assert "换标并轮换" not in text
    assert "近期业务摘要" not in text
    assert archived.asset_status == "disposed"
