from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from apps.audit.models import AuditLog
from apps.core.logging import redact_log_text


REDACTED = "[REDACTED]"
_SENSITIVE_NAME_PARTS = (
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
)


def _normalize_field_name(value):
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_field(name):
    normalized = _normalize_field_name(name)
    return any(part in normalized for part in _SENSITIVE_NAME_PARTS)


def sanitize_audit_data(value, *, excluded_fields=None):
    excluded = {
        _normalize_field_name(field) for field in (excluded_fields or ())
    }
    return _sanitize_value(value, excluded=excluded, active_containers=set())


def _sanitize_value(value, *, excluded, active_containers):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (Decimal, UUID, date, datetime, time)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("审计数据不能包含循环引用")
        active_containers.add(identity)
        try:
            result = {}
            for key, item in value.items():
                normalized_key = _normalize_field_name(key)
                if normalized_key in excluded:
                    continue
                result[str(key)] = (
                    REDACTED
                    if _is_sensitive_field(key)
                    else _sanitize_value(
                        item,
                        excluded=excluded,
                        active_containers=active_containers,
                    )
                )
            return result
        finally:
            active_containers.remove(identity)

    if isinstance(value, (list, tuple, set, frozenset)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("审计数据不能包含循环引用")
        active_containers.add(identity)
        try:
            items = value
            if isinstance(value, (set, frozenset)):
                items = sorted(value, key=repr)
            return [
                _sanitize_value(
                    item,
                    excluded=excluded,
                    active_containers=active_containers,
                )
                for item in items
            ]
        finally:
            active_containers.remove(identity)

    raise TypeError(f"不支持写入审计 JSON 的类型：{type(value).__name__}")


def request_audit_context(request):
    if request is None:
        return {"ip_address": None, "user_agent": ""}
    return {
        "ip_address": request.META.get("REMOTE_ADDR") or None,
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
    }


def write_audit_log(
    *,
    action,
    object_type,
    object_id="",
    user=None,
    old_data=None,
    new_data=None,
    ip_address=None,
    user_agent="",
    correlation_id=None,
    excluded_fields=None,
    company=None,
):
    """
    追加一条审计记录。

    Sprint 1 将 company 参数替换为真实的 Company PROTECT/NULL 外键；Sprint 0
    仅允许 company=None 的预初始化系统事件，禁止文本公司标识。
    """
    if company is not None:
        raise ValueError("Sprint 0 尚未建立 Company，审计 company 必须为 None")
    if not str(action).strip() or not str(object_type).strip():
        raise ValueError("action 和 object_type 不得为空")

    values = {
        "user": user,
        "action": str(action).strip(),
        "object_type": str(object_type).strip(),
        "object_id": "" if object_id is None else str(object_id),
        "old_data_json": sanitize_audit_data(
            {} if old_data is None else old_data,
            excluded_fields=excluded_fields,
        ),
        "new_data_json": sanitize_audit_data(
            {} if new_data is None else new_data,
            excluded_fields=excluded_fields,
        ),
        "ip_address": ip_address,
        "user_agent": redact_log_text(user_agent or "")[:1000],
    }
    if correlation_id is not None:
        values["correlation_id"] = correlation_id
    return AuditLog.objects.create(**values)
