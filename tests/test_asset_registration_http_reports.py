import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.assets.models import Asset, AssetRegistration, AttachmentLink
from apps.finance.models import AssetFinance, DepreciationEntry
from apps.reports.queries import build_dashboard, build_report_dataset, build_tplus_dataset
from tests.test_asset_registration_finance_separation import context, registered, activate
from tests.test_sprint3_support import make_user, pdf_upload
from apps.offboarding.services import initiate_clearance


pytestmark = pytest.mark.django_db(transaction=True)


def browser_data(context, **extra):
    return {
        "asset_name": "无照片先建档", "category": str(context["category"].pk),
        "department": str(context["department"].pk), "responsible_employee": str(context["employee"].pk),
        "location": str(context["location"].pk), "unit": "台", "quantity": "1",
        "asset_action": "register", "idempotency_key": "browser-create",
        **extra,
    }


def photo_upload():
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), color="blue").save(stream, format="PNG")
    return SimpleUploadedFile("later-photo.png", stream.getvalue(), content_type="image/png")


def test_browser_create_without_photo_is_immediate_and_idempotent(client, context):
    client.force_login(context["equipment"])
    page = client.get(reverse("assets:asset-create"))
    assert page.status_code == 200
    assert b'name="idempotency_key"' in page.content
    assert all(page.context["form"].fields[name].required for name in ("unit", "department", "responsible_employee", "location"))
    assert "无需财务复核" in page.content.decode()
    payload = browser_data(context)
    first = client.post(reverse("assets:asset-create"), payload)
    assert first.status_code == 302
    asset = Asset.objects.get(asset_name=payload["asset_name"])
    assert asset.asset_code and asset.asset_status == "pending_label"
    assert not AssetFinance.objects.exists()
    assert client.post(reverse("assets:asset-create"), payload).status_code == 302
    assert Asset.objects.count() == AssetRegistration.objects.count() == 1
    detail = client.get(first.url)
    assert detail.status_code == 200
    assert "补拍照片" in detail.content.decode()
    assert "提交财务确认" not in detail.content.decode()


def test_optional_draft_remains_available_without_complete_assignment(client, context):
    client.force_login(context["equipment"])
    data = browser_data(context, asset_action="draft", department="", responsible_employee="", location="")
    assert client.post(reverse("assets:asset-create"), data).status_code == 302
    asset = Asset.objects.get()
    assert asset.asset_status == "draft" and asset.asset_code is None


def test_registration_form_rejects_missing_request_key_and_finance_field_injection(client, context):
    client.force_login(context["equipment"])
    response = client.post(reverse("assets:asset-create"), browser_data(context, idempotency_key=""))
    assert response.status_code == 200 and response.context["form"].errors
    assert not Asset.objects.exists()
    response = client.post(reverse("assets:asset-create"), browser_data(context, original_cost="500.00"))
    assert response.status_code in {200, 403}
    assert not Asset.objects.exists()


def test_photo_capture_and_late_invoice_use_existing_access_controls(client, context):
    asset = registered(context)
    activate(context, asset)
    client.force_login(context["equipment"])
    url = reverse("assets:attachment-upload", args=[asset.pk])
    page = client.get(url + "?role=photo")
    assert page.status_code == 200 and b'capture="environment"' in page.content
    response = client.post(url + "?role=photo", {
        "role": "photo", "security_class": "A0", "file": photo_upload(),
    })
    assert response.status_code == 302
    client.force_login(context["finance"])
    page = client.get(url + "?role=invoice")
    assert page.context["form"].initial == {"role": "invoice", "security_class": "A1"}
    assert client.post(url + "?role=invoice", {
        "role": "invoice", "security_class": "A1", "file": pdf_upload(),
    }).status_code == 302
    invoice = AttachmentLink.objects.get(asset=asset, role="invoice")
    client.force_login(context["equipment"])
    download = reverse("assets:attachment-download", args=[asset.pk, invoice.pk])
    assert client.get(download).status_code == 403
    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert invoice.attachment.safe_filename not in detail.content.decode()
    asset.refresh_from_db()
    assert asset.asset_status == "in_use"
    assert not AssetFinance.objects.filter(asset=asset).exists()


def test_finance_workbench_lists_registered_assets_and_keeps_identity(client, context):
    asset = registered(context)
    activate(context, asset)
    original_code, qr_id = asset.asset_code, asset.qr_identities.get(status="active").pk
    url = reverse("finance:finance-confirm", args=[asset.pk])
    client.force_login(context["equipment"])
    assert client.get(url).status_code == 403
    client.force_login(context["finance"])
    listing = client.get(reverse("finance:pending-list"))
    assert asset.pk in [item.pk for item in listing.context["assets"]]
    page = client.get(url)
    assert page.status_code == 200
    assert page.context["form"].fields["code_effective_date"].widget.is_hidden
    for name in ("salvage_mode", "method", "posting_period", "start_rule", "stop_rule"):
        assert list(page.context["form"].fields[name].choices)[0] == ("", "使用政策默认")
    assert "确认并生成正式编号" not in page.content.decode()
    assert "confirm_permanent_code" in page.context["form"].fields
    data = {
        "action": "confirm", "accounting_treatment": "controlled_non_fixed",
        "original_cost": "1200.00", "action_reason": "发票资料已核对",
        "idempotency_key": "finance-from-browser", "confirm_permanent_code": "on",
    }
    response = client.post(url, data)
    assert response.status_code == 302, getattr(response, "context", None) and response.context["form"].errors
    asset.refresh_from_db()
    assert asset.asset_status == "in_use" and asset.asset_code == original_code
    assert asset.qr_identities.get(status="active").pk == qr_id
    assert asset.finance.original_cost == Decimal("1200.00")
    assert asset.finance.finance_confirmed_at is not None
    assert not DepreciationEntry.objects.exists()


def test_unconfirmed_asset_stays_in_physical_reports_but_not_tplus_values(context):
    asset = registered(context)
    ledger = build_report_dataset(actor=context["equipment"], company=context["company"], report_key="asset_ledger")
    assert [row["asset_code"] for row in ledger.rows] == [asset.asset_code]
    assert ledger.rows[0]["asset_status"] == "待贴标"
    dashboard = build_dashboard(actor=context["finance"], company=context["company"])
    assert dashboard["pending"]["pending_finance"] == 1
    month = timezone.localdate().replace(day=1)
    end = (month + timedelta(days=32)).replace(day=1)
    tplus = build_tplus_dataset(actor=context["finance"], company=context["company"], period_start=month, period_end=end)
    assert not tplus.asset_rows
    assert any("尚未确认" in warning for warning in tplus.warnings)


def test_unconfirmed_physical_asset_is_included_in_employee_clearance(context):
    asset = registered(context)
    hr = make_user("registration-hr", "hr")
    clearance = initiate_clearance(
        actor=hr, employee=context["employee"], idempotency_key="leave-before-invoice",
    )
    assert clearance.items.filter(asset=asset).exists()
    assert not AssetFinance.objects.filter(asset=asset).exists()
