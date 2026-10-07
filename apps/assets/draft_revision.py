"""Signed browser revisions for persisted asset drafts and custom values."""
import hashlib
import json
from datetime import date, datetime, time

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

from .models import AssetCustomValue

ASSET_EDIT_REVISION_SALT = "assets.draft-edit-revision.v1"
ASSET_EDIT_REVISION_MAX_AGE = 24 * 60 * 60


class _RevisionEncoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return super().default(value)


def read_asset_draft_custom_values(asset):
    return list(AssetCustomValue.objects.filter(asset_id=asset.pk)
                .select_related("custom_field").order_by("custom_field_id", "pk"))


def _persisted_fields(instance):
    return {field.attname: getattr(instance, field.attname) for field in instance._meta.concrete_fields}


def asset_draft_revision_snapshot(asset, *, custom_values=None):
    # GET shares its captured rows with the displayed form. Service calls omit
    # this argument and query the current persisted rows after locking the asset.
    if custom_values is None:
        custom_values = read_asset_draft_custom_values(asset)
    payload = {"asset": _persisted_fields(asset),
               "custom_values": [_persisted_fields(value) for value in custom_values]}
    canonical = json.dumps(payload, cls=_RevisionEncoder, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def asset_edit_revision_token(*, actor, asset, custom_values=None):
    return signing.dumps({
        "version": 1, "actor": str(actor.pk), "company": str(asset.company_id),
        "asset": str(asset.pk),
        "revision": asset_draft_revision_snapshot(asset, custom_values=custom_values),
    }, salt=ASSET_EDIT_REVISION_SALT, compress=True)


def decode_asset_edit_revision(*, token, actor, company, asset):
    # Authentication of the page is separate from comparison under the fresh lock.
    try:
        payload = signing.loads(token, salt=ASSET_EDIT_REVISION_SALT,
                                max_age=ASSET_EDIT_REVISION_MAX_AGE)
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("actor") != str(actor.pk)
                or payload.get("company") != str(company.pk)
                or payload.get("asset") != str(asset.pk)):
            raise signing.BadSignature
        revision = payload.get("revision")
        if (not isinstance(revision, str) or len(revision) != 64
                or any(character not in "0123456789abcdef" for character in revision)):
            raise signing.BadSignature
    except (signing.BadSignature, TypeError, ValueError) as exc:
        raise ValidationError("编辑页面版本无效或已过期，请重新打开最新编辑页面后核对。") from exc
    return revision
