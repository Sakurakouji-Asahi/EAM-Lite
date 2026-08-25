from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from apps.audit.models import AuditLog
from apps.core.logging import redact_log_text


REDACTED = "[REDACTED]"
PRE_INITIALIZATION_ACTIONS = frozenset(
    {
        "auth.login_succeeded",
        "auth.login_failed",
        "account.bootstrap_created",
        "auth.pre_initialization",
    }
)
SENSITIVE_AUDIT_FIELD_PARTS = (
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


def _normalize_field_name(value):
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_field(name):
    normalized = _normalize_field_name(name)
    return any(part in normalized for part in SENSITIVE_AUDIT_FIELD_PARTS)


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
        return {"ip_address": None, "user_agent": "", "correlation_id": None}
    return {
        "ip_address": getattr(
            request,
            "client_ip_address",
            request.META.get("REMOTE_ADDR") or None,
        ),
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
        "correlation_id": getattr(request, "correlation_id", None),
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
    _allow_pre_initialization=False,
):
    """
    追加一条审计记录。

    The generic API always requires a real Company. The narrow
    ``write_system_audit_log`` wrapper is the only path that may omit it for
    fixed Sprint 0 bootstrap/authentication events before Company exists.
    """
    if (
        company is not None
        and getattr(getattr(company, "_meta", None), "label", None)
        != "masterdata.Company"
    ):
        raise ValueError("company 必须是真实的 Company 实例")
    if not str(action).strip() or not str(object_type).strip():
        raise ValueError("action 和 object_type 不得为空")
    normalized_action = str(action).strip()
    if company is None:
        if not _allow_pre_initialization:
            raise ValueError("通用审计事件必须关联真实 Company")
        if normalized_action not in PRE_INITIALIZATION_ACTIONS:
            raise ValueError("company=None 只允许固定的预初始化系统事件")
        try:
            from apps.masterdata.models import Company
        except (ImportError, RuntimeError):
            Company = None
        if Company is not None:
            # During a real Sprint 0 -> Sprint 1 migration the model may be
            # importable before its table exists.  That is still genuinely
            # pre-initialization and must not break authentication auditing.
            from django.db import connection

            if (
                Company._meta.db_table in connection.introspection.table_names()
                and Company.objects.exists()
            ):
                raise ValueError("Company 已建立，审计事件必须关联真实 Company")

    values = {
        "company": company,
        "user": user,
        "action": normalized_action,
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


def write_system_audit_log(*, action, company=None, **kwargs):
    """Write a fixed authentication/bootstrap event, including pre-init."""
    if str(action).strip() not in PRE_INITIALIZATION_ACTIONS:
        raise ValueError("不允许的系统审计事件")
    return write_audit_log(
        action=action,
        company=company,
        _allow_pre_initialization=company is None,
        **kwargs,
    )


def write_business_audit_log(*, company, **kwargs):
    """写入必须具备公司边界的业务审计事件。"""
    if company is None:
        raise ValueError("业务审计事件必须关联 Company")
    return write_audit_log(company=company, **kwargs)
