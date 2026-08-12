from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.assets.models import Asset, AssetCodeHistory, AssetCustomField, AssetCustomValue
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_sprint3_support import (
    direct_draft,
    make_category,
    make_company,
    make_custom_field,
)


pytestmark = pytest.mark.django_db


def test_draft_uses_null_official_identity_and_single_item_quantity():
    company = make_company()
    category = make_category(company)

    asset = direct_draft(company, category)

    assert asset.asset_code is None
    assert asset.current_issued_code_id is None
    assert asset.quantity == 1
    assert asset.tracking_mode == "single_item"
    assert asset.draft_number.startswith("D-")
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetCodeHistory.objects.count() == 0


@pytest.mark.parametrize("quantity", (0, 2, 32767))
def test_database_rejects_any_non_single_quantity(quantity):
    company = make_company()
    category = make_category(company)

    with pytest.raises(IntegrityError), transaction.atomic():
        direct_draft(company, category, quantity=quantity)


def test_database_rejects_empty_string_as_fake_official_code():
    company = make_company()
    category = make_category(company)

    with pytest.raises(IntegrityError), transaction.atomic():
        direct_draft(company, category, asset_code="")


def test_asset_schema_contains_no_batch_or_partial_quantity_fields():
    names = {field.name for field in Asset._meta.get_fields()}

    assert "batch_quantity" not in names
    assert "partial_quantity" not in names
    assert "transfer_quantity" not in names
    assert "disposal_quantity" not in names
    assert {value for value, _label in Asset.TrackingMode.choices} == {"single_item"}


def test_physical_category_does_not_store_accounting_classification():
    company = make_company()
    equipment = make_category(company, "MOLD", category_type="mold")
    asset = direct_draft(company, equipment)

    asset_fields = {field.name for field in Asset._meta.get_fields()}
    category_fields = {field.name for field in equipment._meta.get_fields()}
    assert asset.category.category_type == "mold"
    assert "accounting_treatment" not in asset_fields
    assert "fixed_asset_category" not in asset_fields
    assert "accounting_treatment" not in category_fields


@pytest.mark.parametrize(
    ("field_type", "options"),
    (
        ("text", None),
        ("decimal", None),
        ("date", None),
        ("boolean", None),
        ("select", ["合格", "待复核"]),
    ),
)
def test_custom_field_accepts_exact_approved_types(field_type, options):
    company = make_company()
    category = make_category(company)

    field = make_custom_field(company, category, field_type, field_type, options=options)

    assert field.field_type == field_type
    assert field.options_json == options


@pytest.mark.parametrize("field_type", ("integer", "json", "python", "custom"))
def test_custom_field_rejects_unknown_type_at_model_and_database(field_type):
    company = make_company()
    category = make_category(company)
    field = AssetCustomField(
        company=company,
        category=category,
        name="非法字段",
        code=f"BAD-{field_type}",
        field_type=field_type,
        options_json=None,
    )

    with pytest.raises(ValidationError):
        field.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCustomField.objects.create(
            company=company,
            category=category,
            name="非法字段",
            code=f"BAD-{field_type}",
            normalized_code=f"bad-{field_type}",
            field_type=field_type,
        )


@pytest.mark.parametrize(
    ("field_type", "options"),
    (
        ("text", ["X"]),
        ("decimal", []),
        ("select", None),
        ("select", []),
        ("select", [""]),
        ("select", [" X"]),
        ("select", ["X", "X"]),
        ("select", [1]),
    ),
)
def test_custom_field_options_matrix_rejects_invalid_combinations(field_type, options):
    company = make_company()
    category = make_category(company)
    field = AssetCustomField(
        company=company,
        category=category,
        name="选项测试",
        code="OPTION",
        field_type=field_type,
        options_json=options,
    )

    with pytest.raises(ValidationError):
        field.full_clean()


def test_custom_field_code_is_normalized_unique_in_company_scope():
    company = make_company()
    category = make_category(company)
    make_custom_field(company, category, "Serial", "text")

    duplicate = AssetCustomField(
        company=company,
        category=category,
        name="重复字段",
        code="ＳＥＲＩＡＬ",
        field_type="text",
    )
    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.parametrize(
    ("field_type", "value_column", "value"),
    (
        ("text", "value_text", "ABC"),
        ("decimal", "value_decimal", Decimal("12.34000000")),
        ("date", "value_date", date(2026, 8, 13)),
        ("boolean", "value_boolean", False),
        ("select", "value_text", "A"),
    ),
)
def test_custom_value_maps_each_type_to_exactly_one_column(
    field_type, value_column, value
):
    company = make_company()
    category = make_category(company)
    asset = direct_draft(company, category)
    field = make_custom_field(
        company,
        category,
        field_type,
        field_type,
        options=["A", "B"] if field_type == "select" else None,
    )
    kwargs = {value_column: value}
    saved = AssetCustomValue(
        company=company,
        asset=asset,
        custom_field=field,
        **kwargs,
    )
    saved.full_clean()
    saved.save()

    assert getattr(saved, value_column) == value
    other_columns = {
        "value_text",
        "value_decimal",
        "value_date",
        "value_boolean",
    } - {value_column}
    assert all(getattr(saved, column) is None for column in other_columns)


def test_custom_value_rejects_wrong_column_multiple_columns_and_unapproved_select():
    company = make_company()
    category = make_category(company)
    asset = direct_draft(company, category)
    decimal_field = make_custom_field(company, category, "WEIGHT", "decimal")
    select_field = make_custom_field(
        company, category, "GRADE", "select", options=["A", "B"]
    )
    invalid = (
        AssetCustomValue(
            company=company,
            asset=asset,
            custom_field=decimal_field,
            value_text="12.3",
        ),
        AssetCustomValue(
            company=company,
            asset=asset,
            custom_field=decimal_field,
            value_decimal=Decimal("12.3"),
            value_text="extra",
        ),
        AssetCustomValue(
            company=company,
            asset=asset,
            custom_field=select_field,
            value_text="C",
        ),
    )

    for value in invalid:
        with pytest.raises(ValidationError):
            value.full_clean()


def test_custom_value_is_unique_per_asset_and_field():
    company = make_company()
    category = make_category(company)
    asset = direct_draft(company, category)
    field = make_custom_field(company, category, "COLOR", "text")
    AssetCustomValue.objects.create(
        company=company,
        asset=asset,
        custom_field=field,
        value_text="red",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCustomValue.objects.create(
            company=company,
            asset=asset,
            custom_field=field,
            value_text="blue",
        )


def test_asset_code_history_schema_is_append_only_and_empty_in_sprint3():
    company = make_company()
    category = make_category(company)
    direct_draft(company, category)

    assert AssetCodeHistory.objects.count() == 0
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    history_fields = {field.name for field in AssetCodeHistory._meta.get_fields()}
    assert {
        "asset",
        "company",
        "event_type",
        "old_issued_code",
        "new_issued_code",
        "reason",
        "effective_at",
        "operated_by",
    } <= history_fields
