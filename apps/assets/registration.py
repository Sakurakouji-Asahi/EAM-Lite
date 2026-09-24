"""Register physical assets and issue their identity without a finance review."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils import timezone

from apps.assets.models import Asset, AssetQrIdentity, AssetRegistration
from apps.assets.permissions import can_create_asset_draft, require_view_asset
from apps.assets.services import (
    _audit,
    _controlled_update,
    _require_current_company,
    _require_initialization_completed,
    _validate_submission,
    create_asset_draft,
)
from apps.coding.issuance import _issue_asset_code
from apps.masterdata.models import Company


def _key(value):
    result = str(value or "").strip()
    if not result or len(result) > 200:
        raise ValidationError({"idempotency_key": "请刷新建档页面后重试。"})
    return result


def _reject_reversed_key(company, key):
    from apps.audit.undo import registration_key_was_undone
    if registration_key_was_undone(company, key):
        raise ValidationError("该建档请求已撤销，请刷新页面后重新建档。")


def _normalized(value):
    if isinstance(value, Mapping):
        return {str(key): _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if hasattr(value, "pk"):
        return str(value.pk)
    return value


def _fingerprint(payload):
    encoded = json.dumps(
        _normalized(payload), cls=DjangoJSONEncoder,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def lock_registration_company(company):
    current = _require_current_company(company)
    return Company.objects.select_for_update().get(pk=current.pk)


def _lock_asset(asset, company):
    queryset = Asset.objects.select_for_update()
    if connection.vendor == "postgresql":
        queryset = queryset.select_for_update(of=("self",))
    try:
        return queryset.select_related(
            "company", "category", "department", "responsible_employee", "location",
            "requested_coding_scheme",
        ).get(pk=asset.pk, company=company)
    except Asset.DoesNotExist as exc:
        raise PermissionDenied("目标资产不存在或不属于当前公司。") from exc


def require_asset_registration(actor, asset):
    require_view_asset(actor, asset)
    if not can_create_asset_draft(actor, asset.company, asset.department):
        raise PermissionDenied("您没有办理此资产实物建档的权限。")


def _replay(existing, *, actor, fingerprint, asset=None):
    require_asset_registration(actor, existing.asset)
    if (asset is not None and existing.asset_id != asset.pk) or existing.request_hash != fingerprint:
        raise ValidationError("同一建档请求已用于其他资产或不同资料，请刷新后核对。")
    return existing.asset


def _register_locked_asset(
    *, actor, asset, idempotency_key, request_hash, code_effective_date=None,
    code_effective_reason="", source=AssetRegistration.Source.PHYSICAL,
    issue_code=_issue_asset_code, request=None,
):
    """Internal transaction step; callers hold Company then Asset locks."""
    if not connection.in_atomic_block:
        raise RuntimeError("实物建档必须在数据库事务中完成。")
    _require_initialization_completed(asset.company)
    require_asset_registration(actor, asset)
    if asset.asset_status not in {"draft", "pending_finance"} or asset.current_issued_code_id:
        raise ValidationError("该资产已建立正式档案，不能重复发号。")
    asset.full_clean()
    _validate_submission(asset)
    effective_date = code_effective_date or timezone.localdate()
    if not isinstance(effective_date, date):
        raise ValidationError({"code_effective_date": "编号生效日期格式无效。"})
    if effective_date > timezone.localdate():
        raise ValidationError({"code_effective_date": "编号生效日期不能晚于今天。"})
    reason = str(code_effective_reason or "").strip()
    if effective_date < timezone.localdate() and not reason:
        raise ValidationError({"code_effective_reason": "历史编号日期必须填写原因。"})
    registry_key = (
        idempotency_key if source == AssetRegistration.Source.LEGACY_FINANCE
        else "registration:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
    )
    issued = issue_code(
        actor=actor, asset=asset, effective_date=effective_date,
        reason=reason, idempotency_key=registry_key,
    )
    previous_status = asset.asset_status
    now = timezone.now()
    _controlled_update(Asset, pk=asset.pk, values={
        "asset_code": issued.display_code,
        "current_issued_code_id": issued.pk,
        "asset_status": Asset.AssetStatus.PENDING_LABEL,
        "submitted_by_id": asset.submitted_by_id or actor.pk,
        "submitted_at": asset.submitted_at or now,
        "updated_by_id": actor.pk,
        "updated_at": now,
    })
    asset.refresh_from_db()
    qr = AssetQrIdentity.objects.create(
        company=asset.company, asset=asset, public_token=secrets.token_urlsafe(32),
        status="active", label_status="ready_to_print",
        issued_at=timezone.now(), issued_by=actor, version=1,
    )
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('eam_lite.controlled_asset_registration', 'on', true)")
    AssetRegistration.objects.create(
        company=asset.company, asset=asset, result_issued_code=issued,
        idempotency_key=idempotency_key, request_hash=request_hash,
        source=source, registered_by=actor, registered_at=now,
    )
    identity_values = getattr(issued, "_identity_values", None)
    if identity_values is not None:
        from apps.coding.standard_issuance import _save_identity
        _save_identity(asset=asset, issued=issued, values=identity_values)
    _audit(actor=actor, action="asset_register", instance=asset,
           old_data={"asset_status": previous_status},
           new_data={"asset_status": asset.asset_status, "asset_code": asset.asset_code,
                     "source": source}, request=request)
    _audit(actor=actor, action="asset_code_issue", instance=asset,
           new_data={"issued_code_id": str(issued.pk), "asset_code": issued.display_code}, request=request)
    _audit(actor=actor, action="asset_qr_identity_create", instance=asset,
           new_data={"qr_identity_id": str(qr.pk), "label_status": "ready_to_print"}, request=request)
    return asset


@transaction.atomic
def create_registered_asset(
    *, actor, company, data, custom_values=None, idempotency_key, request=None,
):
    company = lock_registration_company(company)
    normalized_key = _key(idempotency_key)
    _reject_reversed_key(company, normalized_key)
    fingerprint = _fingerprint({"operation": "create", "data": data, "custom_values": custom_values or {}})
    existing = AssetRegistration.objects.select_related("asset__company", "asset__department").filter(
        company=company, idempotency_key=normalized_key,
    ).first()
    if existing is not None:
        return _replay(existing, actor=actor, fingerprint=fingerprint)
    asset = create_asset_draft(
        actor=actor, company=company, data=data, custom_values=custom_values, request=request,
    )
    return _register_locked_asset(
        actor=actor, asset=asset, idempotency_key=normalized_key,
        request_hash=fingerprint, request=request,
    )


@transaction.atomic
def register_asset(
    *, actor, asset, idempotency_key, code_effective_date=None,
    code_effective_reason="", request=None,
):
    company = lock_registration_company(asset.company)
    asset = _lock_asset(asset, company)
    normalized_key = _key(idempotency_key)
    _reject_reversed_key(company, normalized_key)
    fingerprint = _fingerprint({
        "operation": "register", "asset": str(asset.pk),
        "code_effective_date": code_effective_date,
        "code_effective_reason": str(code_effective_reason or "").strip(),
    })
    existing = AssetRegistration.objects.select_related("asset__company", "asset__department").filter(
        company=company, idempotency_key=normalized_key,
    ).first()
    if existing is not None:
        return _replay(existing, actor=actor, fingerprint=fingerprint, asset=asset)
    return _register_locked_asset(
        actor=actor, asset=asset, idempotency_key=normalized_key, request_hash=fingerprint,
        code_effective_date=code_effective_date, code_effective_reason=code_effective_reason,
        request=request,
    )
