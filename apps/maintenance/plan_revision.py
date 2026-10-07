"""Signed revisions of the complete persisted maintenance plan shown for edit."""
import hashlib
import json
from datetime import date, datetime, time

from django.core import signing
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

PLAN_EDIT_REVISION_SALT = "maintenance.plan-edit-revision.v1"
PLAN_EDIT_REVISION_MAX_AGE = 24 * 60 * 60


class _RevisionEncoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        return super().default(value)


def maintenance_plan_revision_snapshot(plan):
    # Include derived completion dates and lifecycle fields: a completion,
    # reversal or status change can invalidate a page without touching updated_at.
    persisted = {field.attname: getattr(plan, field.attname)
                 for field in plan._meta.concrete_fields}
    canonical = json.dumps(persisted, cls=_RevisionEncoder, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_edit_revision_token(*, actor, plan):
    return signing.dumps({
        "version": 1, "actor": str(actor.pk), "company": str(plan.company_id),
        "plan": str(plan.pk), "revision": maintenance_plan_revision_snapshot(plan),
    }, salt=PLAN_EDIT_REVISION_SALT, compress=True)


def decode_plan_edit_revision(*, token, actor, company, plan):
    try:
        payload = signing.loads(token, salt=PLAN_EDIT_REVISION_SALT,
                                max_age=PLAN_EDIT_REVISION_MAX_AGE)
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("actor") != str(actor.pk)
                or payload.get("company") != str(company.pk)
                or payload.get("plan") != str(plan.pk)):
            raise signing.BadSignature
        revision = payload.get("revision")
        if (not isinstance(revision, str) or len(revision) != 64
                or any(character not in "0123456789abcdef" for character in revision)):
            raise signing.BadSignature
    except (signing.BadSignature, TypeError, ValueError) as exc:
        raise ValidationError("编辑页面版本无效或已过期，请重新打开最新编辑页面后核对。") from exc
    return revision
