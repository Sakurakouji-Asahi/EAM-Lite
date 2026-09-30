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


def test_new_masterdata_numbers_allow_auto_numbering_and_keep_other_required_errors():
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
        assert form.fields[field_name].required is False
        assert field_name not in form.errors
        assert "自动生成" in form.fields[field_name].help_text
        errors = list(form.errors["name"])
        assert len(errors) == 1 and "必填" in errors[0]

        # Once a record exists, its identifier must not be silently emptied.
        form.instance._state.adding = False
        from apps.core.numbering import configure_auto_number_field
        configure_auto_number_field(form)
        assert form.fields[field_name].required is True
        form.full_clean()
        edit_errors = list(form.errors[field_name])
        assert len(edit_errors) == 1 and "必填" in edit_errors[0]


def test_blank_coding_segment_does_not_add_unsupported_type_errors():
    form = AssetCodingSegmentForm(data={})

    assert not form.is_valid()
    errors = list(form.errors["segment_type"])
    assert len(errors) == 1
    assert "必填" in errors[0]
    assert "不支持" not in str(form.errors)
