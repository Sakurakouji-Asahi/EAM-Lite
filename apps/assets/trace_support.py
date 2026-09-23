"""Shared transactional primitives for physical origin and custody records."""
import hashlib
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from apps.assets.permissions import can_create_asset_draft, can_view_asset_p1
from apps.masterdata.permissions import current_company


def trace_request_fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_trace_request(key, reason):
    key, reason = str(key or "").strip(), str(reason or "").strip()
    if not key or len(key) > 128 or not reason or len(reason) > 1000:
        raise ValidationError("请刷新页面并填写 1—1000 字的依据说明。")
    return key, reason


def lock_trace_assets(actor, assets):
    from apps.assets.models import Asset
    from apps.masterdata.models import Company
    company = current_company()
    if company is None:
        raise PermissionDenied("尚未配置当前公司。")
    company = Company.objects.select_for_update().get(pk=company.pk)
    ids = {getattr(asset, "pk", asset) for asset in assets}
    rows = list(Asset.objects.select_for_update(of=("self",)).select_related("company", "department", "current_issued_code")
                .filter(company=company, pk__in=ids).order_by("pk"))
    if len(rows) != len(ids) or any(not can_view_asset_p1(actor, row)
            or not can_create_asset_draft(actor, company, row.department) for row in rows):
        raise PermissionDenied("无权维护所选资产的来源或组合资料。")
    if any(row.current_issued_code_id is None for row in rows):
        raise ValidationError("请先为参与资产建立正式编号。")
    return company, {row.pk: row for row in rows}


def insert_trace_record(record, *, capability="eam_lite.controlled_asset_trace"):
    record.full_clean()
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s,'on',true)", [capability])
    record.save()
    return record
