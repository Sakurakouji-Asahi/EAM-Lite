"""Real CSRF middleware checks for no-referrer inventory form submissions."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.core.qr_csrf import build_qr_opaque_origin_bridge
from apps.inventory.models import InventoryScan
from apps.inventory.services import publish_inventory_task, stop_inventory_scanning
from tests.test_sprint3_support import make_user
from tests.test_sprint8_services import _draft
from tests.test_sprint8_support import inventory_context


pytestmark = pytest.mark.django_db


def _csrf_data(client, page):
    return {
        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        "opaque_origin_bridge": page.context["opaque_origin_bridge"],
    }


def _scan_data(page):
    form = page.context["form"]
    return {name: form[name].value() for name in (
        "idempotency_key", "actual_location", "actual_employee", "actual_status",
    )}


@override_settings(
    QR_BASE_URL="https://testserver", CSRF_TRUSTED_ORIGINS=["https://testserver", "http://testserver"],
)
def test_normal_scan_chain_accepts_null_origin_with_no_referer_on_http_and_https():
    context, _asset, qr = inventory_context("SCANORIGIN")
    task = publish_inventory_task(actor=context["finance"], task=_draft(context, "SCANORIGIN-T"))
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["equipment"])
    entry_url = reverse("inventory:task-scan", args=[task.pk])

    for secure in (False, True):
        entry = client.get(entry_url, secure=secure)
        assert entry["Referrer-Policy"] == "no-referrer"
        assert entry["Cache-Control"] == "private, no-store"
        assert 'name="opaque_origin_bridge"' in entry.content.decode()
        started = client.post(
            entry_url, {**_csrf_data(client, entry), "token": qr.public_token},
            HTTP_ORIGIN="null", secure=secure,
        )
        assert started.status_code == 302
        assert qr.public_token not in started.url
        page = client.get(started.url, secure=secure)
        assert page["Referrer-Policy"] == "no-referrer"
        assert page["Cache-Control"] == "private, no-store"
        saved = client.post(
            started.url, {**_csrf_data(client, page), **_scan_data(page)},
            HTTP_ORIGIN="null", secure=secure,
        )
        assert saved.status_code == 302 and saved.url == entry_url
    assert InventoryScan.objects.filter(inventory_task=task).count() == 2
    assert InventoryScan.objects.filter(inventory_task=task, is_effective=True).count() == 1


@override_settings(QR_BASE_URL="http://testserver", ALLOWED_HOSTS=["testserver", "alternate.test"])
def test_scan_bridge_rejections_preserve_context_and_still_require_cookie_token_session_and_path():
    context, _asset, qr = inventory_context("SCANORIGINSAFE")
    task = publish_inventory_task(actor=context["finance"], task=_draft(context, "SCANORIGINSAFE-T"))
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["equipment"])
    entry_url = reverse("inventory:task-scan", args=[task.pk])
    entry = client.get(entry_url)
    entry_data = {**_csrf_data(client, entry), "token": qr.public_token}
    # A token alone is not enough to opt into the compatibility exception.
    without_bridge = {key:value for key,value in entry_data.items() if key != "opaque_origin_bridge"}
    assert client.post(entry_url, without_bridge, HTTP_ORIGIN="null").status_code == 403
    assert client.post(entry_url, {**entry_data, "opaque_origin_bridge": entry_data["opaque_origin_bridge"] + "tampered"}, HTTP_ORIGIN="null").status_code == 403
    with patch("django.core.signing.TimestampSigner.timestamp", return_value="1"):
        expired = build_qr_opaque_origin_bridge(
            user_id=context["equipment"].pk, session_key=client.session.session_key, path=entry_url,
        )
    assert client.post(entry_url, {**entry_data, "opaque_origin_bridge": expired}, HTTP_ORIGIN="null").status_code == 403
    started = client.post(entry_url, entry_data, HTTP_ORIGIN="null")
    assert started.status_code == 302
    page = client.get(started.url)
    payload = {**_csrf_data(client, page), **_scan_data(page)}
    scan_state = deepcopy(dict(client.session))
    assert client.post(started.url, {**payload, "opaque_origin_bridge": entry_data["opaque_origin_bridge"]}, HTTP_ORIGIN="null").status_code == 403
    assert client.post(started.url, {**payload, "csrfmiddlewaretoken": "A" * 32}, HTTP_ORIGIN="null").status_code == 403
    assert client.post(started.url, {key:value for key,value in payload.items() if key != "csrfmiddlewaretoken"}, HTTP_ORIGIN="null").status_code == 403
    assert client.post(started.url, payload, HTTP_ORIGIN="null", HTTP_HOST="alternate.test").status_code == 403
    assert client.post(started.url, payload, HTTP_ORIGIN="null", HTTP_REFERER="https://evil.example/form/").status_code == 403
    assert client.post(started.url, payload, HTTP_ORIGIN="https://evil.example").status_code == 403
    for other_path in (reverse("logout"), reverse("inventory:task-stop", args=[task.pk])):
        assert client.post(other_path, payload, HTTP_ORIGIN="null").status_code == 403

    csrf_cookie = client.cookies.pop("csrftoken")
    assert client.post(started.url, payload, HTTP_ORIGIN="null").status_code == 403
    client.cookies["csrftoken"] = csrf_cookie
    another_session = Client(enforce_csrf_checks=True)
    another_session.force_login(context["equipment"])
    another_session.cookies["csrftoken"] = csrf_cookie
    assert another_session.post(started.url, payload, HTTP_ORIGIN="null").status_code == 403
    outsider = make_user("scan-origin-outsider", "employee")
    outsider_client = Client(enforce_csrf_checks=True)
    outsider_client.force_login(outsider)
    outsider_client.cookies["csrftoken"] = csrf_cookie
    own_bridge = build_qr_opaque_origin_bridge(
        user_id=outsider.pk, session_key=outsider_client.session.session_key, path=entry_url,
    )
    assert outsider_client.post(entry_url, {**entry_data, "opaque_origin_bridge": own_bridge}, HTTP_ORIGIN="null").status_code == 404
    assert InventoryScan.objects.filter(inventory_task=task).count() == 0
    assert dict(client.session) == scan_state
    # The successful retry uses the original context left intact by CSRF 403s.
    assert client.post(started.url, payload, HTTP_ORIGIN="null").status_code == 302
    assert InventoryScan.objects.filter(inventory_task=task).count() == 1


@override_settings(QR_BASE_URL="https://testserver", CSRF_TRUSTED_ORIGINS=["https://testserver"])
def test_controlled_supplement_uses_its_own_bridge_and_preserves_reconciliation():
    context, asset, qr = inventory_context("SUPPLEMENTORIGIN")
    task = publish_inventory_task(actor=context["finance"], task=_draft(context, "SUPPLEMENTORIGIN-T"))
    stop_inventory_scanning(actor=context["finance"], task=task, reason="现场结束", idempotency_key="supplement-origin-stop")
    row = task.task_assets.get(asset=asset)
    url = reverse("inventory:task-supplement", args=[task.pk, row.pk])
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["finance"])
    page = client.get(url, secure=True)
    assert page.status_code == 200 and page["Referrer-Policy"] == "no-referrer"
    payload = {
        **_csrf_data(client, page), **_scan_data(page),
        "public_token": qr.public_token, "supplement_reason": "现场漏盘后受控补盘",
    }
    without_bridge = {key:value for key,value in payload.items() if key != "opaque_origin_bridge"}
    assert client.post(url, without_bridge, HTTP_ORIGIN="null", secure=True).status_code == 403
    assert not InventoryScan.objects.filter(inventory_task=task).exists()
    saved = client.post(url, payload, HTTP_ORIGIN="null", secure=True)
    assert saved.status_code == 302
    task.refresh_from_db()
    assert task.status == "reconciliation"
    scan = InventoryScan.objects.get(inventory_task=task)
    assert scan.scan_mode == "supplemental" and scan.is_effective
    assert qr.public_token not in saved.url
