from __future__ import annotations

import pytest

from apps.finance.forms import FixedAssetCategoryForm
from apps.masterdata.forms import (
    AssetCategoryForm,
    AssetCodingSegmentForm,
    EmployeeForm,
    LocationForm,
)
from apps.supplies.forms import (
    SupplyCategoryForm,
    SupplyItemForm,
    SupplyWarehouseForm,
)
from tests.test_sprint3_support import make_company, make_user


pytestmark = pytest.mark.django_db


def test_blank_required_identifiers_have_one_actionable_error_each():
    company = make_company("FORM-ERRORS")
    finance = make_user("form-errors-finance", "finance")
    equipment = make_user("form-errors-equipment", "equipment")
    hr = make_user("form-errors-hr", "hr")
    warehouse = make_user("form-errors-warehouse", "warehouse")

    cases = (
        (FixedAssetCategoryForm(data={}, actor=finance), "code"),
        (LocationForm(data={}, actor=equipment, company=company), "code"),
        (AssetCategoryForm(data={}, actor=equipment, company=company), "code"),
        (EmployeeForm(data={}, actor=hr, company=company), "employee_no"),
        (SupplyCategoryForm(data={}, actor=warehouse, company=company), "code"),
        (SupplyWarehouseForm(data={}, actor=warehouse, company=company), "code"),
        (SupplyItemForm(data={}, actor=warehouse, company=company), "item_code"),
    )

    for form, field_name in cases:
        assert not form.is_valid()
        errors = list(form.errors[field_name])
        assert len(errors) == 1
        assert "必填" in errors[0]


def test_blank_coding_segment_does_not_add_unsupported_type_errors():
    form = AssetCodingSegmentForm(data={})

    assert not form.is_valid()
    errors = list(form.errors["segment_type"])
    assert len(errors) == 1
    assert "必填" in errors[0]
    assert "不支持" not in str(form.errors)
