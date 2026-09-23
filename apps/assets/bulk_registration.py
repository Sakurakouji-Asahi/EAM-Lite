"""Preview and register bounded selections, preserving per-asset idempotency."""
import uuid

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.assets.models import Asset, AssetRegistration
from apps.assets.permissions import scoped_assets_p1
from apps.assets.registration import lock_registration_company, require_asset_registration, register_asset
from apps.assets.bulk_support import (
    MAX_BULK_ASSETS, asset_revision_snapshot, asset_validation_messages, normalize_asset_selection,
)
from apps.assets.services import _require_current_company, _require_initialization_completed, _validate_submission
from apps.coding.preview import preview_next_asset_code

SALT = "assets.bulk-registration.v1"
MAX_BATCH = MAX_BULK_ASSETS


def _row_key(batch, pk):
    return f"bulk-register:{batch}:{pk}"


def registration_candidates(actor, company):
    qs = scoped_assets_p1(actor, company).filter(record_status="active", asset_status__in=("draft", "pending_finance"),
                                               current_issued_code__isnull=True)
    # Object permission is rechecked at preview and under the registration lock.
    return qs.select_related("company", "category", "department", "responsible_employee", "location", "requested_coding_scheme")


def preview_bulk_registration(*, actor, company, asset_ids):
    company = _require_current_company(company)
    _require_initialization_completed(company)
    ids = normalize_asset_selection(asset_ids)
    assets = {str(item.pk): item for item in scoped_assets_p1(actor, company).filter(pk__in=ids).select_related(
        "company", "category", "department", "responsible_employee", "location", "requested_coding_scheme")}
    if len(assets) != len(ids):
        raise PermissionDenied("所选资产包含当前账号无权查看的记录，请刷新列表。")
    rows, ready = [], []
    for pk in ids:
        asset = assets[pk]
        require_asset_registration(actor, asset)
        error = ""
        try:
            if asset.record_status != "active" or asset.current_issued_code_id or asset.asset_status not in {"draft", "pending_finance"}:
                raise ValidationError("该资产已建档或已归档，请刷新列表。")
            asset.full_clean()
            _validate_submission(asset)
            preview_next_asset_code(actor=actor, asset=asset)
        except ValidationError as exc:
            error = asset_validation_messages(exc)
        rows.append({"asset": asset, "error": error})
        if not error:
            ready.append({"id": pk, "snapshot": asset_revision_snapshot(asset)})
    payload = {"version": 1, "actor": str(actor.pk), "company": str(company.pk),
               "batch": str(uuid.uuid4()), "rows": ready}
    return {"rows": rows, "ready_count": len(ready), "error_count": len(rows)-len(ready),
            "token": signing.dumps(payload, salt=SALT, compress=True) if ready else ""}


def confirm_bulk_registration(*, actor, company, token, request=None):
    try:
        payload = signing.loads(token, salt=SALT, max_age=3600)
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("actor") != str(actor.pk) or payload.get("company") != str(company.pk)):
            raise signing.BadSignature
        batch = str(uuid.UUID(payload["batch"]))
        rows = payload["rows"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH:
            raise signing.BadSignature
        ids = [str(uuid.UUID(row["id"])) for row in rows]
        if len(ids) != len(set(ids)) or any(not isinstance(row.get("snapshot"), str) for row in rows):
            raise signing.BadSignature
    except (signing.BadSignature, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("建档预览已失效或不属于当前账号，请重新核对。") from exc
    results = []
    # A row has its own transaction: a rejected row does not undo valid rows.
    for row in rows:
        asset = None
        try:
            with transaction.atomic():
                locked_company = lock_registration_company(company)
                asset = scoped_assets_p1(actor, locked_company, Asset.objects.select_for_update(of=("self",))).select_related(
                    "company", "department", "category", "responsible_employee", "location", "requested_coding_scheme",
                ).get(pk=row["id"])
                require_asset_registration(actor, asset)
                key = _row_key(batch, asset.pk)
                replay = AssetRegistration.objects.filter(company=locked_company, asset=asset, idempotency_key=key).exists()
                if not replay and asset_revision_snapshot(asset) != row["snapshot"]:
                    raise ValidationError("预览后资料已变更，请重新核对后办理。")
                asset = register_asset(actor=actor, asset=asset, idempotency_key=key, request=request)
            results.append({"asset": asset, "error": ""})
        except (PermissionDenied, Asset.DoesNotExist):
            results.append({"asset": None, "error": "资产已不可用或当前账号已无办理权限。"})
        except ValidationError as exc:
            results.append({"asset": asset, "error": asset_validation_messages(exc)})
    return {"rows": results, "success_count": sum(not row["error"] for row in results),
            "error_count": sum(bool(row["error"]) for row in results)}
