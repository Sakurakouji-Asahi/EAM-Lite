from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.masterdata.models import (
    AssetCategory,
    AssetCodingScheme,
    AssetCodingSegment,
    IssuedCode,
    SequenceCounter,
)
from tests.test_sprint2_support import make_company, make_user


pytestmark = pytest.mark.django_db

SOURCE_TYPES = (
    "company_code",
    "major_category_code",
    "minor_category_code",
    "category_code",
    "department_code",
    "year",
    "year_month",
    "full_date",
)
ALL_TYPES = ("fixed_text", *SOURCE_TYPES, "sequence", "custom_text", "separator")


def direct_scheme(company, *, key="DB", version=1, **overrides):
    values = {
        "company": company,
        "name": f"{key} v{version}",
        "scheme_key": key,
        "version": version,
        "status": "draft",
        "is_default": False,
        "reset_mode": "never",
        "sequence_start": 1,
        "category_scope_level": None,
        "effective_from": timezone.localdate(),
        "effective_to": None,
    }
    values.update(overrides)
    return AssetCodingScheme.objects.create(**values)


def segment_values(segment_type, **overrides):
    values = {
        "sequence_order": 1,
        "segment_type": segment_type,
        "fixed_value": None,
        "format_string": None,
        "sequence_length": None,
        "zero_pad": None,
    }
    if segment_type in {"fixed_text", "custom_text"}:
        values["fixed_value"] = "CONST"
    elif segment_type == "separator":
        values["fixed_value"] = "-"
    elif segment_type == "sequence":
        values.update(sequence_length=4, zero_pad=True)
    values.update(overrides)
    return values


def assert_db_rejects_create(scheme, segment_type, **overrides):
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodingSegment.objects.create(
            coding_scheme=scheme,
            **segment_values(segment_type, **overrides),
        )


@pytest.mark.parametrize("segment_type", ALL_TYPES)
def test_database_accepts_each_legal_segment_field_combination(segment_type):
    company = make_company()
    scheme = direct_scheme(company, key=f"LEGAL-{segment_type}")

    saved = AssetCodingSegment.objects.create(
        coding_scheme=scheme, **segment_values(segment_type)
    )

    assert saved.pk is not None


@pytest.mark.parametrize("segment_type", SOURCE_TYPES)
@pytest.mark.parametrize(
    "extra",
    (
        {"fixed_value": "EXTRA"},
        {"sequence_length": 4},
        {"zero_pad": False},
    ),
)
def test_database_rejects_every_extra_field_on_source_and_date_segments(
    segment_type, extra
):
    company = make_company()
    scheme = direct_scheme(company)

    assert_db_rejects_create(scheme, segment_type, **extra)


@pytest.mark.parametrize("segment_type", ("fixed_text", "custom_text"))
@pytest.mark.parametrize("extra", ({"sequence_length": 4}, {"zero_pad": False}))
def test_database_rejects_sequence_fields_on_constant_segments(segment_type, extra):
    company = make_company()
    scheme = direct_scheme(company)

    assert_db_rejects_create(scheme, segment_type, **extra)


@pytest.mark.parametrize("extra", ({"sequence_length": 4}, {"zero_pad": False}))
def test_database_rejects_sequence_fields_on_separator(extra):
    company = make_company()
    scheme = direct_scheme(company)

    assert_db_rejects_create(scheme, "separator", **extra)


@pytest.mark.parametrize("sequence_length", (None, 0, 13))
def test_database_rejects_null_zero_and_thirteen_sequence_length(sequence_length):
    company = make_company()
    scheme = direct_scheme(company)

    assert_db_rejects_create(scheme, "sequence", sequence_length=sequence_length)


def test_database_requires_sequence_zero_pad_but_allows_both_booleans():
    company = make_company()
    null_scheme = direct_scheme(company, key="NULL-PAD")
    assert_db_rejects_create(null_scheme, "sequence", zero_pad=None)

    for index, value in enumerate((True, False), start=1):
        scheme = direct_scheme(company, key=f"PAD-{index}")
        row = AssetCodingSegment.objects.create(
            coding_scheme=scheme,
            **segment_values("sequence", zero_pad=value),
        )
        assert row.zero_pad is value


@pytest.mark.parametrize("value", ("--", "::", ":", " ", ""))
def test_database_rejects_multicharacter_or_non_whitelisted_separator(value):
    company = make_company()
    scheme = direct_scheme(company)

    assert_db_rejects_create(scheme, "separator", fixed_value=value)


@pytest.mark.parametrize("segment_type", ("fixed_text", "custom_text"))
@pytest.mark.parametrize(
    "value", (None, "", " leading", "trailing ", "A\nB", "A\x01B", "{A}", "A}B")
)
def test_database_rejects_invalid_constant_text(segment_type, value):
    company = make_company()
    scheme = direct_scheme(company)

    assert_db_rejects_create(scheme, segment_type, fixed_value=value)


@pytest.mark.parametrize("segment_type", ALL_TYPES)
def test_database_rejects_non_null_format_string_for_every_type(segment_type):
    company = make_company()
    scheme = direct_scheme(company)

    assert_db_rejects_create(scheme, segment_type, format_string="YYYY")


def test_database_rejects_custom_field_and_duplicate_order_or_sequence():
    company = make_company()
    custom_scheme = direct_scheme(company, key="CUSTOM")
    assert_db_rejects_create(custom_scheme, "custom_field", fixed_value="X")

    scheme = direct_scheme(company, key="DUP")
    AssetCodingSegment.objects.create(
        coding_scheme=scheme, **segment_values("sequence")
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodingSegment.objects.create(
            coding_scheme=scheme,
            **segment_values("fixed_text", fixed_value="A"),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodingSegment.objects.create(
            coding_scheme=scheme,
            **segment_values("sequence", sequence_order=2),
        )


def test_scheme_database_constraints_cover_version_reset_dates_and_default():
    company = make_company()
    invalid_rows = (
        {"key": "VERSION", "version": 0},
        {"key": "START", "sequence_start": -1},
        {"key": "RESET", "reset_mode": "weekly"},
        {"key": "SCOPE", "reset_mode": "never", "category_scope_level": "leaf"},
        {
            "key": "DATES",
            "effective_from": timezone.localdate(),
            "effective_to": timezone.localdate() - timedelta(days=1),
        },
        {"key": "ACTIVE", "status": "active", "effective_from": None},
        {"key": "DEFAULT", "status": "draft", "is_default": True},
    )
    for values in invalid_rows:
        with pytest.raises(IntegrityError), transaction.atomic():
            direct_scheme(company, **values)


def test_database_uniqueness_for_scheme_version_and_active_default():
    company = make_company()
    direct_scheme(company, key="SAME", version=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        direct_scheme(company, key="SAME", version=1)

    direct_scheme(company, key="D1", status="active", is_default=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        direct_scheme(company, key="D2", status="active", is_default=True)


def test_model_validation_rejects_cross_company_version_chain():
    company = make_company()
    other = make_company("C2", active=False)
    previous = direct_scheme(company, key="CHAIN")
    candidate = AssetCodingScheme(
        company=other,
        name="bad chain",
        scheme_key="CHAIN",
        version=2,
        status="draft",
        reset_mode="never",
        sequence_start=1,
        effective_from=timezone.localdate(),
        previous_version=previous,
    )

    with pytest.raises(ValidationError):
        candidate.full_clean()


def test_counter_composite_unique_constraint():
    company = make_company()
    scheme = direct_scheme(company)
    SequenceCounter.objects.create(
        company=company, coding_scheme=scheme, scope_key="scope", current_value=0
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SequenceCounter.objects.create(
            company=company,
            coding_scheme=scheme,
            scope_key="scope",
            current_value=1,
        )


def test_issued_code_composite_uniques_and_state_checks():
    company = make_company()
    scheme = direct_scheme(company)
    AssetCodingSegment.objects.create(
        coding_scheme=scheme, **segment_values("sequence")
    )
    AssetCodingScheme.objects.filter(pk=scheme.pk).update(status="active")
    scheme.refresh_from_db()
    now = timezone.now()
    row = IssuedCode.objects.create(
        company=company,
        coding_scheme=scheme,
        scope_key="scope",
        sequence_value=1,
        display_code="A-0001",
        normalized_code="a-0001",
        effective_date=timezone.localdate(),
        idempotency_key="idem-1",
        issued_at=now,
    )
    conflicts = (
        {"normalized_code": row.normalized_code, "sequence_value": 2, "idempotency_key": "idem-2"},
        {"normalized_code": "a-0002", "sequence_value": 1, "idempotency_key": "idem-2"},
        {"normalized_code": "a-0002", "sequence_value": 2, "idempotency_key": row.idempotency_key},
    )
    for index, override in enumerate(conflicts, start=2):
        values = {
            "company": company,
            "coding_scheme": scheme,
            "scope_key": "scope",
            "sequence_value": index,
            "display_code": f"A-{index:04d}",
            "normalized_code": f"a-{index:04d}",
            "effective_date": timezone.localdate(),
            "idempotency_key": f"idem-{index}",
            "issued_at": now,
        }
        values.update(override)
        with pytest.raises(IntegrityError), transaction.atomic():
            IssuedCode.objects.create(**values)

    with pytest.raises(IntegrityError), transaction.atomic():
        IssuedCode.objects.create(
            company=company,
            coding_scheme=scheme,
            scope_key="negative",
            sequence_value=-1,
            display_code="NEG",
            normalized_code="neg",
            effective_date=timezone.localdate(),
            idempotency_key="negative",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        IssuedCode.objects.filter(pk=row.pk).update(
            status="voided", replaced_or_voided_reason="", replaced_or_voided_at=None
        )


def test_issued_code_model_delete_is_always_forbidden():
    company = make_company()
    scheme = direct_scheme(company)
    AssetCodingSegment.objects.create(
        coding_scheme=scheme, **segment_values("sequence")
    )
    AssetCodingScheme.objects.filter(pk=scheme.pk).update(status="active")
    scheme.refresh_from_db()
    row = IssuedCode.objects.create(
        company=company,
        coding_scheme=scheme,
        scope_key="scope",
        sequence_value=1,
        display_code="A-1",
        normalized_code="a-1",
        effective_date=timezone.localdate(),
        idempotency_key="delete-guard",
    )

    with pytest.raises(ValidationError):
        row.delete()


def test_postgresql_18_4_and_sprint2_guard_triggers_are_installed():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 2 PostgreSQL acceptance requires PostgreSQL")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        assert cursor.fetchone()[0].startswith("18.4")
        expected_names = {
            "trg_coding_scheme_validate",
            "trg_coding_scheme_delete",
            "trg_coding_scheme_structure",
            "trg_coding_segment_guard",
            "trg_coding_segment_structure",
            "trg_category_coding_scheme",
            "trg_sequence_counter_validate",
            "trg_issued_code_validate",
            "trg_issued_code_delete",
        }
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND NOT tgisinternal",
            [list(expected_names)],
        )
        names = {row[0] for row in cursor.fetchall()}
    assert expected_names <= names


def test_postgresql_counter_guard_rejects_cross_company_and_low_initial_value():
    if connection.vendor != "postgresql":
        pytest.skip("cross-table counter constraints require PostgreSQL")
    company = make_company()
    other = make_company("C2", active=False)
    scheme = direct_scheme(company, sequence_start=10)

    for owner, value in ((other, 9), (company, 8)):
        with pytest.raises(IntegrityError), transaction.atomic():
            SequenceCounter.objects.create(
                company=owner,
                coding_scheme=scheme,
                scope_key=f"{owner.pk}-{value}",
                current_value=value,
            )
    accepted = SequenceCounter.objects.create(
        company=company,
        coding_scheme=scheme,
        scope_key="initial",
        current_value=9,
    )
    assert accepted.current_value == scheme.sequence_start - 1


def test_postgresql_category_scheme_guard_rejects_cross_company_and_noncurrent():
    if connection.vendor != "postgresql":
        pytest.skip("cross-table category constraints require PostgreSQL")
    company = make_company()
    other = make_company("C2", active=False)
    current = direct_scheme(company, status="active")
    future = direct_scheme(
        company,
        key="FUTURE",
        status="active",
        effective_from=timezone.localdate() + timedelta(days=1),
    )
    category = AssetCategory.objects.create(
        company=other,
        code="EQ",
        normalized_code="eq",
        name="设备",
        category_type="equipment",
    )

    for candidate in (current, future):
        with pytest.raises(IntegrityError), transaction.atomic():
            AssetCategory.objects.filter(pk=category.pk).update(
                default_coding_scheme=candidate
            )


def test_postgresql_issued_code_identity_is_immutable_and_raw_delete_fails():
    if connection.vendor != "postgresql":
        pytest.skip("permanent issued-code guards require PostgreSQL")
    company = make_company()
    scheme = direct_scheme(company)
    AssetCodingSegment.objects.create(
        coding_scheme=scheme, **segment_values("sequence")
    )
    AssetCodingScheme.objects.filter(pk=scheme.pk).update(status="active")
    scheme.refresh_from_db()
    row = IssuedCode.objects.create(
        company=company,
        coding_scheme=scheme,
        scope_key="scope",
        sequence_value=1,
        display_code="A-1",
        normalized_code="a-1",
        effective_date=timezone.localdate(),
        idempotency_key="immutable",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        IssuedCode.objects.filter(pk=row.pk).update(display_code="CHANGED")
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM masterdata_issuedcode WHERE id = %s", [row.pk])
    assert IssuedCode.objects.filter(pk=row.pk).exists()


def test_postgresql_active_scheme_and_segments_are_immutable():
    if connection.vendor != "postgresql":
        pytest.skip("version immutability guards require PostgreSQL")
    company = make_company()
    scheme = direct_scheme(company)
    segment = AssetCodingSegment.objects.create(
        coding_scheme=scheme, **segment_values("sequence")
    )
    AssetCodingScheme.objects.filter(pk=scheme.pk).update(status="active")

    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodingScheme.objects.filter(pk=scheme.pk).update(sequence_start=999)
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodingSegment.objects.filter(pk=segment.pk).update(sequence_length=5)
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodingSegment.objects.filter(pk=segment.pk).delete()


def test_tables_begin_empty_for_sprint2_business_workflows():
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
