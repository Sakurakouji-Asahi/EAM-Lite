from apps.core.qr_csrf import (
    build_qr_opaque_origin_bridge,
    validate_qr_opaque_origin_bridge,
)


def test_qr_opaque_origin_bridge_is_bound_to_user_session_and_path():
    bridge = build_qr_opaque_origin_bridge(
        user_id=7,
        session_key="session-a",
        path="/assets/example/labels/confirm/",
    )

    assert validate_qr_opaque_origin_bridge(
        bridge,
        user_id=7,
        session_key="session-a",
        path="/assets/example/labels/confirm/",
    )
    assert not validate_qr_opaque_origin_bridge(
        bridge,
        user_id=8,
        session_key="session-a",
        path="/assets/example/labels/confirm/",
    )
    assert not validate_qr_opaque_origin_bridge(
        bridge,
        user_id=7,
        session_key="session-b",
        path="/assets/example/labels/confirm/",
    )
    assert not validate_qr_opaque_origin_bridge(
        bridge,
        user_id=7,
        session_key="session-a",
        path="/logout/",
    )
    assert not validate_qr_opaque_origin_bridge(
        bridge + "tampered",
        user_id=7,
        session_key="session-a",
        path="/assets/example/labels/confirm/",
    )
