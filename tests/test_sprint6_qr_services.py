from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.assets.models import (
    Asset,
    AssetLabelAttachmentRequest,
    AssetLabelPrintBatch,
    AssetLabelPrintItem,
    AssetMovement,
    AssetQrIdentity,
)
from apps.assets.qr_services import (
    build_qr_payload,
    cancel_print_batch,
    confirm_label_attachment,
    confirm_print_batch,
    generate_print_batch,
    generate_public_token,
    render_qr_svg,
    rotate_qr_identity,
)
from apps.audit.models import AuditLog
from tests.test_sprint3_support import (
    direct_draft,
    make_category,
    make_company,
)
from tests.test_sprint6_support import formal_asset_context


pytestmark = pytest.mark.django_db


def _generated(context, asset, key, **options):
    return generate_print_batch(
        actor=context["finance"],
        assets=[asset],
        idempotency_key=key,
        **options,
    )


def _printed(context, asset, key):
    batch = _generated(context, asset, key)
    return confirm_print_batch(actor=context["finance"], batch=batch)


def _attached(prefix: str, *, target_status="in_use"):
    context, asset, qr_identity = formal_asset_context(prefix)
    _printed(context, asset, f"{prefix}-print")
    qr_identity.refresh_from_db()
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr_identity.public_token,
        target_status=target_status,
        idempotency_key=f"{prefix}-attach",
    )
    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    return context, asset, qr_identity


def test_token_has_256_bits_of_urlsafe_entropy_and_is_not_asset_derived():
    context, asset, qr_identity = formal_asset_context("S6TOKEN")
    tokens = {generate_public_token() for _ in range(128)}

    assert len(tokens) == 128
    assert all(len(token) == 43 for token in tokens)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", token) for token in tokens)
    assert len(qr_identity.public_token) == 43
    assert str(asset.pk) not in qr_identity.public_token
    assert asset.asset_code not in qr_identity.public_token
    assert context["company"].code not in qr_identity.public_token


def test_qr_payload_contains_only_https_lan_root_and_opaque_token():
    context, asset, qr_identity = formal_asset_context("S6PAYLOAD")
    payload = build_qr_payload(qr_identity)
    svg = render_qr_svg(qr_identity)

    assert payload.startswith("https://")
    assert payload.endswith(f"/assets/scan/{qr_identity.public_token}/")
    for forbidden in (
        asset.asset_name,
        asset.asset_code,
        str(asset.pk),
        asset.responsible_employee.name,
        asset.department.name,
        asset.location.name,
    ):
        assert forbidden not in payload
    assert svg.startswith("<?xml")
    assert "<svg" in svg


def test_generate_batch_is_idempotent_and_freezes_nonfinancial_snapshot():
    context, asset, qr_identity = formal_asset_context("S6SNAP")
    batch = _generated(
        context,
        asset,
        "S6SNAP-batch",
        include_responsible_employee=True,
        include_location=True,
        include_model=True,
    )
    repeated = _generated(
        context,
        asset,
        "S6SNAP-batch",
        include_responsible_employee=True,
        include_location=True,
        include_model=True,
    )
    item = batch.items.get()

    assert repeated.pk == batch.pk
    assert AssetLabelPrintBatch.objects.count() == 1
    assert AssetLabelPrintItem.objects.count() == 1
    assert batch.status == "generated"
    assert batch.printed_by_id is None
    assert batch.printed_at is None
    assert item.page_no == 1 and item.position_no == 1
    assert item.label_snapshot_json == {
        "company_short_name": context["company"].short_name,
        "asset_name": asset.asset_name,
        "asset_code": asset.asset_code,
        "department": asset.department.name,
        "responsible_employee": asset.responsible_employee.name,
        "location": " / ".join(
            (asset.location.parent.parent.name, asset.location.parent.name, asset.location.name)
        ),
        "model": asset.model,
    }
    assert not set(item.label_snapshot_json).intersection(
        {"original_cost", "book_value", "public_token", "employee_no"}
    )
    qr_identity.refresh_from_db()
    assert qr_identity.label_status == "ready_to_print"
    assert AuditLog.objects.filter(action="asset_label.print_generated").count() == 1


def test_same_print_idempotency_key_rejects_different_request():
    context, asset, _ = formal_asset_context("S6IDEM")
    _generated(context, asset, "S6IDEM-key")

    with pytest.raises(ValidationError):
        _generated(
            context,
            asset,
            "S6IDEM-key",
            include_location=True,
        )

    assert AssetLabelPrintBatch.objects.count() == 1


def test_draft_without_official_code_or_qr_cannot_generate_label():
    context, _formal, _qr = formal_asset_context("S6DRAFT")
    draft = direct_draft(
        context["company"],
        context["category"],
        actor=context["equipment"],
    )

    with pytest.raises(ValidationError):
        _generated(context, draft, "S6DRAFT-key")

    assert not AssetLabelPrintBatch.objects.exists()


def test_cross_company_asset_selection_is_rejected_before_batch_creation():
    context, _asset, _qr = formal_asset_context("S6CROSS")
    other_company = make_company("S6OTHER", active=False)
    other_category = make_category(other_company, "S6OTHER-CAT")
    other_asset = direct_draft(
        other_company,
        other_category,
        actor=context["equipment"],
    )

    with pytest.raises(PermissionDenied):
        _generated(context, other_asset, "S6CROSS-key")

    assert not AssetLabelPrintBatch.objects.exists()


def test_role_without_label_action_is_rejected_even_with_global_asset_scope():
    context, asset, _qr = formal_asset_context("S6ROLE")

    with pytest.raises(PermissionDenied):
        generate_print_batch(
            actor=context["admin"],
            assets=[asset],
            idempotency_key="S6ROLE-key",
        )


def test_confirm_print_atomically_marks_batch_items_and_qr_but_not_asset():
    context, asset, qr_identity = formal_asset_context("S6PRINT")
    batch = _generated(context, asset, "S6PRINT-key")
    confirmed = confirm_print_batch(actor=context["finance"], batch=batch)
    repeated = confirm_print_batch(actor=context["finance"], batch=confirmed)
    asset.refresh_from_db()
    qr_identity.refresh_from_db()

    assert repeated.pk == batch.pk
    assert repeated.status == "printed"
    assert repeated.printed_by_id == context["finance"].pk
    assert repeated.printed_at is not None
    assert repeated.items.get().print_status == "printed"
    assert qr_identity.label_status == "printed"
    assert asset.asset_status == "pending_label"
    assert not AssetMovement.objects.exists()
    assert AuditLog.objects.filter(action="asset_label.print_confirmed").count() == 1


def test_cancel_batch_is_idempotent_and_does_not_falsely_mark_qr_printed():
    context, asset, qr_identity = formal_asset_context("S6CANCEL")
    batch = _generated(context, asset, "S6CANCEL-key")
    cancelled = cancel_print_batch(
        actor=context["finance"], batch=batch, reason="打印机卡纸"
    )
    repeated = cancel_print_batch(
        actor=context["finance"], batch=cancelled, reason="重复请求"
    )
    qr_identity.refresh_from_db()

    assert repeated.status == "cancelled"
    assert repeated.printed_by_id is None
    assert repeated.printed_at is None
    assert repeated.items.get().print_status == "cancelled"
    assert qr_identity.label_status == "ready_to_print"
    assert AuditLog.objects.filter(action="asset_label.print_cancelled").count() == 1


def test_cancel_requires_reason_and_rolls_back_state():
    context, asset, _qr = formal_asset_context("S6CANREASON")
    batch = _generated(context, asset, "S6CANREASON-key")

    with pytest.raises(ValidationError):
        cancel_print_batch(actor=context["finance"], batch=batch, reason="  ")

    batch.refresh_from_db()
    assert batch.status == "generated"
    assert batch.items.get().print_status == "generated"


def test_printed_label_requires_explicit_reprint_and_reuses_current_token():
    context, asset, qr_identity = formal_asset_context("S6REPRINT")
    _printed(context, asset, "S6REPRINT-first")

    with pytest.raises(ValidationError):
        _generated(context, asset, "S6REPRINT-implicit")

    reprint = _generated(
        context,
        asset,
        "S6REPRINT-explicit",
        explicit_reprint=True,
    )
    assert reprint.items.get().qr_identity_id == qr_identity.pk
    assert AssetQrIdentity.objects.filter(asset=asset).count() == 1


def test_attach_requires_printed_current_token_and_failure_is_atomic():
    context, asset, qr_identity = formal_asset_context("S6ATOMIC")

    with pytest.raises(ValidationError):
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token=qr_identity.public_token,
            target_status="in_use",
            idempotency_key="S6ATOMIC-not-printed",
        )
    with pytest.raises(PermissionDenied):
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token="X" * 43,
            target_status="in_use",
            idempotency_key="S6ATOMIC-wrong-token",
        )

    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    assert asset.asset_status == "pending_label"
    assert qr_identity.label_status == "ready_to_print"
    assert not AssetMovement.objects.exists()
    assert not AuditLog.objects.filter(action="asset_label.attached").exists()


def test_first_attachment_creates_exact_snapshot_once_and_is_idempotent():
    context, asset, qr_identity = formal_asset_context("S6ATTACH")
    _printed(context, asset, "S6ATTACH-print")
    qr_identity.refresh_from_db()

    attached = confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr_identity.public_token,
        target_status="in_use",
        idempotency_key="S6ATTACH-key",
    )
    repeated = confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr_identity.public_token,
        target_status="in_use",
        idempotency_key="S6ATTACH-key",
    )
    asset.refresh_from_db()
    movement = AssetMovement.objects.get(asset=asset)

    assert repeated.pk == attached.pk
    assert asset.asset_status == "in_use"
    assert attached.label_status == "attached"
    assert attached.attached_by_id == context["finance"].pk
    assert attached.attached_at is not None
    assert movement.movement_type == "label_activation"
    assert (movement.from_status, movement.to_status) == ("pending_label", "in_use")
    assert movement.from_department_id == movement.to_department_id == asset.department_id
    assert movement.from_employee_id == movement.to_employee_id == asset.responsible_employee_id
    assert movement.from_location_id == movement.to_location_id == asset.location_id
    assert AssetMovement.objects.filter(asset=asset).count() == 1
    assert AuditLog.objects.filter(action="asset_label.attached").count() == 1
    assert AssetLabelAttachmentRequest.objects.filter(
        asset=asset, idempotency_key="S6ATTACH-key"
    ).count() == 1

    with pytest.raises(ValidationError):
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token=qr_identity.public_token,
            target_status="idle",
            idempotency_key="S6ATTACH-key",
        )
    with pytest.raises(ValidationError):
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token=qr_identity.public_token,
            target_status="in_use",
            idempotency_key="S6ATTACH-new-key",
        )


def test_first_attachment_can_explicitly_choose_idle():
    _context, asset, qr_identity = _attached("S6IDLE", target_status="idle")

    assert asset.asset_status == "idle"
    assert qr_identity.label_status == "attached"
    assert AssetMovement.objects.get(asset=asset).to_status == "idle"


def test_missing_responsibility_prevents_activation_and_rolls_back():
    context, asset, qr_identity = formal_asset_context("S6MISSING")
    _printed(context, asset, "S6MISSING-print")
    Asset.objects.filter(pk=asset.pk).update(location=None)
    asset.refresh_from_db()
    qr_identity.refresh_from_db()

    with pytest.raises(ValidationError):
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token=qr_identity.public_token,
            target_status="in_use",
            idempotency_key="S6MISSING-key",
        )

    asset.refresh_from_db()
    qr_identity.refresh_from_db()
    assert asset.asset_status == "pending_label"
    assert qr_identity.label_status == "printed"
    assert not AssetMovement.objects.exists()


def test_attached_asset_cannot_reprint_until_token_rotation():
    context, asset, qr_identity = _attached("S6ATTACHED")

    with pytest.raises(ValidationError):
        _generated(
            context,
            asset,
            "S6ATTACHED-reprint",
            explicit_reprint=True,
        )

    assert AssetQrIdentity.objects.get(pk=qr_identity.pk).status == "active"


@pytest.mark.django_db(transaction=True)
def test_rotation_revokes_old_token_creates_one_new_version_and_redacts_audit():
    context, asset, old = _attached("S6ROTATE")
    new = rotate_qr_identity(
        actor=context["finance"],
        asset=asset,
        reason="标签破损，现场换标",
    )
    old.refresh_from_db()

    assert old.status == "revoked"
    assert old.revoked_at is not None
    assert old.revoked_by_id == context["finance"].pk
    assert new.status == "active"
    assert new.label_status == "ready_to_print"
    assert new.version == old.version + 1
    assert new.public_token != old.public_token
    assert AssetQrIdentity.objects.filter(asset=asset, status="active").count() == 1
    audit_text = json.dumps(
        list(
            AuditLog.objects.filter(action="asset_qr.rotated").values(
                "old_data_json", "new_data_json", "user_agent"
            )
        ),
        ensure_ascii=False,
    )
    assert old.public_token not in audit_text
    assert new.public_token not in audit_text


def test_relabel_confirmation_keeps_business_status_and_adds_no_second_movement():
    context, asset, _old = _attached("S6RELABEL", target_status="idle")
    new = rotate_qr_identity(
        actor=context["finance"], asset=asset, reason="旧标签无法扫描"
    )
    batch = _generated(context, asset, "S6RELABEL-reprint")
    confirm_print_batch(actor=context["finance"], batch=batch)
    new.refresh_from_db()
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=new.public_token,
        target_status=None,
        idempotency_key="S6RELABEL-relabel-attach",
    )
    asset.refresh_from_db()
    new.refresh_from_db()

    assert asset.asset_status == "idle"
    assert new.label_status == "attached"
    assert AssetMovement.objects.filter(asset=asset).count() == 1
    assert AssetLabelAttachmentRequest.objects.filter(
        asset=asset, idempotency_key="S6RELABEL-relabel-attach"
    ).count() == 1


@pytest.mark.django_db(transaction=True)
def test_generated_batch_must_be_closed_before_attachment_or_rotation():
    context, asset, qr_identity = formal_asset_context("S6OPENBATCH")
    _printed(context, asset, "S6OPENBATCH-first")
    generated = _generated(
        context,
        asset,
        "S6OPENBATCH-reprint",
        explicit_reprint=True,
    )
    qr_identity.refresh_from_db()

    with pytest.raises(ValidationError):
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token=qr_identity.public_token,
            target_status="in_use",
            idempotency_key="S6OPENBATCH-attach",
        )

    cancel_print_batch(actor=context["finance"], batch=generated, reason="取消多余重印")
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr_identity.public_token,
        target_status="in_use",
        idempotency_key="S6OPENBATCH-attach",
    )
    replacement = rotate_qr_identity(
        actor=context["finance"],
        asset=asset,
        reason="旧标签损坏",
    )
    assert replacement.version == qr_identity.version + 1


def test_audit_rows_for_print_and_attachment_never_contain_full_token():
    context, asset, qr_identity = formal_asset_context("S6AUDIT")
    _printed(context, asset, "S6AUDIT-print")
    qr_identity.refresh_from_db()
    confirm_label_attachment(
        actor=context["finance"],
        asset=asset,
        scanned_token=qr_identity.public_token,
        target_status="in_use",
        idempotency_key="S6AUDIT-attach",
    )

    audit_text = json.dumps(
        list(
            AuditLog.objects.filter(company=context["company"]).values(
                "action", "object_id", "old_data_json", "new_data_json", "user_agent"
            )
        ),
        ensure_ascii=False,
        default=str,
    )
    assert qr_identity.public_token not in audit_text
