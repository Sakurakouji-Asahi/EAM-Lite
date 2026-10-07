"""Labels remain individually checked while batch follow-up stays in context."""

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.assets.models import AssetMovement, AssetQrIdentity
from apps.assets.qr_services import confirm_label_attachment, generate_print_batch, rotate_qr_identity
from apps.audit.models import AuditLog
from tests.test_label_queue_usability import make_asset
from tests.test_sprint3_support import make_user
from tests.test_unified_asset_identity import context


pytestmark = pytest.mark.django_db


def _attach(context, asset, key):
    identity = asset.qr_identities.get(status="active")
    confirm_label_attachment(
        actor=context["finance"], asset=asset, scanned_token=identity.public_token,
        target_status="in_use", idempotency_key=key,
    )


def test_batch_followup_tracks_current_states_and_filters_without_changing_counts(context, client):
    pending, attached, inactive = [make_asset(context, index) for index in range(1, 4)]
    batch = generate_print_batch(actor=context["finance"], assets=[pending, attached, inactive], idempotency_key="batch-followup")
    _attach(context, attached, "batch-followup-attach")
    _attach(context, inactive, "batch-followup-rotate-attach")
    rotate_qr_identity(actor=context["finance"], asset=inactive, reason="标签损坏")
    client.force_login(context["finance"])
    url = reverse("assets:label-batch-detail", args=[batch.pk])
    page = client.get(url)
    assert page.status_code == 200
    assert page.context["label_counts"] == {"total": 3, "pending": 1, "attached": 1, "inactive": 1, "waiting": 0}
    assert not page.context["can_open_print_view"]
    html = page.content.decode()
    assert "打开 A4 打印视图" not in html
    assert "原打印页不再开放" in html
    assert f"?return_batch={batch.pk}" in html
    for state, asset in (("pending", pending), ("attached", attached), ("inactive", inactive)):
        filtered = client.get(url, {"work": state})
        assert [item.qr_identity.asset_id for item in filtered.context["items"]] == [asset.pk]
        assert filtered.context["label_counts"] == page.context["label_counts"]
    searched = client.get(url, {"q": "EQUIP-001", "work": "pending", "page": 2})
    assert [item.qr_identity.asset_id for item in searched.context["items"]] == [pending.pk]
    assert "EQUIP-001" in searched.content.decode()
    params = parse_qs(urlsplit(searched.context["label_links"]["attached"]).query)
    assert params == {"q": ["EQUIP-001"], "work": ["attached"]}
    assert client.get(url, {"work": "invalid"}).status_code == 400
    assert client.get(url, {"q": "x" * 201}).status_code == 400
    assert all(identity.public_token not in html for identity in AssetQrIdentity.objects.all())


def test_web_confirmation_returns_to_batch_pending_items_and_preserves_existing_audit(context, client):
    first, second = make_asset(context, 1), make_asset(context, 2)
    batch = generate_print_batch(actor=context["finance"], assets=[first, second], idempotency_key="continuous-batch")
    client.force_login(context["finance"])
    url = reverse("assets:qr-web-attach", args=[first.pk])
    page = client.get(url, {"return_batch": str(batch.pk)})
    assert page.status_code == 200
    assert page.context["return_batch"].pk == batch.pk
    assert "返回本批次待贴标" in page.content.decode()
    form = page.context["form"]
    payload = {"qr_identity_id": form["qr_identity_id"].value(), "idempotency_key": form["idempotency_key"].value(),
               "return_batch": str(batch.pk), "target_status": "in_use"}
    incomplete = client.post(url, payload)
    assert incomplete.status_code == 400
    assert incomplete.context["return_batch"].pk == batch.pk
    assert not AssetMovement.objects.filter(asset=first, movement_type="label_activation").exists()
    response = client.post(url, {**payload, "label_attached": "on", "responsibility_confirmed": "on"})
    destination = reverse("assets:label-batch-detail", args=[batch.pk]) + "?work=pending"
    assert response.status_code == 302 and response.url == destination
    remaining = client.get(destination)
    assert [item.qr_identity.asset_id for item in remaining.context["items"]] == [second.pk]
    assert remaining.context["label_counts"]["attached"] == 1
    assert remaining.context["label_counts"]["pending"] == 1
    audit = AuditLog.objects.get(action="asset_label.attached", object_id=str(first.pk))
    assert audit.new_data_json["confirmation_method"] == "web"
    assert AssetMovement.objects.filter(asset=first, movement_type="label_activation").count() == 1
    completed_page = client.get(url, {"return_batch": str(batch.pk)})
    assert completed_page.status_code == 200
    assert "当前标签已完成确认，无需重复贴标。" in completed_page.content.decode()
    assert "当前二维码尚未执行打印操作" not in completed_page.content.decode()


def test_forged_batch_return_is_rejected_before_any_attachment(context, client):
    first, other = make_asset(context, 1), make_asset(context, 2)
    original = generate_print_batch(actor=context["finance"], assets=[first], idempotency_key="return-original")
    unrelated = generate_print_batch(actor=context["finance"], assets=[other], idempotency_key="return-unrelated")
    identity = first.qr_identities.get(status="active")
    client.force_login(context["finance"])
    url = reverse("assets:qr-web-attach", args=[first.pk])
    payload = {"qr_identity_id": str(identity.pk), "label_attached": "on", "responsibility_confirmed": "on",
               "target_status": "in_use", "idempotency_key": "return-invalid-attachment"}
    for batch_id in ("https://outside.example/", str(uuid.uuid4()), str(unrelated.pk)):
        assert client.get(url, {"return_batch": batch_id}).status_code == 404
        assert client.post(url, {**payload, "return_batch": batch_id}).status_code == 404
    identity.refresh_from_db()
    assert identity.label_status == "printed"
    assert not AssetMovement.objects.filter(asset=first, movement_type="label_activation").exists()
    assert not AuditLog.objects.filter(action="asset_label.attached", object_id=str(first.pk)).exists()
    assert client.get(url, {"return_batch": str(original.pk)}).status_code == 200
    client.force_login(make_user("batch-followup-employee", "employee"))
    assert client.get(reverse("assets:label-batch-detail", args=[original.pk])).status_code == 403
    assert client.get(url, {"return_batch": str(original.pk)}).status_code == 403
