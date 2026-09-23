"""Maintain physical source/composition evidence without moving accounting values."""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.assets.domain import TERMINAL_ASSET_STATUSES
from apps.assets.services import _audit
from apps.assets.trace_support import (
    trace_request_fingerprint, validate_trace_request, lock_trace_assets, insert_trace_record,
)


@transaction.atomic
def record_asset_origin(*, actor, source, target, relation_type, reason, idempotency_key, request=None):
    from apps.assets.models import AssetOriginLink
    key, reason = validate_trace_request(idempotency_key, reason)
    company, assets = lock_trace_assets(actor, [source, target])
    source, target = assets[source.pk], assets[target.pk]
    if relation_type not in {"split", "merge"} or source.pk == target.pk:
        raise ValidationError("请选择不同的来源、目标资产和有效关系。")
    payload = {"source": str(source.pk), "target": str(target.pk), "type": relation_type, "reason": reason}
    fingerprint = trace_request_fingerprint(payload)
    existing = AssetOriginLink.objects.filter(company=company, idempotency_key=key).first()
    if existing:
        if existing.request_hash != fingerprint:
            raise ValidationError("相同请求已用于不同的来源记录。")
        return existing
    if target.record_status != "active" or target.asset_status in TERMINAL_ASSET_STATUSES:
        raise ValidationError("目标资产必须处于未归档的管理状态。")
    if AssetOriginLink.objects.filter(company=company, source_asset=source, target_asset=target, relation_type=relation_type, reversal__isnull=True).exists():
        raise ValidationError("该来源关系已经登记。")
    adjacency = {}
    for start, end in AssetOriginLink.objects.filter(company=company, reversal__isnull=True).values_list("source_asset_id", "target_asset_id"):
        adjacency.setdefault(start, set()).add(end)
    pending, seen = [target.pk], set()
    while pending:
        node = pending.pop()
        if node == source.pk:
            raise ValidationError("来源关系不能形成循环。")
        if node not in seen:
            seen.add(node)
            pending.extend(adjacency.get(node, ()))
    result = insert_trace_record(AssetOriginLink(company=company, source_asset=source, target_asset=target,
        source_issued_code=source.current_issued_code, target_issued_code=target.current_issued_code,
        relation_type=relation_type, reason=reason, idempotency_key=key, request_hash=fingerprint, recorded_by=actor))
    _audit(actor=actor, action="asset_origin_recorded", instance=result, new_data={**payload,
        "source_code": source.asset_code, "target_code": target.asset_code, "financial_values_transferred": False}, request=request)
    return result


@transaction.atomic
def reverse_asset_origin(*, actor, origin, reason, idempotency_key, request=None):
    from apps.assets.models import AssetOriginLink, AssetOriginReversal
    key, reason = validate_trace_request(idempotency_key, reason)
    stored = AssetOriginLink.objects.only("source_asset_id", "target_asset_id").get(pk=origin.pk)
    company, _ = lock_trace_assets(actor, [stored.source_asset_id, stored.target_asset_id])
    origin = AssetOriginLink.objects.select_for_update().get(pk=origin.pk, company=company)
    payload = {"origin": str(origin.pk), "reason": reason}
    fingerprint = trace_request_fingerprint(payload)
    existing = AssetOriginReversal.objects.filter(company=company, idempotency_key=key).first()
    if existing:
        if existing.request_hash != fingerprint:
            raise ValidationError("相同请求已用于不同的撤销登记。")
        return existing
    if AssetOriginReversal.objects.filter(origin=origin).exists():
        raise ValidationError("该来源登记已经撤销，请刷新页面。")
    result = insert_trace_record(AssetOriginReversal(company=company, origin=origin, reason=reason,
        idempotency_key=key, request_hash=fingerprint, recorded_by=actor),
        capability="eam_lite.controlled_asset_trace_reversal")
    _audit(actor=actor, action="asset_origin_reversed", instance=result, new_data=payload, request=request)
    return result


def normalize_members(members):
    if not isinstance(members, list) or not 1 <= len(members) <= 500:
        raise ValidationError("组合清单需要 1—500 行成员。")
    result = []
    for number, member in enumerate(members, 1):
        if not isinstance(member, dict) or set(member) - {"name", "quantity", "unit", "note"}:
            raise ValidationError(f"第 {number} 行格式无效。")
        name, unit, note = (str(member.get(field) or "").strip() for field in ("name", "unit", "note"))
        if not name or len(name) > 200 or not unit or len(unit) > 32 or len(note) > 500:
            raise ValidationError(f"第 {number} 行请填写成员名称、单位，并控制说明长度。")
        try:
            quantity = Decimal(str(member.get("quantity", "")))
            if not quantity.is_finite() or not Decimal("0") < quantity < Decimal("100000000000000") or quantity != quantity.quantize(Decimal("0.0001")):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValidationError(f"第 {number} 行数量须为正数，最多四位小数。") from None
        result.append({"name": name, "quantity": format(quantity, ".4f"), "unit": unit, "note": note})
    return result


@transaction.atomic
def record_asset_composition(*, actor, asset, members, reason, idempotency_key, request=None):
    from apps.assets.models import AssetCompositionRevision
    key, reason = validate_trace_request(idempotency_key, reason)
    members = normalize_members(members)
    company, assets = lock_trace_assets(actor, [asset])
    asset = assets[asset.pk]
    fingerprint = trace_request_fingerprint({"asset": str(asset.pk), "members": members, "reason": reason})
    existing = AssetCompositionRevision.objects.filter(company=company, idempotency_key=key).first()
    if existing:
        if existing.request_hash != fingerprint:
            raise ValidationError("相同请求已用于不同的组合清单。")
        return existing
    if asset.record_status != "active" or asset.asset_status in TERMINAL_ASSET_STATUSES:
        raise ValidationError("已归档或结束管理的资产不能修改组合成员。")
    previous = asset.composition_revisions.first()
    result = insert_trace_record(AssetCompositionRevision(company=company, asset=asset, members=members,
        revision=previous.revision + 1 if previous else 1, previous_revision=previous, reason=reason,
        idempotency_key=key, request_hash=fingerprint, recorded_by=actor))
    _audit(actor=actor, action="asset_composition_recorded", instance=result,
           old_data={"revision": previous.revision if previous else None},
           new_data={"asset": str(asset.pk), "revision": result.revision, "members": members,
                     "financial_values_changed": False, "supply_stock_changed": False}, request=request)
    return result
