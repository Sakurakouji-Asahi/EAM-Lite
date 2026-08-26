"""Transactional Sprint 6 QR printing, rotation and attachment services."""

from __future__ import annotations

import io
import hashlib
import json
import secrets
import uuid
from urllib.parse import quote

import qrcode
from qrcode.image.svg import SvgPathImage
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.assets.qr_permissions import require_label_action
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company


LABEL_TEMPLATE_VERSION = "a4-v1"
LABELS_PER_PAGE = 24
QR_MINIMUM_PRINT_SIZE_MM = 20


def generate_public_token() -> str:
    """Return 256 bits from the OS CSPRNG as an opaque URL-safe token."""
    return secrets.token_urlsafe(32)


def build_qr_payload(qr_identity) -> str:
    return f"{settings.QR_BASE_URL}/assets/scan/{quote(qr_identity.public_token, safe='')}/"


def render_qr_svg(qr_identity) -> str:
    qr = qrcode.QRCode(version=None, box_size=10, border=4)
    qr.add_data(build_qr_payload(qr_identity))
    qr.make(fit=True)
    stream = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(stream)
    return stream.getvalue().decode("utf-8")


def _company():
    company = current_company()
    if company is None or not company.is_active:
        raise PermissionDenied("当前没有启用公司。")
    return company


def _audit(*, actor, action, instance, old_data=None, new_data=None, request=None):
    return write_business_audit_log(
        company=instance.company,
        user=actor,
        action=action,
        object_type=instance._meta.object_name,
        object_id=instance.pk,
        old_data=old_data or {},
        new_data=new_data or {},
        **request_audit_context(request),
    )


def _enable_capability(name):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, true)", [name, "on"])


def _controlled_update(model, pk, values, capability):
    _enable_capability(capability)
    updated = QuerySet.update(model._base_manager.filter(pk=pk), **values)
    if updated != 1:
        raise ValidationError("受控更新未命中唯一记录。")


def _snapshot(
    asset, *, include_responsible_employee=False, include_location=False,
    include_model=False,
):
    result = {
        "company_short_name": asset.company.short_name,
        "asset_name": asset.asset_name,
        "asset_code": asset.asset_code,
        "department": asset.department.name if asset.department_id else "",
    }
    if include_responsible_employee:
        result["responsible_employee"] = (
            asset.responsible_employee.name if asset.responsible_employee_id else ""
        )
    if include_location:
        result["location"] = _location_path(asset.location)
    if include_model:
        result["model"] = asset.model
    return result


def _location_path(location):
    names = []
    seen = set()
    while location is not None and location.pk not in seen:
        seen.add(location.pk)
        names.append(location.name)
        location = location.parent
    return " / ".join(reversed(names))


def _print_request_fingerprint(
    asset_ids,
    *,
    include_responsible_employee,
    include_location,
    include_model,
    explicit_reprint,
):
    material = "|".join(
        [
            *(str(item) for item in asset_ids),
            str(bool(include_responsible_employee)),
            str(bool(include_location)),
            str(bool(include_model)),
            str(bool(explicit_reprint)),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@transaction.atomic
def generate_print_batch(
    *, actor, assets, idempotency_key, include_responsible_employee=False,
    include_location=False, include_model=False, explicit_reprint=False, request=None,
):
    from apps.assets.models import Asset, AssetLabelPrintBatch, AssetLabelPrintItem, AssetQrIdentity
    from apps.masterdata.models import Company

    company = _company()
    company = Company.objects.select_for_update().get(pk=company.pk)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "生成打印批次必须提供幂等键。"})
    asset_ids = sorted({str(getattr(asset, "pk", asset)) for asset in assets})
    if not asset_ids:
        raise ValidationError("至少选择一项资产。")
    locked = list(
        Asset.objects.select_for_update(of=("self",)).filter(company=company, pk__in=asset_ids)
        .select_related("company", "department", "responsible_employee", "location")
        .order_by("asset_code", "pk")
    )
    if len(locked) != len(asset_ids):
        raise PermissionDenied("选择中包含不存在或跨公司资产。")
    for asset in locked:
        require_label_action(actor, asset)
    existing = AssetLabelPrintBatch.objects.filter(
        company=company, idempotency_key=key
    ).first()
    if existing:
        existing_ids = sorted(
            str(asset_id)
            for asset_id in existing.items.values_list("qr_identity__asset_id", flat=True)
        )
        same_options = (
            existing.include_responsible_employee == bool(include_responsible_employee)
            and existing.include_location == bool(include_location)
            and existing.include_model == bool(include_model)
        )
        from apps.audit.models import AuditLog

        audit = AuditLog.objects.filter(
            company=company,
            action="asset_label.print_generated",
            object_type="AssetLabelPrintBatch",
            object_id=str(existing.pk),
        ).order_by("created_at").first()
        expected_fingerprint = _print_request_fingerprint(
            asset_ids,
            include_responsible_employee=include_responsible_employee,
            include_location=include_location,
            include_model=include_model,
            explicit_reprint=explicit_reprint,
        )
        if (
            existing_ids != asset_ids
            or not same_options
            or audit is None
            or audit.new_data_json.get("request_fingerprint") != expected_fingerprint
            or bool(audit.new_data_json.get("explicit_reprint")) != bool(explicit_reprint)
        ):
            raise ValidationError("相同幂等键已用于不同资产集合或排版选项。")
        if existing.status == "generated":
            return confirm_print_batch(
                actor=actor,
                batch=existing,
                request=request,
            )
        return existing
    identities = {}
    for asset in locked:
        if not asset.asset_code or asset.asset_status in {"draft", "pending_finance"}:
            raise ValidationError(f"资产 {asset} 没有正式编号，不能打印正式标签。")
        identity = AssetQrIdentity.objects.select_for_update().filter(
            company=company, asset=asset, status="active"
        ).first()
        if identity is None:
            raise ValidationError(f"资产 {asset.asset_code} 缺失财务确认创建的有效二维码身份。")
        if identity.label_status == "attached":
            raise ValidationError(f"资产 {asset.asset_code} 已贴标，必须先执行换标。")
        if identity.label_status not in {"ready_to_print", "printed"}:
            raise ValidationError(f"资产 {asset.asset_code} 的二维码当前不可打印。")
        if identity.label_status == "printed" and not explicit_reprint:
            raise ValidationError(f"资产 {asset.asset_code} 已记录打印，请明确选择重新打印。")
        identities[asset.pk] = identity
    batch = AssetLabelPrintBatch(
        company=company,
        batch_code=(
            f"LB-{timezone.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8].upper()}"
        ),
        template_version=LABEL_TEMPLATE_VERSION, status="generated",
        include_responsible_employee=bool(include_responsible_employee),
        include_location=bool(include_location), include_model=bool(include_model),
        created_by=actor, idempotency_key=key,
    )
    batch.full_clean()
    try:
        batch.save()
    except IntegrityError as exc:
        raise ValidationError("打印批次与现有记录冲突。") from exc
    for index, asset in enumerate(locked):
        values = dict(
            batch=batch, qr_identity=identities[asset.pk], page_no=index // LABELS_PER_PAGE + 1,
            position_no=index % LABELS_PER_PAGE + 1, print_status="generated",
        )
        fields = {f.name for f in AssetLabelPrintItem._meta.fields}
        if "label_snapshot_json" in fields:
            values["label_snapshot_json"] = _snapshot(
                asset,
                include_responsible_employee=include_responsible_employee,
                include_location=include_location,
                include_model=include_model,
            )
        item = AssetLabelPrintItem(**values)
        item.full_clean()
        item.save()
    _audit(actor=actor, action="asset_label.print_generated", instance=batch,
           new_data={"asset_count": len(locked), "template_version": LABEL_TEMPLATE_VERSION,
                     "explicit_reprint": bool(explicit_reprint),
                     "request_fingerprint": _print_request_fingerprint(
                         asset_ids,
                         include_responsible_employee=include_responsible_employee,
                         include_location=include_location,
                         include_model=include_model,
                         explicit_reprint=explicit_reprint,
                     )}, request=request)
    return confirm_print_batch(actor=actor, batch=batch, request=request)


@transaction.atomic
def confirm_print_batch(*, actor, batch, request=None):
    from apps.assets.models import AssetLabelPrintBatch, AssetLabelPrintItem, AssetQrIdentity
    company = _company()
    batch = AssetLabelPrintBatch.objects.select_for_update().get(pk=batch.pk, company=company)
    items = list(batch.items.select_for_update().select_related("qr_identity__asset"))
    for item in items:
        require_label_action(actor, item.qr_identity.asset)
    if batch.status == "printed":
        return batch
    if batch.status != "generated":
        raise ValidationError("只有历史遗留的已生成批次可补记打印操作。")
    now = timezone.now()
    locked_qrs = []
    for item in items:
        qr = AssetQrIdentity.objects.select_for_update().get(pk=item.qr_identity_id)
        if qr.status != "active" or qr.label_status not in {"ready_to_print", "printed"}:
            raise ValidationError("批次中的二维码身份已失效或不可打印。")
        locked_qrs.append(qr)
    for qr in locked_qrs:
        if qr.label_status == "ready_to_print":
            _controlled_update(AssetQrIdentity, qr.pk, {"label_status": "printed"},
                               "eam_lite.controlled_qr_identity_mutation")
    _controlled_update(AssetLabelPrintBatch, batch.pk,
                       {"status": "printed", "printed_by_id": actor.pk, "printed_at": now},
                       "eam_lite.controlled_label_batch_mutation")
    for item in items:
        _controlled_update(AssetLabelPrintItem, item.pk, {"print_status": "printed"},
                           "eam_lite.controlled_label_batch_mutation")
    batch.refresh_from_db()
    _audit(actor=actor, action="asset_label.print_confirmed", instance=batch,
           old_data={"status": "generated"}, new_data={
               "status": "printed",
               "asset_count": len(items),
               "automatic": True,
           },
           request=request)
    return batch


@transaction.atomic
def cancel_print_batch(*, actor, batch, reason="", request=None):
    from apps.assets.models import AssetLabelPrintBatch, AssetLabelPrintItem
    company = _company()
    explanation = str(reason or "").strip()
    if not explanation:
        raise ValidationError({"reason": "取消打印必须填写失败说明。"})
    batch = AssetLabelPrintBatch.objects.select_for_update().get(pk=batch.pk, company=company)
    items = list(batch.items.select_for_update().select_related("qr_identity__asset"))
    for item in items:
        require_label_action(actor, item.qr_identity.asset)
    if batch.status == "cancelled":
        return batch
    if batch.status != "generated":
        raise ValidationError("只有已生成且未确认的批次可取消。")
    _controlled_update(AssetLabelPrintBatch, batch.pk, {"status": "cancelled"},
                       "eam_lite.controlled_label_batch_mutation")
    for item in items:
        _controlled_update(AssetLabelPrintItem, item.pk, {"print_status": "cancelled"},
                           "eam_lite.controlled_label_batch_mutation")
    batch.refresh_from_db()
    _audit(actor=actor, action="asset_label.print_cancelled", instance=batch,
           old_data={"status": "generated"}, new_data={"status": "cancelled", "reason": explanation},
           request=request)
    return batch


@transaction.atomic
def rotate_qr_identity(*, actor, asset, reason, request=None):
    from apps.assets.models import Asset, AssetLabelPrintItem, AssetQrIdentity
    company = _company()
    asset = Asset.objects.select_for_update().get(pk=asset.pk, company=company)
    require_label_action(actor, asset)
    explanation = str(reason or "").strip()
    if not explanation:
        raise ValidationError({"reason": "换标必须填写原因。"})
    if asset.asset_status not in {"in_use", "idle"}:
        raise ValidationError("只有在用或闲置资产可执行换标。")
    old = AssetQrIdentity.objects.select_for_update().filter(asset=asset, status="active").first()
    if old is None or old.label_status != "attached":
        raise ValidationError("只有已贴标资产可执行换标。")
    if AssetLabelPrintItem.objects.filter(
        qr_identity=old,
        batch__status="generated",
        print_status="generated",
    ).exists():
        raise ValidationError(
            "当前二维码仍有未确认的打印批次，请先确认或取消该批次。"
        )
    now = timezone.now()
    _controlled_update(AssetQrIdentity, old.pk,
                       {"status": "revoked", "revoked_at": now, "revoked_by_id": actor.pk,
                        "revoke_reason": explanation}, "eam_lite.controlled_qr_identity_mutation")
    new = AssetQrIdentity(company=company, asset=asset, public_token=generate_public_token(),
                          status="active", label_status="ready_to_print", issued_by=actor,
                          version=old.version + 1)
    new.full_clean()
    _enable_capability("eam_lite.controlled_qr_identity_mutation")
    new.save()
    _audit(actor=actor, action="asset_qr.rotated", instance=new,
           old_data={"version": old.version, "status": "active"},
           new_data={"version": new.version, "status": "active", "reason": explanation}, request=request)
    return new


@transaction.atomic
def confirm_label_attachment(
    *,
    actor,
    asset,
    scanned_token,
    target_status=None,
    idempotency_key,
    confirmation_method="scan",
    request=None,
):
    from apps.assets.models import (
        Asset,
        AssetLabelAttachmentRequest,
        AssetLabelPrintBatch,
        AssetLabelPrintItem,
        AssetMovement,
        AssetQrIdentity,
    )
    company = _company()
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError({"idempotency_key": "确认贴标必须提供幂等键。"})
    method = str(confirmation_method or "").strip().casefold()
    if method not in {
        "scan",
        "scan_opaque_origin",
        "web",
        "web_opaque_origin",
    }:
        raise ValidationError({"confirmation_method": "贴标确认方式无效。"})
    asset = Asset.objects.select_for_update(of=("self",)).select_related(
        "department", "responsible_employee", "location"
    ).get(pk=asset.pk, company=company)
    require_label_action(actor, asset)
    qr_candidate = AssetQrIdentity.objects.filter(
        asset=asset,
        status="active",
    ).first()
    if qr_candidate is None or not secrets.compare_digest(
        qr_candidate.public_token,
        str(scanned_token or ""),
    ):
        raise PermissionDenied("所扫二维码不是该资产当前有效标签。")
    generated_batch_ids = AssetLabelPrintItem.objects.filter(
        qr_identity=qr_candidate,
        batch__status="generated",
        print_status="generated",
    ).values_list("batch_id", flat=True)
    generated_batches = list(
        AssetLabelPrintBatch.objects.select_for_update()
        .filter(
            company=company,
            pk__in=generated_batch_ids,
            status="generated",
        )
        .order_by("created_at", "pk")
    )
    generated_items = list(
        AssetLabelPrintItem.objects.select_for_update()
        .filter(batch__in=generated_batches)
        .order_by("batch_id", "position_no", "pk")
    )
    items_by_batch = {}
    for item in generated_items:
        items_by_batch.setdefault(item.batch_id, []).append(item)
    if any(
        len(items_by_batch.get(batch.pk, ())) != 1
        or items_by_batch[batch.pk][0].qr_identity_id != qr_candidate.pk
        or items_by_batch[batch.pk][0].print_status != "generated"
        for batch in generated_batches
    ):
        raise ValidationError(
            "当前二维码属于尚未确认的多资产打印批次，请先在打印批次页确认或取消。"
        )
    qr = AssetQrIdentity.objects.select_for_update().get(pk=qr_candidate.pk)
    if (
        qr.status != "active"
        or qr.asset_id != asset.pk
        or not secrets.compare_digest(qr.public_token, str(scanned_token or ""))
    ):
        raise PermissionDenied("所扫二维码不是该资产当前有效标签。")
    normalized_target = str(target_status or "")
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "asset_id": str(asset.pk),
                "qr_identity_id": str(qr.pk),
                "qr_version": qr.version,
                "target_status": normalized_target,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    existing_request = AssetLabelAttachmentRequest.objects.select_for_update().filter(
        company=company, idempotency_key=key
    ).first()
    if existing_request is not None:
        if (
            existing_request.asset_id != asset.pk
            or existing_request.qr_identity_id != qr.pk
            or existing_request.request_hash != request_hash
        ):
            raise ValidationError("相同幂等键已用于不同的贴标请求。")
        return qr
    existing = AssetMovement.objects.filter(
        company=company, idempotency_key=key
    ).first()
    if existing:
        if (
            existing.asset_id != asset.pk
            or existing.movement_type != "label_activation"
            or existing.to_status != target_status
            or qr.label_status != "attached"
            or asset.asset_status != target_status
            or qr.version > 1
            or AssetQrIdentity.objects.filter(
                asset=asset, status="revoked"
            ).exists()
        ):
            raise ValidationError("相同幂等键已用于其他资产或不同启用状态。")
        AssetLabelAttachmentRequest.objects.create(
            company=company,
            asset=asset,
            qr_identity=qr,
            idempotency_key=key,
            request_hash=request_hash,
            target_status=normalized_target,
            completed_by=actor,
        )
        return qr
    if qr.label_status == "attached" and asset.asset_status in {"in_use", "idle"}:
        raise ValidationError("该标签已经完成贴标；新请求不得冒充原幂等请求。")
    if qr.label_status != "printed":
        raise ValidationError("当前二维码尚未执行打印操作。")
    if not asset.asset_code:
        raise ValidationError("确认贴标前资产必须已有正式编号。")
    if not all((asset.department_id, asset.responsible_employee_id, asset.location_id)):
        raise ValidationError("确认贴标前必须补齐部门、责任人和位置。")
    now = timezone.now()
    for batch in generated_batches:
        item = items_by_batch[batch.pk][0]
        _controlled_update(
            AssetLabelPrintBatch,
            batch.pk,
            {"status": "cancelled"},
            "eam_lite.controlled_label_batch_mutation",
        )
        _controlled_update(
            AssetLabelPrintItem,
            item.pk,
            {"print_status": "cancelled"},
            "eam_lite.controlled_label_batch_mutation",
        )
        _audit(
            actor=actor,
            action="asset_label.print_cancelled",
            instance=batch,
            old_data={"status": "generated"},
            new_data={
                "status": "cancelled",
                "reason": "确认实际贴标时自动关闭未完成的单项打印预览",
                "automatic": True,
                "asset_id": str(asset.pk),
            },
            request=request,
        )
    if asset.asset_status == "pending_label":
        if target_status not in {"in_use", "idle"}:
            raise ValidationError({"target_status": "首次贴标只能选择在用或闲置。"})
        confirmation_reason = (
            "Web 端逐项确认首次贴标"
            if method in {"web", "web_opaque_origin"}
            else "现场扫码确认首次贴标"
        )
        movement = AssetMovement(
            company=company, asset=asset, movement_type="label_activation", effective_at=now,
            from_department=asset.department, to_department=asset.department,
            from_employee=asset.responsible_employee, to_employee=asset.responsible_employee,
            from_location=asset.location, to_location=asset.location,
            from_status="pending_label", to_status=target_status, reason=confirmation_reason,
            idempotency_key=key, operated_by=actor,
        )
        movement.full_clean()
        _enable_capability("eam_lite.controlled_asset_movement_insert")
        movement.save()
        _controlled_update(Asset, asset.pk, {"asset_status": target_status},
                           "eam_lite.controlled_asset_mutation")
        new_status = target_status
    elif asset.asset_status in {"in_use", "idle"}:
        new_status = asset.asset_status
    else:
        raise ValidationError("当前资产状态不允许确认贴标。")
    _controlled_update(AssetQrIdentity, qr.pk,
                       {"label_status": "attached", "attached_at": now, "attached_by_id": actor.pk},
                       "eam_lite.controlled_qr_identity_mutation")
    qr.refresh_from_db()
    AssetLabelAttachmentRequest.objects.create(
        company=company,
        asset=asset,
        qr_identity=qr,
        idempotency_key=key,
        request_hash=request_hash,
        target_status=normalized_target,
        completed_by=actor,
    )
    _audit(actor=actor, action="asset_label.attached", instance=asset,
           old_data={"asset_status": asset.asset_status, "label_status": "printed"},
           new_data={
               "asset_status": new_status,
               "label_status": "attached",
               "confirmation_method": method,
               "auto_cancelled_preview_batches": len(generated_batches),
           }, request=request)
    return qr
