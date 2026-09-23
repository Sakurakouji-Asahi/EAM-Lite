import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import AssetLabelAttachmentRequest, AssetLabelPrintBatch, AssetMovement, AssetQrIdentity
from apps.assets.qr_services import confirm_label_attachment
from apps.finance.models import AssetFinance
from tests.test_sprint3_support import make_user
from tests.test_unified_asset_identity import context, registered

pytestmark = pytest.mark.django_db(transaction=True)


def confirm(context, asset, method, key="nonphysical-confirm", evidence="已核对档案或现场标识"):
    qr = asset.qr_identities.get(status="active")
    return confirm_label_attachment(actor=context["equipment"], asset=asset,
        scanned_token=qr.public_token, target_status="in_use", idempotency_key=key,
        confirmation_method=method, identification_evidence=evidence)


def test_intangible_activates_without_a_print_batch(context):
    asset = registered(context, management_attribute="IA", unit="项")
    original_code = asset.asset_code
    qr = confirm(context, asset, "electronic", evidence="软件许可 LIC-TEST-01，已核对电子许可台账")
    asset.refresh_from_db()
    assert asset.asset_status == "in_use" and asset.asset_code == original_code
    assert qr.label_status == "attached"
    assert qr.get_label_status_display() == "电子台账已确认"
    assert not AssetLabelPrintBatch.objects.exists()
    assert not AssetFinance.objects.exists()
    assert AssetMovement.objects.get().movement_type == "label_activation"
    assert AssetLabelAttachmentRequest.objects.get().identification_method == "electronic"


def test_alternative_identification_records_evidence_and_replays(context):
    asset = registered(context)
    first = confirm(context, asset, "alternative", evidence="已核对设备铭牌及位置图，无法粘贴普通标签")
    repeated = confirm(context, asset, "alternative", evidence="已核对设备铭牌及位置图，无法粘贴普通标签")
    assert repeated.pk == first.pk
    assert first.get_label_status_display() == "替代标识已确认"
    assert AssetLabelAttachmentRequest.objects.count() == AssetMovement.objects.count() == 1
    with pytest.raises(ValidationError, match="不同"):
        confirm(context, asset, "alternative", evidence="变更原核对依据")


def test_nonphysical_confirmation_requires_evidence_and_correct_attribute(context):
    asset = registered(context)
    with pytest.raises(ValidationError, match="依据"):
        confirm(context, asset, "alternative", evidence="")
    with pytest.raises(ValidationError, match="IA"):
        confirm(context, asset, "electronic")
    with pytest.raises(ValidationError, match="打印"):
        confirm(context, asset, "web")
    assert not AssetMovement.objects.exists()
    assert not AssetLabelAttachmentRequest.objects.exists()


def test_qr_cannot_skip_print_without_a_nonphysical_confirmation_record(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL transition guard")
    asset = registered(context, management_attribute="IA")
    qr = asset.qr_identities.get(status="active")
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('eam_lite.controlled_qr_identity_mutation','on',true)")
        QuerySet.update(AssetQrIdentity.objects.filter(pk=qr.pk), label_status="attached",
                        attached_at=timezone.now(), attached_by_id=context["equipment"].pk)
    qr.refresh_from_db()
    assert qr.label_status == "ready_to_print"


def test_electronic_confirmation_page_works_without_camera(client, context):
    asset = registered(context, management_attribute="IA")
    client.force_login(context["equipment"])
    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert "确认电子台账" in detail.content.decode()
    url = reverse("assets:identification-confirm", args=[asset.pk])
    response = client.get(url)
    assert response.status_code == 200
    form = response.context["form"]
    result = client.post(url, {"idempotency_key": "electronic-http", "qr_token": form.fields["qr_token"].initial,
        "method": "electronic", "target_status": "in_use", "identification_evidence": "电子许可档案已核对"})
    assert result.status_code == 302
    asset.refresh_from_db()
    assert asset.asset_status == "in_use"
    assert not AssetLabelPrintBatch.objects.exists()


def test_confirmation_actor_can_be_removed_without_rewriting_evidence(context):
    asset = registered(context, management_attribute="IA")
    actor = make_user("temporary-confirmer", "equipment")
    qr = asset.qr_identities.get(status="active")
    confirm_label_attachment(actor=actor, asset=asset, scanned_token=qr.public_token,
        target_status="in_use", idempotency_key="actor-cleanup", confirmation_method="electronic",
        identification_evidence="核对电子台账")
    actor.delete()
    receipt = AssetLabelAttachmentRequest.objects.get()
    assert receipt.completed_by is None
    assert receipt.identification_method == "electronic"
    assert receipt.identification_evidence == "核对电子台账"
