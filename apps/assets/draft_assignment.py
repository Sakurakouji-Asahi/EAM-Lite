"""Preview and apply one audited, atomic assignment change to asset drafts."""
import uuid

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.assets.bulk_support import (
    asset_revision_snapshot, asset_validation_messages, normalize_asset_selection,
)
from apps.assets.models import Asset
from apps.assets.permissions import can_create_asset_draft, require_edit_asset_draft, scoped_assets_p1
from apps.assets.registration import lock_registration_company
from apps.assets.services import update_asset_draft
from apps.audit.models import AuditLog
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.models import Department, Employee, InitializationSetting, Location
from apps.masterdata.permissions import current_company

SALT = "assets.bulk-draft-assignment.v1"
AUDIT_ACTION = "asset_bulk_assignment"
ASSIGNMENT_FIELDS = {
    "department": (Department, "部门"),
    "responsible_employee": (Employee, "责任人"),
    "location": (Location, "位置"),
}


def _company(company):
    current = current_company()
    if current is None or current.pk != getattr(company, "pk", None):
        raise PermissionDenied("目标记录不属于当前公司。")
    if not InitializationSetting.objects.filter(company=current, initialization_completed=True).exists():
        raise PermissionDenied("系统初始化尚未完成。")
    return current


def _selection(actor, company, ids, *, lock=False):
    queryset = Asset.objects.select_for_update(of=("self",)) if lock else Asset.objects.all()
    rows = list(scoped_assets_p1(actor, company, queryset).filter(pk__in=ids).select_related(
        "company", "category", "department", "responsible_employee", "location", "requested_coding_scheme",
    ).order_by("pk"))
    if len(rows) != len(ids):
        raise PermissionDenied("所选资产包含当前账号无权查看的记录，请重新选择。")
    by_id = {str(row.pk): row for row in rows}
    return [by_id[pk] for pk in ids]


def _values(company, data):
    if not isinstance(data, dict) or set(data) - ASSIGNMENT_FIELDS.keys():
        raise ValidationError("批量补资料只支持部门、责任人和位置。")
    result = {}
    for name, value in data.items():
        if value in (None, ""):
            continue
        model, label = ASSIGNMENT_FIELDS[name]
        choices = model.objects.filter(company=company, is_active=True)
        if name == "responsible_employee":
            choices = choices.filter(employment_status="active", department__is_active=True)
        elif name == "location":
            choices = choices.filter(children__isnull=True)
        try:
            result[name] = choices.get(pk=int(str(getattr(value, "pk", value))))
        except (ValueError, TypeError, OverflowError, model.DoesNotExist) as exc:
            raise ValidationError({name: f"{label}不可用，请重新选择。"}) from exc
    if not result:
        raise ValidationError("请至少选择一个要补充的字段；留空的字段保持原值。")
    return result


def _reason(value):
    reason = str(value or "").strip()
    if not reason or len(reason) > 500:
        raise ValidationError("请填写 1—500 字的补充说明。")
    return reason


def _prepare_asset(actor, asset, values):
    require_edit_asset_draft(actor, asset)
    if asset.record_status != "active" or asset.current_issued_code_id is not None:
        raise ValidationError("批量补资料仅适用于未归档、未正式建档的草稿。")
    for name, value in values.items():
        setattr(asset, name, value)
    if not can_create_asset_draft(actor, asset.company, asset.department):
        raise PermissionDenied("您没有把资产改挂到目标部门的权限。")
    asset.full_clean()


def preview_draft_assignment(*, actor, company, asset_ids, data, reason):
    company = _company(company)
    ids = normalize_asset_selection(asset_ids)
    values, reason = _values(company, data), _reason(reason)
    rows, snapshots = [], []
    for asset in _selection(actor, company, ids):
        before = {name: str(getattr(asset, name) or "未填写") for name in values}
        snapshots.append({"id": str(asset.pk), "snapshot": asset_revision_snapshot(asset)})
        error = ""
        try:
            _prepare_asset(actor, asset, values)
        except ValidationError as exc:
            error = asset_validation_messages(exc)
        rows.append({"asset": asset, "changes": [
            {"label": ASSIGNMENT_FIELDS[name][1], "before": before[name], "after": str(value)}
            for name, value in values.items()
        ], "error": error})
    error_count = sum(bool(row["error"]) for row in rows)
    payload = {"version": 1, "actor": str(actor.pk), "company": str(company.pk),
        "batch": str(uuid.uuid4()), "rows": snapshots,
        "values": {name: value.pk for name, value in values.items()}, "reason": reason}
    return {"rows": rows, "error_count": error_count, "count": len(rows), "reason": reason,
        "token": "" if error_count else signing.dumps(payload, salt=SALT, compress=True)}


def _load_preview(actor, company, token):
    try:
        payload = signing.loads(token, salt=SALT, max_age=3600)
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("actor") != str(actor.pk) or payload.get("company") != str(company.pk)):
            raise signing.BadSignature
        payload["batch"] = str(uuid.UUID(payload["batch"]))
        ids = normalize_asset_selection([row["id"] for row in payload["rows"]])
        if len(ids) != len(payload["rows"]) or any(not isinstance(row["snapshot"], str) for row in payload["rows"]):
            raise signing.BadSignature
        return payload, ids
    except (signing.BadSignature, ValidationError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("补资料预览已失效或不属于当前账号，请重新核对。") from exc


@transaction.atomic
def confirm_draft_assignment(*, actor, company, token, request=None):
    company = lock_registration_company(_company(company))
    payload, ids = _load_preview(actor, company, token)
    assets = _selection(actor, company, ids, lock=True)
    # Company then asset locks serialize repeated submissions. The immutable
    # completion receipt and per-asset audit entries commit with the updates.
    existing = AuditLog.objects.filter(company=company, user=actor, action=AUDIT_ACTION,
        object_type="AssetBulkAssignment", object_id=payload["batch"]).first()
    if existing:
        return {"count": len(ids), "asset_ids": ids, "replayed": True}
    snapshots = {row["id"]: row["snapshot"] for row in payload["rows"]}
    values, reason = _values(company, payload["values"]), _reason(payload["reason"])
    for asset in assets:
        if asset_revision_snapshot(asset) != snapshots[str(asset.pk)]:
            raise ValidationError(f"{asset.draft_number} 的资料在预览后发生变化，本批未保存，请重新核对。")
        _prepare_asset(actor, asset, values)
    for asset in assets:
        update_asset_draft(actor=actor, asset=asset, data=values, request=request)
    write_business_audit_log(company=company, user=actor, action=AUDIT_ACTION,
        object_type="AssetBulkAssignment", object_id=payload["batch"], old_data={},
        new_data={"asset_ids": ids, "assignment": payload["values"], "reason": reason},
        **request_audit_context(request))
    return {"count": len(ids), "asset_ids": ids, "replayed": False}
