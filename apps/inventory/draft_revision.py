"""Signed browser revisions of the complete persisted task and assignees."""
import hashlib
import json
from datetime import date, datetime, time

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

from .models import InventoryTaskAssignee

INVENTORY_EDIT_REVISION_SALT = "inventory.task-draft-edit-revision.v1"
INVENTORY_EDIT_REVISION_MAX_AGE = 24 * 60 * 60


class _RevisionEncoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return super().default(value)


def _persisted_fields(instance):
    return {field.attname: getattr(instance, field.attname) for field in instance._meta.concrete_fields}


def inventory_task_revision_snapshot(task, *, persisted_assignees=None, lock_assignees=False):
    # Never use a caller's prefetch cache for the persisted relation version.
    if persisted_assignees is None or lock_assignees:
        assignees = InventoryTaskAssignee.objects.filter(inventory_task_id=task.pk).order_by("pk")
        if lock_assignees:
            assignees = assignees.select_for_update()
    else:
        # The GET page shares this explicit read with its form initial values.
        assignees = sorted(persisted_assignees, key=lambda row: row.pk)
    payload = {"task": _persisted_fields(task), "assignees": [_persisted_fields(row) for row in assignees]}
    encoded = json.dumps(payload, cls=_RevisionEncoder, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def inventory_edit_revision_token(*, actor, task, persisted_assignees=None):
    return signing.dumps({
        "version": 1, "actor": str(actor.pk), "company": str(task.company_id), "task": str(task.pk),
        "revision": inventory_task_revision_snapshot(task, persisted_assignees=persisted_assignees),
    }, salt=INVENTORY_EDIT_REVISION_SALT, compress=True)


def decode_inventory_edit_revision(*, token, actor, company, task):
    try:
        payload = signing.loads(token, salt=INVENTORY_EDIT_REVISION_SALT, max_age=INVENTORY_EDIT_REVISION_MAX_AGE)
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("actor") != str(actor.pk) or payload.get("company") != str(company.pk)
                or payload.get("task") != str(task.pk)):
            raise signing.BadSignature
        revision = payload.get("revision")
        if (not isinstance(revision, str) or len(revision) != 64
                or any(character not in "0123456789abcdef" for character in revision)):
            raise signing.BadSignature
    except (signing.BadSignature, TypeError, ValueError) as exc:
        raise ValidationError("编辑页面版本无效或已过期，请重新打开最新草稿后核对。") from exc
    return revision
