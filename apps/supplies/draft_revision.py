"""Signed browser revisions for an entire supply draft and all its persisted lines."""
import hashlib
import json
from datetime import date, datetime, time

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

from .models import SupplyDocumentLine

SUPPLY_EDIT_REVISION_SALT = "supplies.document-draft-edit-revision.v1"
SUPPLY_EDIT_REVISION_MAX_AGE = 24 * 60 * 60


def _persisted_fields(instance):
    return {field.attname: getattr(instance, field.attname) for field in instance._meta.concrete_fields}


class _RevisionEncoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return super().default(value)


def supply_document_revision_snapshot(document, *, lock_lines=False, persisted_lines=None):
    # Query actual persisted lines instead of using the caller's prefetch cache.
    if persisted_lines is None or lock_lines:
        lines = SupplyDocumentLine.objects.filter(document_id=document.pk).order_by("pk")
        if lock_lines:
            lines = lines.select_for_update()
    else:
        # Only the GET page supplies its explicit query, shared with rendering.
        lines = sorted(persisted_lines, key=lambda line: line.pk)
    payload = {"document": _persisted_fields(document),
               "lines": [_persisted_fields(line) for line in lines]}
    canonical = json.dumps(payload, cls=_RevisionEncoder, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def supply_edit_revision_token(*, actor, document, persisted_lines=None):
    return signing.dumps({
        "version": 1, "actor": str(actor.pk), "company": str(document.company_id),
        "document": str(document.pk), "revision": supply_document_revision_snapshot(
            document, persisted_lines=persisted_lines),
    }, salt=SUPPLY_EDIT_REVISION_SALT, compress=True)


def decode_supply_edit_revision(*, token, actor, company, document):
    # Only validate the signed identity here. The version comparison belongs
    # after the service's fresh row lock, not in form validation.
    try:
        payload = signing.loads(token, salt=SUPPLY_EDIT_REVISION_SALT, max_age=SUPPLY_EDIT_REVISION_MAX_AGE)
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("actor") != str(actor.pk)
                or payload.get("company") != str(company.pk)
                or payload.get("document") != str(document.pk)):
            raise signing.BadSignature
        revision = payload.get("revision")
        if (not isinstance(revision, str) or len(revision) != 64
                or any(character not in "0123456789abcdef" for character in revision)):
            raise signing.BadSignature
    except (signing.BadSignature, TypeError, ValueError) as exc:
        raise ValidationError("编辑页面版本无效或已过期，请重新打开最新草稿后核对。") from exc
    return revision
