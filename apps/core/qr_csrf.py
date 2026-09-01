"""Short-lived signed handoff for QR confirmation in opaque app browsers."""

from __future__ import annotations

from django.core import signing


QR_OPAQUE_ORIGIN_BRIDGE_SALT = "eam-lite.qr-opaque-origin-csrf"
QR_OPAQUE_ORIGIN_BRIDGE_MAX_AGE_SECONDS = 30 * 60


def build_qr_opaque_origin_bridge(*, user_id, session_key, path: str) -> str:
    if not user_id or not session_key or not str(path or "").startswith("/"):
        raise ValueError("二维码不透明来源桥接参数不完整。")
    return signing.dumps(
        {
            "user_id": str(user_id),
            "session_key": str(session_key),
            "path": str(path),
        },
        salt=QR_OPAQUE_ORIGIN_BRIDGE_SALT,
        compress=True,
    )


def validate_qr_opaque_origin_bridge(
    value,
    *,
    user_id,
    session_key,
    path: str,
) -> bool:
    try:
        payload = signing.loads(
            str(value or ""),
            salt=QR_OPAQUE_ORIGIN_BRIDGE_SALT,
            max_age=QR_OPAQUE_ORIGIN_BRIDGE_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("user_id") == str(user_id or "")
        and payload.get("session_key") == str(session_key or "")
        and payload.get("path") == str(path or "")
    )


__all__ = [
    "build_qr_opaque_origin_bridge",
    "validate_qr_opaque_origin_bridge",
]
