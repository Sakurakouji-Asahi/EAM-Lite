from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import Client, override_settings
from django.urls import reverse

from apps.assets.models import Asset, AssetMovement, AssetQrIdentity
from apps.assets.qr_services import (
    confirm_label_attachment,
    confirm_print_batch,
    generate_print_batch,
    rotate_qr_identity,
)
from apps.audit.models import AuditLog
from tests.test_sprint3_support import make_user
from tests.test_sprint6_support import formal_asset_context


pytestmark = pytest.mark.django_db


def _print(context, asset, key):
    batch = generate_print_batch(
        actor=context["finance"], assets=[asset], idempotency_key=key
    )
    confirm_print_batch(actor=context["finance"], batch=batch)
    return batch


def _attach(context, asset, qr_identity, key, target="in_use"):
    _print(context, asset, f"{key}-print")
    qr_identity.refresh_from_db()
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr_identity.public_token,
        target_status=target,
        idempotency_key=f"{key}-attach",
    )
    asset.refresh_from_db()
    qr_identity.refresh_from_db()


def test_unauthenticated_scan_redirects_to_login_with_security_headers(client):
    _context, _asset, qr_identity = formal_asset_context("S6LOGIN")
    response = client.get(reverse("assets:qr-scan", args=[qr_identity.public_token]))

    assert response.status_code == 302
    assert response.url.startswith("/login/?next=")
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_unknown_scan_is_generic_404_and_does_not_echo_token(client):
    context, _asset, _qr = formal_asset_context("S6UNKNOWN")
    client.force_login(context["equipment"])
    token = "unknown-opaque-token-12345678901234567890"
    response = client.get(reverse("assets:qr-scan", args=[token]))

    assert response.status_code == 404
    assert token.encode() not in response.content
    assert "二维码无效" in response.content.decode()
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.django_db(transaction=True)
def test_revoked_scan_is_generic_410_without_asset_details(client):
    context, asset, old = formal_asset_context("S6REVOKED")
    _attach(context, asset, old, "S6REVOKED")
    rotate_qr_identity(
        actor=context["finance"], asset=asset, reason="测试旧标签失效"
    )
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:qr-scan", args=[old.public_token]))
    text = response.content.decode()

    assert response.status_code == 410
    assert "此标签已失效" in text
    assert old.public_token not in text
    assert asset.asset_name not in text
    assert asset.asset_code not in text
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_logged_in_user_without_object_scope_gets_generic_403_and_audited_denial(client):
    context, asset, qr_identity = formal_asset_context("S6FORBID")
    outsider = make_user("s6-forbidden-employee", "employee")
    client.force_login(outsider)
    response = client.get(
        reverse("assets:qr-scan", args=[qr_identity.public_token]),
        HTTP_USER_AGENT=(
            f"scanner /assets/scan/{qr_identity.public_token}/ token={qr_identity.public_token}"
        ),
    )
    text = response.content.decode()

    assert response.status_code == 403
    assert "无权查看" in text
    assert asset.asset_name not in text
    assert asset.asset_code not in text
    assert qr_identity.public_token not in text
    denied = AuditLog.objects.get(action="asset_qr.scan_denied")
    assert qr_identity.public_token not in denied.user_agent
    assert "[REDACTED]" in denied.user_agent


def test_equipment_scan_shows_physical_summary_but_not_financial_amount(client):
    context, asset, qr_identity = formal_asset_context(
        "S6NOFIN", cost=Decimal("4321.98")
    )
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:qr-scan", args=[qr_identity.public_token]))
    text = response.content.decode()

    assert response.status_code == 200
    assert asset.asset_name in text
    assert asset.asset_code in text
    assert asset.department.name in text
    assert asset.responsible_employee.name in text
    assert "4321.98" not in text
    assert "财务只读摘要" not in text
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_print_view_is_local_a4_snapshot_and_browser_get_does_not_confirm(client):
    context, asset, qr_identity = formal_asset_context("S6A4")
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key="S6A4-batch",
    )
    item = batch.items.get()
    client.force_login(context["finance"])
    response = client.get(reverse("assets:label-batch-print", args=[batch.pk]))
    text = response.content.decode()

    assert response.status_code == 200
    assert context["company"].short_name in text
    assert asset.asset_name in text
    assert asset.asset_code in text
    assert asset.department.name in text
    assert "原值" not in text and "账面净值" not in text
    assert "cdn" not in text.casefold()
    assert "fonts.googleapis" not in text.casefold()
    qr_url = reverse("assets:label-item-qr", args=[item.pk])
    assert f'href="{qr_url}"' in text
    assert f'src="{qr_url}"' in text
    assert 'target="_blank"' in text and 'rel="noopener"' in text
    assert "手机先连接与电脑相同的 Wi-Fi" in text
    assert "微信、支付宝等内置扫码可能会拦截局域网地址" in text
    assert "将鼠标移到二维码上即可放大" in text
    batch.refresh_from_db()
    qr_identity.refresh_from_db()
    assert batch.status == "generated" and batch.printed_at is None
    assert qr_identity.label_status == "ready_to_print"


@override_settings(QR_BASE_URL_IS_DURABLE=False)
def test_print_view_marks_machine_bound_qr_labels_as_temporary(client):
    context, asset, _qr_identity = formal_asset_context("S6TEMPQR")
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key="S6TEMPQR-batch",
    )
    client.force_login(context["finance"])

    response = client.get(reverse("assets:label-batch-print", args=[batch.pk]))
    text = response.content.decode()

    assert response.status_code == 200
    assert "本地验收标签" in text
    assert "不能作为迁移到其他电脑后的正式长期标签" in text
    assert "本地验收 · 部署后重印" in text


def test_qr_svg_endpoint_is_authenticated_scoped_and_noncacheable(client):
    context, asset, _qr = formal_asset_context("S6SVG")
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key="S6SVG-batch",
    )
    item = batch.items.get()
    url = reverse("assets:label-item-qr", args=[item.pk])

    anonymous = client.get(url)
    assert anonymous.status_code == 302
    client.force_login(context["finance"])
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert b"<svg" in response.content


def test_asset_current_qr_is_visible_to_label_roles_and_protected(client):
    context, asset, _qr = formal_asset_context("S6ASSETQR")
    url = reverse("assets:asset-current-qr", args=[asset.pk])

    anonymous = client.get(url)
    assert anonymous.status_code == 302

    client.force_login(context["admin"])
    forbidden = client.get(url)
    assert forbidden.status_code == 403

    client.force_login(context["finance"])
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert b"<svg" in response.content

    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    text = detail.content.decode()
    assert detail.status_code == 200
    assert url in text
    assert "查看二维码" in text
    assert "打印二维码" in text
    assert "Web 确认贴标" not in text


def test_asset_direct_print_creates_a_single_item_controlled_batch(client):
    context, asset, _qr = formal_asset_context("S6DIRECTPRINT")
    client.force_login(context["finance"])

    response = client.post(
        reverse("assets:asset-label-print", args=[asset.pk]),
        {"idempotency_key": "S6DIRECTPRINT-key"},
    )

    batch = asset.qr_identities.get(status="active").print_items.get().batch
    assert response.status_code == 302
    assert response.url == reverse("assets:label-batch-print", args=[batch.pk])
    assert batch.status == "generated"
    assert batch.items.count() == 1
    assert batch.items.get().qr_identity.asset_id == asset.pk

    detail = client.get(response.url)
    assert detail.status_code == 200
    assert reverse(
        "assets:label-item-qr", args=[batch.items.get().pk]
    ) in detail.content.decode()

    confirm_print_batch(actor=context["finance"], batch=batch)
    printed_detail = client.get(reverse("assets:label-batch-detail", args=[batch.pk]))
    assert printed_detail.status_code == 200
    assert reverse("assets:qr-web-attach", args=[asset.pk]) in printed_detail.content.decode()


def test_web_attachment_requires_checks_and_records_confirmation_method(client):
    context, asset, qr_identity = formal_asset_context("S6WEBATTACH")
    _print(context, asset, "S6WEBATTACH-print")
    qr_identity.refresh_from_db()
    client.force_login(context["finance"])
    url = reverse("assets:qr-web-attach", args=[asset.pk])

    page = client.get(url)
    text = page.content.decode()
    assert page.status_code == 200
    assert reverse("assets:asset-current-qr", args=[asset.pk]) in text
    assert "Web 确认贴标" in text
    assert "确认已贴标" in text
    assert qr_identity.public_token not in text

    incomplete = client.post(
        url,
        {
            "qr_identity_id": str(qr_identity.pk),
            "target_status": "in_use",
            "idempotency_key": "S6WEBATTACH-incomplete",
        },
    )
    assert incomplete.status_code == 400
    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    assert asset.asset_status == "pending_label"
    assert qr_identity.label_status == "printed"

    confirmed = client.post(
        url,
        {
            "qr_identity_id": str(qr_identity.pk),
            "label_attached": "on",
            "responsibility_confirmed": "on",
            "target_status": "in_use",
            "idempotency_key": "S6WEBATTACH-confirm",
        },
    )
    assert confirmed.status_code == 302
    assert confirmed.url == reverse("assets:asset-detail", args=[asset.pk])

    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    movement = AssetMovement.objects.get(asset=asset, movement_type="label_activation")
    audit = AuditLog.objects.get(action="asset_label.attached", object_id=str(asset.pk))
    assert asset.asset_status == "in_use"
    assert qr_identity.label_status == "attached"
    assert movement.reason == "Web 端逐项确认首次贴标"
    assert audit.new_data_json["confirmation_method"] == "web"


def test_web_attachment_is_not_available_before_print_confirmation(client):
    context, asset, qr_identity = formal_asset_context("S6WEBEARLY")
    client.force_login(context["finance"])
    url = reverse("assets:qr-web-attach", args=[asset.pk])

    page = client.get(url)
    assert page.status_code == 200
    assert "尚未确认打印完成" in page.content.decode()

    response = client.post(
        url,
        {
            "qr_identity_id": str(qr_identity.pk),
            "label_attached": "on",
            "responsibility_confirmed": "on",
            "target_status": "in_use",
            "idempotency_key": "S6WEBEARLY-key",
        },
    )
    assert response.status_code == 400
    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    assert asset.asset_status == "pending_label"
    assert qr_identity.label_status == "ready_to_print"


def test_generated_batch_refuses_a_noncurrent_identity(client):
    context, asset, qr_identity = formal_asset_context("S6STALE")
    batch = generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key="S6STALE-batch",
    )
    item = batch.items.get()
    client.force_login(context["finance"])

    qr_identity.label_status = "attached"
    item.qr_identity = qr_identity

    from apps.assets.qr_views import _item_has_current_printable_identity

    assert _item_has_current_printable_identity(item) is False


def test_print_css_fixes_a4_geometry_qr_minimum_size_and_360px_layout():
    css = open("static/css/qr-labels.css", encoding="utf-8").read()

    assert "size: A4 portrait" in css
    assert "grid-template-columns: repeat(3, 1fr)" in css
    assert "grid-template-rows: repeat(8, 35mm)" in css
    assert "width: 22mm" in css
    assert "height: 22mm" in css
    assert "@media (max-width: 360px)" in css
    assert "@media print" in css
    assert ".qr-preview-link" in css
    assert ".screen-scan-hint" in css
    assert "transform: scale(2.5)" in css
    assert "cursor: zoom-in" in css
    assert "content: none !important" in css
    assert "http://" not in css and "https://" not in css


@pytest.mark.django_db(transaction=True)
def test_postgresql_guards_reject_raw_qr_history_and_movement_mutation():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-specific database guard acceptance")
    context, asset, qr_identity = formal_asset_context("S6RAW")
    _attach(context, asset, qr_identity, "S6RAW")
    movement = AssetMovement.objects.get(asset=asset)

    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE assets_assetqridentity SET public_token = %s WHERE id = %s",
                ["Z" * 43, qr_identity.pk],
            )
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM assets_assetmovement WHERE id = %s", [movement.pk]
            )

    qr_identity.refresh_from_db()
    assert qr_identity.public_token != "Z" * 43
    assert AssetMovement.objects.filter(pk=movement.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_attachment_creates_only_one_activation():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-locking acceptance")
    context, asset, qr_identity = formal_asset_context("S6CONCUR")
    _print(context, asset, "S6CONCUR-print")
    qr_identity.refresh_from_db()
    actor_id = context["finance"].pk
    asset_id = asset.pk
    token = qr_identity.public_token

    def worker():
        close_old_connections()
        try:
            actor = type(context["finance"]).objects.get(pk=actor_id)
            locked_asset = Asset.objects.get(pk=asset_id)
            result = confirm_label_attachment(
                actor=actor,
                asset=locked_asset,
                scanned_token=token,
                target_status="in_use",
                idempotency_key="S6CONCUR-attach",
            )
            return str(result.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: worker(), range(2)))

    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    assert len(set(results)) == 1
    assert asset.asset_status == "in_use"
    assert qr_identity.label_status == "attached"
    assert AssetMovement.objects.filter(asset=asset).count() == 1
    assert AuditLog.objects.filter(action="asset_label.attached").count() == 1
