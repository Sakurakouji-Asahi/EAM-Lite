"""Bounded selections and revision checks shared by bulk asset actions."""
import hashlib
import json
import uuid

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.serializers.json import DjangoJSONEncoder

from apps.assets.models import Asset

MAX_BULK_ASSETS = 200


def normalize_asset_selection(values):
    try:
        ids = list(dict.fromkeys(str(uuid.UUID(str(value))) for value in values))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("所选资产无效，请重新选择。") from exc
    if not 1 <= len(ids) <= MAX_BULK_ASSETS:
        raise ValidationError(f"每批请选择 1—{MAX_BULK_ASSETS} 件资产。")
    return ids


def asset_revision_snapshot(asset):
    payload = {field.attname: getattr(asset, field.attname) for field in asset._meta.concrete_fields}
    encoded = json.dumps(payload, cls=DjangoJSONEncoder, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def asset_validation_messages(exc):
    if hasattr(exc, "message_dict"):
        messages = []
        for field, errors in exc.message_dict.items():
            try:
                label = str(Asset._meta.get_field(field).verbose_name) + "："
            except FieldDoesNotExist:
                label = "" if field == "__all__" else "建档资料："
            messages.extend(label + str(error) for error in errors)
        return "；".join(messages)
    return "；".join(exc.messages)
