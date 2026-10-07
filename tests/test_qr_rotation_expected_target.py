"""Unexecuted HTTP symptom test; source-only prototype, not a run result."""

from __future__ import annotations

from html.parser import HTMLParser
from html import escape
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid
import json

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections
from django.test import Client, override_settings
from django.urls import reverse

from apps.assets.models import Asset, AssetMovement, AssetQrIdentity
from apps.assets.qr_services import (
    confirm_label_attachment, confirm_print_batch, generate_print_batch,
    rotate_qr_identity,
)
from apps.audit.models import AuditLog
from tests.test_sprint7_support import active_asset_context


class _RenderedForms(HTMLParser):
    """Collect real form controls without assuming any proposed new field exists."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            if self.current is not None:
                raise AssertionError("Unexpected nested form in rotation HTML")
            self.current = {"names": set(), "hidden": {}}
        elif self.current is not None and tag in {"input", "select", "textarea"}:
            name = attrs.get("name")
            if name:
                self.current["names"].add(name)
                if tag == "input" and attrs.get("type", "").lower() == "hidden":
                    if name in self.current["hidden"]:
                        raise AssertionError(f"Duplicate hidden control: {name}")
                    self.current["hidden"][name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def _actual_rotation_payload(response, *, explanation):
    parser = _RenderedForms()
    parser.feed(response.content.decode("utf-8"))
    forms = [item for item in parser.forms if {"reason", "explanation"} <= item["names"]]
    assert len(forms) == 1, "Expected exactly one actual rotation form"
    data = dict(forms[0]["hidden"])
    assert data.get("csrfmiddlewaretoken"), "Actual GET must render its CSRF control"
    data.update(reason="damaged", explanation=explanation)
    return data


@pytest.mark.django_db(transaction=True)
@override_settings(ALLOWED_HOSTS=["testserver"])
def test_old_rotation_page_cannot_revoke_a_newly_attached_replacement():
    # Build v1 only through established formalize/print/attach services.
    context, asset, v1 = active_asset_context("QRSTALEHTTP", status="idle")
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["finance"])
    url = reverse("assets:qr-rotate", args=[asset.pk])
    old_page = client.get(url)
    assert old_page.status_code == 200
    assert old_page.context["qr_identity"].pk == v1.pk
    explanation = "旧页面保留的损坏位置说明"
    old_payload = _actual_rotation_payload(old_page, explanation=explanation)

    # A separate legitimate operation produces, prints, and attaches v2.
    v2 = rotate_qr_identity(actor=context["finance"], asset=asset, reason="另一次现场合法换标")
    batch = generate_print_batch(
        actor=context["finance"], assets=[asset], idempotency_key="QRSTALEHTTP-v2-print",
    )
    confirm_print_batch(actor=context["finance"], batch=batch)
    v2.refresh_from_db()
    confirm_label_attachment(
        actor=context["finance"], asset=asset, scanned_token=v2.public_token,
        target_status=None, idempotency_key="QRSTALEHTTP-v2-attach",
    )
    v1.refresh_from_db()
    v2.refresh_from_db()
    asset.refresh_from_db()
    rotations = AuditLog.objects.filter(company=context["company"], action="asset_qr.rotated")
    assert v1.status == AssetQrIdentity.Status.REVOKED
    assert v2.status == AssetQrIdentity.Status.ACTIVE
    assert v2.label_status == AssetQrIdentity.LabelStatus.ATTACHED
    assert v2.version == v1.version + 1
    assert asset.asset_status == "idle"
    assert rotations.count() == 1
    movements_before = AssetMovement.objects.filter(asset=asset).count()

    # Submit the actual old HTML controls. No proposed field is inspected above.
    response = client.post(url, old_payload, HTTP_ORIGIN="http://testserver")
    v2.refresh_from_db()
    observation = {
        "old_version": v1.version, "replacement_version": v2.version,
        "replacement_status": v2.status, "response_status": response.status_code,
        "identity_count": AssetQrIdentity.objects.filter(asset=asset).count(),
        "rotation_audit_count": rotations.count(),
    }
    print("QR_ROTATION_EXPECTED_TARGET " + json.dumps(observation, sort_keys=True))

    # FIRST symptom assertion: do not fail first on a future field/API addition.
    assert v2.status == AssetQrIdentity.Status.ACTIVE, (
        "Old rotation page revoked a newly printed and attached replacement label"
    )
    assert AssetQrIdentity.objects.filter(asset=asset).count() == 2
    assert rotations.count() == 1
    assert response.status_code == 200
    form = response.context["form"]
    assert form.is_bound
    assert form.non_field_errors()
    assert form["reason"].value() == "damaged"
    assert form["explanation"].value() == explanation
    asset.refresh_from_db()
    assert asset.asset_status == "idle"
    assert AssetMovement.objects.filter(asset=asset).count() == movements_before


    # Only after the existing business symptom/assertions pass, inspect new target.
    assert str(form["expected_qr_identity_id"].value()) == str(v1.pk)
    rejected_payload = _actual_rotation_payload(response, explanation=explanation)
    assert rejected_payload["expected_qr_identity_id"] == str(v1.pk)
    rejected_again = client.post(url, rejected_payload, HTTP_ORIGIN="http://testserver")
    assert rejected_again.status_code == 200
    retained = rejected_again.context["form"]
    assert retained.is_bound
    assert retained.non_field_errors()
    assert str(retained["expected_qr_identity_id"].value()) == str(v1.pk)
    assert retained["reason"].value() == "damaged"
    assert retained["explanation"].value() == explanation
    v2.refresh_from_db()
    asset.refresh_from_db()
    assert v2.status == AssetQrIdentity.Status.ACTIVE
    assert v2.label_status == AssetQrIdentity.LabelStatus.ATTACHED
    assert AssetQrIdentity.objects.filter(asset=asset).count() == 2
    assert rotations.count() == 1
    assert AssetMovement.objects.filter(asset=asset).count() == movements_before
    assert asset.asset_status == "idle"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("flag,value", [("is_active", False), ("is_superuser", True)])
def test_rotation_rechecks_authoritative_account_flags_for_a_stale_actor(flag, value):
    context, asset, original = active_asset_context("QRACTORFLAGS", status="idle")
    saved_actor = context["finance"]
    assert saved_actor.is_active and not saved_actor.is_superuser
    type(saved_actor).objects.filter(pk=saved_actor.pk).update(**{flag: value})
    assert saved_actor.is_active and not saved_actor.is_superuser
    audit_count = AuditLog.objects.filter(company=context["company"], action="asset_qr.rotated").count()
    with pytest.raises(PermissionDenied, match="您没有对此资产执行标签操作的权限"):
        rotate_qr_identity(
            actor=saved_actor, asset=asset, reason="旧 actor 对象不能继续换标",
            expected_qr_identity_id=original.pk,
        )
    original.refresh_from_db()
    assert original.status == AssetQrIdentity.Status.ACTIVE
    assert original.label_status == AssetQrIdentity.LabelStatus.ATTACHED
    assert AssetQrIdentity.objects.filter(asset=asset).count() == 1
    assert AuditLog.objects.filter(company=context["company"], action="asset_qr.rotated").count() == audit_count
    asset.refresh_from_db()
    assert asset.asset_status == "idle"


@pytest.mark.django_db(transaction=True)
@override_settings(ALLOWED_HOSTS=["testserver"])
def test_fresh_rotation_page_rotates_only_its_rendered_current_identity():
    context, asset, original = active_asset_context("QRFRESHHTTP", status="idle")
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["finance"])
    url = reverse("assets:qr-rotate", args=[asset.pk])
    page = client.get(url)
    assert page.status_code == 200
    explanation = "已核对当前标签的损坏位置"
    payload = _actual_rotation_payload(page, explanation=explanation)
    assert payload["expected_qr_identity_id"] == str(original.pk)
    assert original.public_token not in page.content.decode("utf-8")
    movements_before = AssetMovement.objects.filter(asset=asset).count()
    response = client.post(url, payload, HTTP_ORIGIN="http://testserver")
    assert response.status_code == 302
    assert response.url == reverse("assets:label-queue")
    original.refresh_from_db()
    successor = AssetQrIdentity.objects.get(asset=asset, status=AssetQrIdentity.Status.ACTIVE)
    asset.refresh_from_db()
    assert original.status == AssetQrIdentity.Status.REVOKED
    assert successor.pk != original.pk
    assert successor.version == original.version + 1
    assert successor.label_status == AssetQrIdentity.LabelStatus.READY_TO_PRINT
    assert AssetQrIdentity.objects.filter(asset=asset).count() == 2
    assert asset.asset_status == "idle"
    assert AssetMovement.objects.filter(asset=asset).count() == movements_before
    audit = AuditLog.objects.get(company=context["company"], action="asset_qr.rotated")
    assert audit.user_id == context["finance"].pk
    assert audit.new_data_json["reason"] == "damaged：" + explanation
    audit_text = json.dumps([audit.old_data_json, audit.new_data_json], ensure_ascii=False)
    assert original.public_token not in audit_text
    assert successor.public_token not in audit_text


@pytest.mark.django_db(transaction=True)
@override_settings(ALLOWED_HOSTS=["testserver"])
@pytest.mark.parametrize("bad_target", ["missing", "malformed", "other_uuid"])
def test_bad_http_expected_target_is_visible_and_retains_input_without_mutation(bad_target):
    context, asset, original = active_asset_context("QRBADTARGET", status="idle")
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["finance"])
    url = reverse("assets:qr-rotate", args=[asset.pk])
    page = client.get(url)
    assert page.status_code == 200
    explanation = "需保留的位置说明 <壳体 & 线缆>"
    payload = _actual_rotation_payload(page, explanation=explanation)
    assert payload["expected_qr_identity_id"] == str(original.pk)
    if bad_target == "missing":
        payload.pop("expected_qr_identity_id")
    elif bad_target == "malformed":
        payload["expected_qr_identity_id"] = "not-a-valid-uuid"
    else:
        payload["expected_qr_identity_id"] = str(uuid.uuid4())
        assert payload["expected_qr_identity_id"] != str(original.pk)
    audit_count = AuditLog.objects.filter(company=context["company"], action="asset_qr.rotated").count()
    movements_before = AssetMovement.objects.filter(asset=asset).count()
    response = client.post(url, payload, HTTP_ORIGIN="http://testserver")
    assert response.status_code == 200
    form = response.context["form"]
    assert form.is_bound
    errors = form.errors.get("expected_qr_identity_id") or form.non_field_errors()
    assert errors, "Expected target refusal must be visible to the operator"
    text = response.content.decode("utf-8")
    for error in errors:
        assert escape(str(error)) in text
    assert form["reason"].value() == "damaged"
    assert form["explanation"].value() == explanation
    assert escape(explanation) in text
    assert form["expected_qr_identity_id"].value() == payload.get("expected_qr_identity_id")
    original.refresh_from_db()
    asset.refresh_from_db()
    assert original.status == AssetQrIdentity.Status.ACTIVE
    assert original.label_status == AssetQrIdentity.LabelStatus.ATTACHED
    assert AssetQrIdentity.objects.filter(asset=asset).count() == 1
    assert AuditLog.objects.filter(company=context["company"], action="asset_qr.rotated").count() == audit_count
    assert AssetMovement.objects.filter(asset=asset).count() == movements_before
    assert asset.asset_status == "idle"


@pytest.mark.django_db(transaction=True)
def test_postgresql_same_expected_identity_saves_once_and_refuses_the_competitor():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-locking acceptance")
    context, asset, original = active_asset_context("QRCONCURRENT", status="idle")
    actor_id, asset_id, expected_id = context["finance"].pk, asset.pk, original.pk
    movements_before = AssetMovement.objects.filter(asset=asset).count()
    barrier = Barrier(2)
    stale_message = "标签版本已变化或原标签已失效，请重新打开换标页面核对。"

    def worker():
        close_old_connections()
        try:
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config(%s, %s, false)", ["lock_timeout", "10s"])
                cursor.execute("SELECT set_config(%s, %s, false)", ["statement_timeout", "20s"])
                cursor.execute("SELECT pg_backend_pid(), current_setting('lock_timeout'), current_setting('statement_timeout')")
                backend_pid, lock_timeout, statement_timeout = cursor.fetchone()
            assert lock_timeout == "10s" and statement_timeout == "20s"
            actor = get_user_model().objects.get(pk=actor_id)
            current_asset = Asset.objects.get(pk=asset_id)
            # No lock has been requested before the rendezvous.
            barrier.wait(timeout=10)
            try:
                result = rotate_qr_identity(
                    actor=actor, asset=current_asset, reason="并发核对同一当前标签",
                    expected_qr_identity_id=expected_id,
                )
            except ValidationError as exc:
                if exc.messages != [stale_message] or hasattr(exc, "message_dict"):
                    raise
                return {"outcome": "stale", "backend_pid": backend_pid,
                        "lock_timeout": lock_timeout, "statement_timeout": statement_timeout,
                        "message": exc.messages[0]}
            return {"outcome": "saved", "identity_id": str(result.pk), "backend_pid": backend_pid,
                    "lock_timeout": lock_timeout, "statement_timeout": statement_timeout}
        finally:
            connection.close()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    print("QR_ROTATION_EXPECTED_CONCURRENCY " + json.dumps({"workers": results}, sort_keys=True))
    assert len({row["backend_pid"] for row in results}) == 2
    assert sorted(row["outcome"] for row in results) == ["saved", "stale"]
    original.refresh_from_db()
    asset.refresh_from_db()
    successor = AssetQrIdentity.objects.get(asset=asset, status=AssetQrIdentity.Status.ACTIVE)
    assert original.status == AssetQrIdentity.Status.REVOKED
    assert successor.version == original.version + 1
    assert successor.label_status == AssetQrIdentity.LabelStatus.READY_TO_PRINT
    assert AssetQrIdentity.objects.filter(asset=asset).count() == 2
    assert successor.pk == uuid.UUID(next(row["identity_id"] for row in results if row["outcome"] == "saved"))
    assert AuditLog.objects.filter(company=context["company"], action="asset_qr.rotated").count() == 1
    assert AssetMovement.objects.filter(asset=asset).count() == movements_before
    assert asset.asset_status == "idle"
