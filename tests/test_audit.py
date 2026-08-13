import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError

from apps.audit.models import AuditLog
from apps.audit.services import (
    REDACTED,
    sanitize_audit_data,
    write_audit_log,
    write_system_audit_log,
)
from apps.core.logging import RedactingFormatter


pytestmark = pytest.mark.django_db


def test_audit_service_serializes_decimal_date_datetime_and_uuid():
    identifier = uuid4()
    occurred_at = datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)

    audit = write_system_audit_log(
        action="auth.pre_initialization",
        object_type="Example",
        object_id=identifier,
        old_data={"amount": Decimal("12.30"), "date": date(2026, 8, 11)},
        new_data={"id": identifier, "occurred_at": occurred_at},
    )
    audit.refresh_from_db()

    assert audit.object_id == str(identifier)
    assert audit.old_data_json == {"amount": "12.30", "date": "2026-08-11"}
    assert audit.new_data_json == {
        "id": str(identifier),
        "occurred_at": occurred_at.isoformat(),
    }


def test_audit_service_recursively_redacts_and_excludes_sensitive_fields():
    raw = {
        "username": "alice",
        "password": "plain-password",
        "nested": [
            {"api-token": "plain-token", "keep": "visible"},
            {"private_key": "plain-key", "omit_me": "excluded"},
        ],
    }

    sanitized = sanitize_audit_data(raw, excluded_fields={"omit_me"})

    assert sanitized["username"] == "alice"
    assert sanitized["password"] == REDACTED
    assert sanitized["nested"][0]["api-token"] == REDACTED
    assert sanitized["nested"][1]["private_key"] == REDACTED
    assert "omit_me" not in sanitized["nested"][1]
    assert "plain-password" not in repr(sanitized)
    assert "plain-token" not in repr(sanitized)
    assert "plain-key" not in repr(sanitized)


def test_audit_service_rejects_unsupported_and_circular_values():
    with pytest.raises(TypeError):
        sanitize_audit_data({"money": 1.5})

    circular = []
    circular.append(circular)
    with pytest.raises(ValueError):
        sanitize_audit_data(circular)


def test_audit_rejects_fake_company_value():
    with pytest.raises(ValueError):
        write_audit_log(
            action="test.company",
            object_type="Example",
            company="fake-company",
        )


def test_audit_rows_are_append_only():
    audit = write_system_audit_log(
        action="auth.pre_initialization", object_type="Example"
    )
    audit.action = "test.changed"

    with pytest.raises(ValidationError):
        audit.save()
    with pytest.raises(ValidationError):
        audit.delete()
    with pytest.raises(TypeError):
        AuditLog.objects.filter(pk=audit.pk).update(action="test.changed")
    with pytest.raises(TypeError):
        AuditLog.objects.filter(pk=audit.pk).delete()


def test_runtime_formatter_redacts_sensitive_assignments():
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "request password=plain token:secret-value "
            "\"authorization\": \"Bearer sensitive value\" safe=value "
            "GET /assets/scan/high-entropy-qr-token/?next=/assets/"
            " next=%2Fassets%2Fscan%2Fencoded-high-entropy-token%2F"
        ),
        args=(),
        exc_info=None,
    )
    output = RedactingFormatter("%(message)s").format(record)

    assert "plain" not in output
    assert "secret-value" not in output
    assert "Bearer sensitive value" not in output
    assert "high-entropy-qr-token" not in output
    assert "encoded-high-entropy-token" not in output
    assert "/assets/scan/[REDACTED]/?next=/assets/" in output
    assert "safe=value" in output
    assert output.count("[REDACTED]") == 5


def test_audit_service_redacts_sensitive_user_agent_assignments():
    audit = write_system_audit_log(
        action="auth.pre_initialization",
        object_type="Example",
        user_agent=(
            "ExampleBrowser password=plain-password token=plain-token safe=value"
        ),
    )
    audit.refresh_from_db()

    assert "plain-password" not in audit.user_agent
    assert "plain-token" not in audit.user_agent
    assert audit.user_agent.count(REDACTED) == 2
    assert "safe=value" in audit.user_agent


def test_user_agent_is_redacted_before_length_limit():
    audit = write_system_audit_log(
        action="auth.pre_initialization",
        object_type="Example",
        user_agent=("a" * 985) + " password=boundary-secret",
    )
    audit.refresh_from_db()

    assert "boundary" not in audit.user_agent
    assert "password=[REDA" in audit.user_agent
    assert len(audit.user_agent) <= 1000
