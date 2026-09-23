"""Token-only labels remain usable through the current server's scanner."""
import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.assets.qr_services import (
    build_qr_payload,
    generate_print_batch,
    token_from_scanned_payload,
)
from apps.inventory.services import publish_inventory_task
from tests.test_sprint3_support import make_user
from tests.test_sprint6_support import formal_asset_context
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db


def test_asset_scanner_finds_the_same_label_after_server_address_changes(client):
    context, asset, identity = formal_asset_context("PORTABLEQR")
    printed = generate_print_batch(
        actor=context["finance"], assets=[asset], idempotency_key="PORTABLEQR-print",
    )
    assert printed.template_version == "a4-v2"
    payload = build_qr_payload(identity)
    assert payload == identity.public_token
    assert "://" not in payload and "/" not in payload

    client.force_login(context["finance"])
    entry = reverse("assets:qr-scan-entry")
    page = client.get(entry)
    assert page.status_code == 200
    assert "zxing-browser.min.js" in page.content.decode()
    assert "qr-camera-scanner.js" in page.content.decode()

    with override_settings(QR_BASE_URL="https://replacement.company.lan"):
        identity.refresh_from_db()
        assert build_qr_payload(identity) == payload
        submitted = client.post(entry, {"payload": payload})
        assert submitted.status_code == 302
        assert submitted.url == reverse("assets:qr-scan", args=[identity.public_token])
        result = client.get(submitted.url)
        assert result.status_code == 200
        assert asset.asset_name in result.content.decode()


def test_scan_entry_rejects_invalid_text_and_preserves_csrf_and_asset_scope():
    context, asset, identity = formal_asset_context("PORTABLEGUARD")
    entry = reverse("assets:qr-scan-entry")
    csrf_client = Client(enforce_csrf_checks=True)
    assert csrf_client.get(entry).status_code == 302
    csrf_client.force_login(context["finance"])
    page = csrf_client.get(entry)
    assert page.status_code == 200
    assert csrf_client.post(entry, {"payload": identity.public_token}).status_code == 403
    csrf_token = csrf_client.cookies["csrftoken"].value
    invalid = csrf_client.post(entry, {
        "csrfmiddlewaretoken": csrf_token, "payload": "unknown-asset",
    })
    assert invalid.status_code == 400
    assert identity.public_token not in invalid.content.decode()
    assert csrf_client.post(entry, {
        "csrfmiddlewaretoken": csrf_token, "payload": identity.public_token,
    }).status_code == 302

    outsider = make_user("portable-scan-outsider", "employee")
    csrf_client.force_login(outsider)
    accepted = csrf_client.post(entry, {
        "csrfmiddlewaretoken": csrf_client.cookies["csrftoken"].value,
        "payload": identity.public_token,
    })
    assert accepted.status_code == 302
    denied = csrf_client.get(accepted.url)
    assert denied.status_code == 403
    assert asset.asset_name not in denied.content.decode()


def test_earlier_url_labels_are_parsed_only_as_tokens_by_the_in_app_scanner(client):
    context, asset, identity = formal_asset_context("PORTABLEOLD")
    old_url = f"http://192.168.1.111:8766/assets/scan/{identity.public_token}/"
    assert token_from_scanned_payload(old_url) == identity.public_token
    assert token_from_scanned_payload(identity.public_token) == identity.public_token
    for invalid in (
        old_url + "?next=https://example.org",
        f"https://user:pass@example.org/assets/scan/{identity.public_token}/",
        f"https://example.org/assets/scan/{identity.public_token}/extra/",
        "http://example.org/assets/scan/not-a-real-token/",
    ):
        assert token_from_scanned_payload(invalid) == ""
    client.force_login(context["finance"])
    submitted = client.post(reverse("assets:qr-scan-entry"), {"payload": old_url})
    assert submitted.status_code == 302
    assert client.get(submitted.url).status_code == 200


def test_inventory_scanner_accepts_raw_token_and_old_url_without_leaking_token(client):
    context, _asset, identity = inventory_context("PORTABLEINV")
    assignee = make_user("portable-inventory-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "PORTABLEINV-T", assignees=[assignee]),
    )
    client.force_login(assignee)
    entry = reverse("inventory:task-scan", args=[task.pk])
    page = client.get(entry)
    assert page.status_code == 200
    assert "qr-camera-scanner.js" in page.content.decode()
    for value in (
        identity.public_token,
        f"http://192.168.1.111:8766/assets/scan/{identity.public_token}/",
    ):
        response = client.post(entry, {"token": value})
        assert response.status_code == 302
        assert identity.public_token not in response.url
