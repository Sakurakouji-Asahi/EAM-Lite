"""Safe AuditLog querying and response projection."""

from __future__ import annotations

import json
from collections.abc import Mapping

from apps.audit.permissions import FINANCE_AUDIT_OBJECT_TYPES, scoped_audit_logs
from apps.audit.services import REDACTED
from apps.core.logging import redact_log_text
from apps.masterdata.permissions import role_names_for


_ALWAYS_REDACT_PARTS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "authorization",
    "cookie",
    "session",
    "csrf",
    "api_key",
    "apikey",
    "private_key",
    "file_content",
    "file_contents",
    "content_bytes",
    "binary_content",
    "file_blob",
    "file_body",
)

_FINANCE_FIELD_PARTS = (
    "accounting_treatment",
    "fixed_asset_category",
    "recognition_threshold",
    "original_cost",
    "capitalization",
    "depreciation",
    "salvage",
    "useful_life",
    "opening_book_value",
    "book_value",
    "impairment",
    "finance_remark",
    "financial_snapshot",
    "disposal_income",
    "external_system",
    "reference_type",
    "reference_value",
    "normalized_value",
    "output_sha256",
    "output_attachment",
)

_ATTACHMENT_OBJECT_TYPES = frozenset({"Attachment", "AttachmentLink"})


def _normalize_key(value) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _contains_part(name, parts) -> bool:
    normalized = _normalize_key(name)
    return any(part in normalized for part in parts)


def _redact_nested(value, *, can_view_finance):
    if isinstance(value, Mapping):
        if any(
            _normalize_key(key) == "security_class"
            and str(item).strip().upper() == "A1"
            for key, item in value.items()
        ):
            return {"已脱敏": REDACTED}
        projected = {}
        for key, item in value.items():
            if _contains_part(key, _ALWAYS_REDACT_PARTS) or (
                not can_view_finance and _contains_part(key, _FINANCE_FIELD_PARTS)
            ):
                projected[str(key)] = REDACTED
            else:
                projected[str(key)] = _redact_nested(
                    item, can_view_finance=can_view_finance
                )
        return projected
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _redact_nested(item, can_view_finance=can_view_finance)
            for item in value
        ]
    if isinstance(value, str):
        return redact_log_text(value)
    return value


def redact_audit_payload(payload, *, user, object_type):
    """Apply current field permissions again at read time.

    Audit rows are immutable historical evidence; this projection deliberately
    does not trust the sanitization policy that was in force when a row was
    written.
    """

    roles = role_names_for(user)
    can_view_finance = "finance" in roles
    if not can_view_finance and object_type in FINANCE_AUDIT_OBJECT_TYPES:
        return {"已脱敏": REDACTED}
    if object_type in _ATTACHMENT_OBJECT_TYPES:
        return {"已脱敏": REDACTED}
    return _redact_nested(payload, can_view_finance=can_view_finance)


def apply_audit_filters(queryset, filters):
    """Apply only validated exact filters after the role scope."""

    queryset = queryset.filter(
        created_at__gte=filters["start_at"],
        created_at__lte=filters["end_at"],
    )
    if filters.get("actor") is not None:
        queryset = queryset.filter(user=filters["actor"])
    for field in ("action", "object_type", "object_id", "correlation_id"):
        value = filters.get(field)
        if value not in (None, ""):
            queryset = queryset.filter(**{field: value})
    return queryset


def audit_log_queryset(*, user, company, filters):
    return apply_audit_filters(
        scoped_audit_logs(user, company).select_related("user"), filters
    ).order_by("-created_at", "-pk")


def visible_audit_actors(*, user, company):
    from django.contrib.auth import get_user_model

    visible_user_ids = scoped_audit_logs(user, company).exclude(
        user_id=None
    ).values("user_id")
    return get_user_model().objects.filter(pk__in=visible_user_ids).order_by(
        "username"
    )


def project_audit_log(log, *, user):
    old_data = redact_audit_payload(
        log.old_data_json, user=user, object_type=log.object_type
    )
    new_data = redact_audit_payload(
        log.new_data_json, user=user, object_type=log.object_type
    )
    return {
        "created_at": log.created_at,
        "actor": (
            (log.user.display_name or log.user.username) if log.user else "系统/已停用用户"
        ),
        "action": log.action,
        "object_type": log.object_type,
        "object_id": log.object_id,
        "old_data": json.dumps(old_data, ensure_ascii=False, indent=2, default=str),
        "new_data": json.dumps(new_data, ensure_ascii=False, indent=2, default=str),
        "correlation_id": log.correlation_id,
    }


__all__ = [
    "apply_audit_filters",
    "audit_log_queryset",
    "project_audit_log",
    "redact_audit_payload",
    "visible_audit_actors",
]
