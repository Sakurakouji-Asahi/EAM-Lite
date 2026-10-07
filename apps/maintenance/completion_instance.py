"""Authenticate the exact maintenance instance displayed by a browser GET."""
from datetime import date

from django.core import signing
from django.core.exceptions import ValidationError

COMPLETION_INSTANCE_SALT = "maintenance.completion-instance.v1"
COMPLETION_INSTANCE_MAX_AGE = 24 * 60 * 60
INVALID_INSTANCE_MESSAGE = "保养页面实例无效或已过期，请重新打开完成页面后核对。当前输入已保留。"


def completion_instance_token(*, actor, plan, scheduled_date, require_current_instance=True):
    return signing.dumps({
        "version": 1, "actor": str(actor.pk), "company": str(plan.company_id),
        "plan": str(plan.pk), "scheduled_date": scheduled_date.isoformat(),
        "mode": "current" if require_current_instance else "redo",
    }, salt=COMPLETION_INSTANCE_SALT, compress=True)


def decode_completion_instance(*, token, actor, plan, require_current_instance=True, redo_date=None):
    try:
        payload = signing.loads(token, salt=COMPLETION_INSTANCE_SALT,
                                max_age=COMPLETION_INSTANCE_MAX_AGE)
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("actor") != str(actor.pk)
                or payload.get("company") != str(plan.company_id)
                or payload.get("plan") != str(plan.pk)
                or payload.get("mode") != ("current" if require_current_instance else "redo")):
            raise signing.BadSignature
        value = payload.get("scheduled_date")
        if not isinstance(value, str):
            raise signing.BadSignature
        captured = date.fromisoformat(value)
        if value != captured.isoformat() or (redo_date is not None and captured != redo_date):
            raise signing.BadSignature
    except (signing.BadSignature, TypeError, ValueError) as exc:
        raise ValidationError(INVALID_INSTANCE_MESSAGE) from exc
    return captured
