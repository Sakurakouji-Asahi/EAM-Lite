"""Cards preserve existing asset/field scope and never mutate QR printing state."""
import pytest
from django.urls import reverse
from apps.assets.models import Asset, AssetLabelPrintBatch
from apps.assets.services import create_asset_draft
from tests.test_sprint3_support import make_department, make_user
from tests.test_sprint7_support import active_asset_context, add_department_manager

pytestmark = pytest.mark.django_db


def test_information_card_is_read_only_and_keeps_qr_identity_and_print_state(client):
    context, asset, qr = active_asset_context("CARD-PHYSICAL")
    client.force_login(context["equipment"])
    original = Asset._base_manager.filter(pk=asset.pk).values().get()
    original_qr = asset.qr_identities.values().get(pk=qr.pk)
    batches, movements = AssetLabelPrintBatch.objects.count(), asset.movements.count()
    url = reverse("assets:asset-information-card", args=[asset.pk])
    page = client.get(url)
    assert page.status_code == 200 and page.context["card_show_qr"]
    assert reverse("assets:asset-current-qr", args=[asset.pk]) in page.content.decode()
    assert "当前责任与位置" in page.content.decode() and context["employee"].name in page.content.decode()
    assert "1234.56" not in page.content.decode() and "折旧" not in page.content.decode()
    assert "no-store" in page["Cache-Control"] and page["Referrer-Policy"] == "no-referrer"
    assert client.post(url).status_code == 405
    assert Asset._base_manager.filter(pk=asset.pk).values().get() == original
    assert asset.qr_identities.values().get(pk=qr.pk) == original_qr
    assert AssetLabelPrintBatch.objects.count() == batches and asset.movements.count() == movements
    detail = client.get(reverse("assets:asset-detail", args=[asset.pk])).content.decode()
    assert "打印资料卡" in detail and url in detail


def test_summary_reader_and_outside_scope_do_not_gain_physical_or_qr_access(client):
    context, asset, qr = active_asset_context("CARD-PRIVATE")
    client.force_login(make_user("card-summary-hr", "hr"))
    page = client.get(reverse("assets:asset-information-card", args=[asset.pk]))
    assert page.status_code == 200 and not page.context["card_can_p1"] and not page.context["card_show_qr"]
    assert page.context["physical_rows"] == [] and context["employee"].name in page.content.decode()
    assert "实物资料" not in page.content.decode()
    assert reverse("assets:asset-current-qr", args=[asset.pk]) not in page.content.decode()
    outside = make_department(context["company"], "CARD-OUTSIDE-D")
    client.force_login(add_department_manager(context, "CARD-OUTSIDE", outside))
    assert client.get(reverse("assets:asset-information-card", args=[asset.pk])).status_code == 404


def test_draft_card_keeps_long_names_and_displays_no_qr_without_formal_identity(client):
    context, formal, qr = active_asset_context("CARD-DRAFT")
    name = "PRINT-DEMO · " + "长名称与设备用途核对" * 12 + ' <原值> "设备"'
    draft = create_asset_draft(actor=context["equipment"], company=context["company"], data={
        "asset_name": name, "category": context["category"], "unit": "台",
        "description": "用途说明\n下一行说明", "notes": "现场核对备注"},
        idempotency_key="card-long-draft")
    client.force_login(context["equipment"])
    page = client.get(reverse("assets:asset-information-card", args=[draft.pk]))
    assert page.status_code == 200 and page.context["asset"].asset_name == name
    assert "&lt;原值&gt;" in page.content.decode() and "临时草稿号" in page.content.decode()
    assert "未生成二维码" in page.content.decode() and not page.context["current_qr"]
    assert not page.context["card_show_qr"] and draft.draft_number in page.content.decode()
    draft.refresh_from_db()
    assert draft.asset_status == "draft" and not draft.asset_code and not draft.qr_identities.exists()
