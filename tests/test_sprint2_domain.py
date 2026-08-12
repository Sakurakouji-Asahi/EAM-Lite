from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError

from apps.coding.domain import (
    build_scope_key,
    normalize_code,
    preview_codes,
    render_code,
    validate_scheme_structure,
    validate_segment_fields,
)


SOURCE_AND_DATE_SEGMENTS = (
    "company_code",
    "major_category_code",
    "minor_category_code",
    "category_code",
    "department_code",
    "year",
    "year_month",
    "full_date",
)
ALL_SEGMENT_TYPES = (
    "fixed_text",
    *SOURCE_AND_DATE_SEGMENTS,
    "sequence",
    "custom_text",
    "separator",
)


def segment(segment_type, order, **overrides):
    values = {
        "segment_type": segment_type,
        "sequence_order": order,
        "fixed_value": None,
        "format_string": None,
        "sequence_length": None,
        "zero_pad": None,
    }
    values.update(overrides)
    return values


def scheme(segments, **overrides):
    values = {
        "segments": segments,
        "reset_mode": "never",
        "sequence_start": 1,
        "category_scope_level": None,
        "status": "draft",
        "effective_from": None,
        "effective_to": None,
    }
    values.update(overrides)
    return values


def category(code, *, parent=None, company_id=None):
    return SimpleNamespace(
        id=uuid4(),
        pk=uuid4(),
        code=code,
        parent=parent,
        company_id=company_id,
        is_active=True,
    )


def rendering_context(*, effective_date=date(2026, 8, 12)):
    company_id = uuid4()
    company = SimpleNamespace(
        id=company_id, pk=company_id, code="ACME", is_active=True
    )
    department = SimpleNamespace(
        id=uuid4(), code="MFG", company_id=company_id, is_active=True
    )
    major = category("EQ", company_id=company_id)
    minor = category("MOLD", parent=major, company_id=company_id)
    leaf = category("PREC", parent=minor, company_id=company_id)
    return {
        "company": company,
        "department": department,
        "category": leaf,
        "effective_date": effective_date,
    }


@pytest.mark.parametrize(
    ("segment_type", "kwargs"),
    [
        ("fixed_text", {"fixed_value": "FA"}),
        ("custom_text", {"fixed_value": "CUSTOM"}),
        ("separator", {"fixed_value": "-"}),
        ("sequence", {"sequence_length": 4, "zero_pad": True}),
        *[(item, {}) for item in SOURCE_AND_DATE_SEGMENTS],
    ],
)
def test_segment_matrix_accepts_every_legal_field_combination(segment_type, kwargs):
    validated = validate_segment_fields(segment_type, **kwargs)

    assert validated["segment_type"] == segment_type
    assert validated["format_string"] is None


@pytest.mark.parametrize("segment_type", SOURCE_AND_DATE_SEGMENTS)
@pytest.mark.parametrize(
    "extra",
    [
        {"fixed_value": "EXTRA"},
        {"sequence_length": 4},
        {"zero_pad": False},
    ],
)
def test_source_and_date_segments_reject_every_extra_field(segment_type, extra):
    with pytest.raises(ValidationError):
        validate_segment_fields(segment_type, **extra)


@pytest.mark.parametrize("segment_type", ["fixed_text", "custom_text"])
@pytest.mark.parametrize(
    "extra",
    [{"sequence_length": 4}, {"zero_pad": False}],
)
def test_constant_segments_reject_sequence_only_fields(segment_type, extra):
    with pytest.raises(ValidationError):
        validate_segment_fields(segment_type, fixed_value="CONST", **extra)


@pytest.mark.parametrize("extra", [{"sequence_length": 4}, {"zero_pad": False}])
def test_separator_rejects_sequence_only_fields(extra):
    with pytest.raises(ValidationError):
        validate_segment_fields("separator", fixed_value="-", **extra)


@pytest.mark.parametrize("fixed_value", ["X", "", 0])
def test_sequence_rejects_non_null_fixed_value(fixed_value):
    with pytest.raises(ValidationError):
        validate_segment_fields(
            "sequence",
            fixed_value=fixed_value,
            sequence_length=4,
            zero_pad=True,
        )


@pytest.mark.parametrize("sequence_length", [None, 0, 13, True])
def test_sequence_length_null_zero_thirteen_and_bool_are_rejected(sequence_length):
    with pytest.raises(ValidationError):
        validate_segment_fields(
            "sequence", sequence_length=sequence_length, zero_pad=True
        )


@pytest.mark.parametrize("zero_pad", [True, False])
def test_sequence_explicit_boolean_zero_pad_values_are_allowed(zero_pad):
    result = validate_segment_fields(
        "sequence", sequence_length=4, zero_pad=zero_pad
    )

    assert result["zero_pad"] is zero_pad


@pytest.mark.parametrize("zero_pad", [None, 0, 1, "true", "false"])
def test_sequence_zero_pad_must_be_an_explicit_boolean(zero_pad):
    with pytest.raises(ValidationError):
        validate_segment_fields(
            "sequence", sequence_length=4, zero_pad=zero_pad
        )


@pytest.mark.parametrize("separator", ["-", "_", ".", "/"])
def test_separator_accepts_only_each_approved_single_character(separator):
    result = validate_segment_fields("separator", fixed_value=separator)

    assert result["fixed_value"] == separator


@pytest.mark.parametrize("separator", ["--", "::", ":", " ", "", None])
def test_separator_rejects_multiple_or_non_whitelisted_characters(separator):
    with pytest.raises(ValidationError):
        validate_segment_fields("separator", fixed_value=separator)


@pytest.mark.parametrize("segment_type", ["fixed_text", "custom_text"])
@pytest.mark.parametrize(
    "fixed_value",
    [None, "", " leading", "trailing ", "\n", "A\x00B", "{value}", "A}B"],
)
def test_constant_text_rejects_empty_whitespace_control_and_braces(
    segment_type, fixed_value
):
    with pytest.raises(ValidationError):
        validate_segment_fields(segment_type, fixed_value=fixed_value)


@pytest.mark.parametrize("segment_type", ALL_SEGMENT_TYPES)
def test_format_string_is_rejected_for_every_segment_type(segment_type):
    kwargs = {"format_string": "YYYY"}
    if segment_type in {"fixed_text", "custom_text", "separator"}:
        kwargs["fixed_value"] = "-" if segment_type == "separator" else "X"
    elif segment_type == "sequence":
        kwargs.update(sequence_length=4, zero_pad=True)

    with pytest.raises(ValidationError):
        validate_segment_fields(segment_type, **kwargs)


def test_custom_field_is_not_a_v1_segment_type():
    with pytest.raises(ValidationError):
        validate_segment_fields("custom_field", fixed_value="anything")


def test_date_segments_have_strict_fixed_output_and_no_custom_format():
    segments = [
        segment("year", 1),
        segment("separator", 2, fixed_value="-"),
        segment("year_month", 3),
        segment("separator", 4, fixed_value="-"),
        segment("full_date", 5),
        segment("separator", 6, fixed_value="-"),
        segment("sequence", 7, sequence_length=1, zero_pad=False),
    ]

    assert render_code(segments, rendering_context(), 1) == "2026-202608-20260812-1"


@pytest.mark.parametrize(
    ("length", "zero_pad", "sequence_value", "expected"),
    [
        (4, True, 23, "0023"),
        (5, True, 23, "00023"),
        (4, False, 23, "23"),
        (5, False, 23, "23"),
    ],
)
def test_sequence_four_five_digit_padding_and_no_padding(
    length, zero_pad, sequence_value, expected
):
    segments = [
        segment(
            "sequence", 1, sequence_length=length, zero_pad=zero_pad
        )
    ]

    assert render_code(segments, rendering_context(), sequence_value) == expected


@pytest.mark.parametrize("zero_pad", [True, False])
def test_sequence_overflow_is_rejected_without_truncation_or_expansion(zero_pad):
    segments = [segment("sequence", 1, sequence_length=2, zero_pad=zero_pad)]

    with pytest.raises(ValidationError, match="溢出"):
        render_code(segments, rendering_context(), 100)


@pytest.mark.parametrize("sequence_start", [0, 1, 37])
def test_sequence_start_is_the_first_preview_value_without_off_by_one(sequence_start):
    configured = scheme(
        [segment("sequence", 1, sequence_length=4, zero_pad=False)],
        sequence_start=sequence_start,
    )

    assert preview_codes(configured, rendering_context()) == [str(sequence_start)]


def test_preview_from_counter_snapshot_starts_at_current_value_plus_one():
    configured = scheme(
        [segment("sequence", 1, sequence_length=4, zero_pad=True)],
        sequence_start=1,
    )

    assert preview_codes(
        configured, rendering_context(), count=3, current_value=9
    ) == ["0010", "0011", "0012"]


def test_all_source_segments_render_from_company_physical_tree_and_department():
    segments = [
        segment("company_code", 1),
        segment("separator", 2, fixed_value="/"),
        segment("major_category_code", 3),
        segment("separator", 4, fixed_value="/"),
        segment("minor_category_code", 5),
        segment("separator", 6, fixed_value="/"),
        segment("category_code", 7),
        segment("separator", 8, fixed_value="/"),
        segment("department_code", 9),
        segment("separator", 10, fixed_value="/"),
        segment("sequence", 11, sequence_length=2, zero_pad=True),
    ]

    assert render_code(segments, rendering_context(), 1) == (
        "ACME/EQ/MOLD/PREC/MFG/01"
    )


@pytest.mark.parametrize(
    ("segment_type", "removed_context"),
    [
        ("company_code", "company"),
        ("department_code", "department"),
        ("category_code", "category"),
    ],
)
def test_missing_required_source_has_explicit_validation_error(
    segment_type, removed_context
):
    context = rendering_context()
    context.pop(removed_context)
    segments = [
        segment(segment_type, 1),
        segment("sequence", 2, sequence_length=1, zero_pad=False),
    ]

    with pytest.raises(ValidationError):
        render_code(segments, context, 1)


@pytest.mark.parametrize("segment_type", ["major_category_code", "minor_category_code"])
def test_category_level_source_rejects_a_path_without_required_level(segment_type):
    context = rendering_context()
    if segment_type == "major_category_code":
        context["category"] = None
    else:
        context["category"] = category("ONLY")
    segments = [
        segment(segment_type, 1),
        segment("sequence", 2, sequence_length=1, zero_pad=False),
    ]

    with pytest.raises(ValidationError):
        render_code(segments, context, 1)


def test_cross_company_category_and_department_sources_are_rejected():
    context = rendering_context()
    context["department"].company_id = uuid4()
    segments = [
        segment("department_code", 1),
        segment("sequence", 2, sequence_length=1, zero_pad=False),
    ]

    with pytest.raises(ValidationError, match="公司"):
        render_code(segments, context, 1)


@pytest.mark.parametrize(
    "segments",
    [
        [segment("fixed_text", 1, fixed_value="A")],
        [
            segment("sequence", 1, sequence_length=2, zero_pad=True),
            segment("sequence", 2, sequence_length=2, zero_pad=True),
        ],
    ],
)
def test_scheme_requires_exactly_one_sequence_segment(segments):
    with pytest.raises(ValidationError, match="必须且只能"):
        validate_scheme_structure(segments)


@pytest.mark.parametrize(
    "orders",
    [(1, 1), (1, 3), (0, 1)],
)
def test_segment_order_must_be_unique_positive_and_contiguous(orders):
    segments = [
        segment("fixed_text", orders[0], fixed_value="A"),
        segment("sequence", orders[1], sequence_length=2, zero_pad=True),
    ]

    with pytest.raises(ValidationError, match="顺序"):
        validate_scheme_structure(segments)


@pytest.mark.parametrize(
    ("reset_mode", "expected_keys"),
    [
        ("never", {"company_id", "scheme_id", "reset"}),
        ("yearly", {"company_id", "scheme_id", "reset", "year"}),
        (
            "monthly",
            {"company_id", "scheme_id", "reset", "year", "month"},
        ),
        (
            "category_yearly",
            {
                "company_id",
                "scheme_id",
                "reset",
                "year",
                "category_level",
                "category_id",
            },
        ),
        (
            "category_monthly",
            {
                "company_id",
                "scheme_id",
                "reset",
                "year",
                "month",
                "category_level",
                "category_id",
            },
        ),
    ],
)
def test_all_five_reset_modes_build_canonical_stable_scopes(
    reset_mode, expected_keys
):
    company_id = uuid4()
    scheme_id = uuid4()
    leaf = category("LEAF")
    kwargs = {}
    if reset_mode.startswith("category_"):
        kwargs = {"category": leaf, "category_scope_level": "leaf"}

    first = build_scope_key(
        company_id, scheme_id, reset_mode, date(2026, 8, 12), **kwargs
    )
    second = build_scope_key(
        company_id, scheme_id, reset_mode, date(2026, 8, 12), **kwargs
    )
    decoded = json.loads(first)

    assert first == second
    assert first == json.dumps(decoded, ensure_ascii=True, separators=(",", ":"))
    assert set(decoded) == expected_keys
    assert decoded["company_id"] == str(company_id)
    assert decoded["scheme_id"] == str(scheme_id)


def test_scope_uses_shanghai_business_date_for_year_and_month_boundaries():
    instant = datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc)

    decoded = json.loads(build_scope_key("company", "scheme", "monthly", instant))

    assert decoded["year"] == 2027
    assert decoded["month"] == 1


def test_scope_requires_explicit_effective_date_and_never_reads_current_time():
    with pytest.raises(ValidationError, match="显式"):
        build_scope_key("company", "scheme", "monthly", None)


def test_company_scheme_month_and_category_scopes_are_mutually_isolated():
    major_a = category("A")
    leaf_a = category("A1", parent=major_a)
    major_b = category("B")
    leaf_b = category("B1", parent=major_b)
    base = ("company-1", "scheme-1", "category_monthly")
    a = build_scope_key(
        *base,
        date(2026, 8, 1),
        category=leaf_a,
        category_scope_level="major",
    )

    variants = {
        build_scope_key(
            "company-2",
            "scheme-1",
            "category_monthly",
            date(2026, 8, 1),
            category=leaf_a,
            category_scope_level="major",
        ),
        build_scope_key(
            "company-1",
            "scheme-2",
            "category_monthly",
            date(2026, 8, 1),
            category=leaf_a,
            category_scope_level="major",
        ),
        build_scope_key(
            *base,
            date(2026, 9, 1),
            category=leaf_a,
            category_scope_level="major",
        ),
        build_scope_key(
            *base,
            date(2026, 8, 1),
            category=leaf_b,
            category_scope_level="major",
        ),
    }

    assert a not in variants
    assert len(variants) == 4


def test_scope_uses_stable_category_id_not_mutable_display_code():
    leaf = category("BEFORE")
    original = build_scope_key(
        "company",
        "scheme",
        "category_yearly",
        date(2026, 8, 12),
        category=leaf,
        category_scope_level="leaf",
    )
    leaf.code = "AFTER"

    assert build_scope_key(
        "company",
        "scheme",
        "category_yearly",
        date(2026, 8, 12),
        category=leaf,
        category_scope_level="leaf",
    ) == original


@pytest.mark.parametrize("level", [None, "unknown"])
def test_category_reset_rejects_missing_or_unknown_scope_level(level):
    with pytest.raises(ValidationError):
        build_scope_key(
            "company",
            "scheme",
            "category_yearly",
            date(2026, 8, 12),
            category=category("LEAF"),
            category_scope_level=level,
        )


def test_preview_generates_ten_consecutive_examples_with_zero_database_queries(
    django_assert_num_queries,
    db,
):
    configured = scheme(
        [
            segment("fixed_text", 1, fixed_value="FA"),
            segment("separator", 2, fixed_value="-"),
            segment("sequence", 3, sequence_length=4, zero_pad=True),
        ],
        sequence_start=1,
    )

    with django_assert_num_queries(0):
        examples = preview_codes(configured, rendering_context(), count=10)

    assert examples == [f"FA-{value:04d}" for value in range(1, 11)]


@pytest.mark.parametrize("count", [0, 11, None, True])
def test_preview_count_is_bounded_to_one_through_ten(count):
    configured = scheme(
        [segment("sequence", 1, sequence_length=4, zero_pad=True)]
    )

    with pytest.raises(ValidationError):
        preview_codes(configured, rendering_context(), count=count)


def test_normalized_code_uses_nfkc_strip_and_casefold():
    assert normalize_code("ＡＢＣ-Ä") == normalize_code("ABC-ä")


def test_render_rejects_a_final_code_longer_than_sixty_four_characters():
    segments = [
        segment("fixed_text", 1, fixed_value="X" * 64),
        segment("sequence", 2, sequence_length=1, zero_pad=False),
    ]

    with pytest.raises(ValidationError, match="64"):
        render_code(segments, rendering_context(), 1)
