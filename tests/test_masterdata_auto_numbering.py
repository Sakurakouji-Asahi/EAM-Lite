"""Business evidence for optional automatic master-data numbering."""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.assets.registration import create_registered_asset
from apps.audit.models import AuditLog
from apps.coding.services import activate_scheme, create_scheme, set_default_scheme
from apps.coding.standard import standard_segments
from apps.finance.forms import FixedAssetCategoryForm
from apps.finance.services import create_fixed_asset_category, update_fixed_asset_category
from apps.masterdata.forms import (
    AssetCategoryForm,
    CompanyForm,
    DepartmentForm,
    EmployeeForm,
    LocationForm,
)
from apps.masterdata.models import (
    AssetCategory,
    Company,
    Department,
    Employee,
    FixedAssetCategory,
    Location,
)
from apps.masterdata.services import (
    create_asset_category,
    create_company,
    create_department,
    create_employee,
    create_location,
    update_asset_category,
    update_company,
    update_department,
    update_employee,
    update_location,
)
from tests.test_sprint1_services import make_company, make_user
from tests.test_sprint4_acceptance import _base_context, _mark_initialized


pytestmark = pytest.mark.django_db


@pytest.fixture
def context():
    company = make_company()
    admin = make_user("number-admin", "system_admin", "hr", "finance")
    department = create_department(
        actor=admin, company=company, data={"code": "WORKSHOP", "name": "车间"}
    )
    return company, admin, department


CASES = (
    (Department, create_department, update_department, "department", "code", "DP000001", {}),
    (Employee, create_employee, update_employee, "employee", "employee_no", "EMP000001", {}),
    (Location, create_location, update_location, "location", "code", "LOC000001", {"location_type": "position"}),
    (AssetCategory, create_asset_category, update_asset_category, "category", "code", "01", {}),
    (FixedAssetCategory, create_fixed_asset_category, update_fixed_asset_category, "category", "code", "FAC000001", {"useful_life_months_default": 60}),
)


@pytest.mark.parametrize("model,create,update,argument,field,expected,extra", CASES)
def test_create_blank_and_manual_and_edit_preserve_identity(
    context, model, create, update, argument, field, expected, extra
):
    company, actor, department = context
    data = {"name": "新增资料", **extra}
    if model is Employee:
        data["department"] = department
    generated = create(actor=actor, company=company, data={**data, field: "  "})
    assert getattr(generated, field) == expected
    audit = AuditLog.objects.filter(
        object_type=model.__name__, object_id=str(generated.pk)
    ).latest("created_at")
    assert audit.new_data_json[field] == expected

    manual = create(actor=actor, company=company, data={**data, field: "LEGACY-008"})
    assert getattr(manual, field) == "LEGACY-008"
    changed = update(actor=actor, **{argument: generated}, data={"name": "更新名称"})
    assert getattr(changed, field) == expected
    assert changed.name == "更新名称"
    with pytest.raises(ValidationError):
        update(actor=actor, **{argument: generated}, data={field: ""})
    generated.refresh_from_db()
    assert getattr(generated, field) == expected


@pytest.mark.parametrize("manual", ["", "LEGACY-COMPANY"])
def test_company_number_is_optional_only_at_creation(manual):
    actor = make_user("company-admin", "system_admin")
    data = {"code": manual, "name": "制造公司", "short_name": "制造", "currency": "CNY", "timezone": "Asia/Shanghai"}
    form = CompanyForm(data=data, actor=actor)
    assert form.is_valid(), form.errors
    company = create_company(actor=actor, data=form.cleaned_data)
    expected = manual or "CO000001"
    assert company.code == expected
    updated = update_company(actor=actor, company=company, data={"name": "制造公司新名称"})
    assert updated.code == expected
    with pytest.raises(ValidationError):
        update_company(actor=actor, company=company, data={"code": ""})
    company.refresh_from_db()
    assert company.code == expected


def test_manual_casefold_collision_is_skipped_and_still_rejected(context):
    company, actor, _ = context
    manual = create_department(
        actor=actor, company=company, data={"code": "ｄｐ０００００１", "name": "历史部门"}
    )
    assert manual.code == "dp000001"
    generated = create_department(actor=actor, company=company, data={"name": "新增部门"})
    assert generated.code == "DP000002"
    with pytest.raises(ValidationError):
        create_department(actor=actor, company=company, data={"code": "DP000001", "name": "重复部门"})


def test_physical_category_reserves_existing_numbers_and_child_uses_separate_prefix(context):
    company, actor, _ = context
    for code in ("01", "03", "99"):
        create_asset_category(actor=actor, company=company, data={"code": code, "name": f"分类{code}"})
    generated = create_asset_category(actor=actor, company=company, data={"name": "新实物分类"})
    child = create_asset_category(actor=actor, company=company, data={"name": "下级分类", "parent": generated})
    assert generated.code == "02"
    assert child.code == f"{generated.code}-01"
    assert child.parent_id == generated.pk and child.category_level == 2


def test_unauthorized_creation_does_not_issue_or_save_number(context):
    company, actor, _ = context
    employee = make_user("ordinary-number-user", "employee")
    with pytest.raises(PermissionDenied):
        create_department(actor=employee, company=company, data={"name": "越权部门"})
    generated = create_department(actor=actor, company=company, data={"name": "有效部门"})
    assert generated.code == "DP000001"
    assert not Department.objects.filter(name="越权部门").exists()


FORM_CASES = (
    (DepartmentForm, Department, "masterdata:department-create", "masterdata:department-edit", "code", {"name": "网页部门"}),
    (EmployeeForm, Employee, "masterdata:employee-create", "masterdata:employee-edit", "employee_no", {"name": "网页员工", "employment_status": "active"}),
    (LocationForm, Location, "masterdata:location-create", "masterdata:location-edit", "code", {"name": "网页位置", "location_type": "position"}),
    (AssetCategoryForm, AssetCategory, "masterdata:category-create", "masterdata:category-edit", "code", {"name": "网页分类"}),
    (FixedAssetCategoryForm, FixedAssetCategory, "finance:fixed-category-create", "finance:fixed-category-edit", "code", {"name": "网页会计类别", "useful_life_months_default": 60}),
)


@pytest.mark.parametrize("form_class,model,create_url,edit_url,field,data", FORM_CASES)
def test_create_page_accepts_blank_number_and_edit_requires_existing_number(
    context, client, form_class, model, create_url, edit_url, field, data
):
    company, actor, department = context
    data = {**data, field: ""}
    if model is Employee:
        data["department"] = department.pk
    kwargs = {"actor": actor}
    if model is not FixedAssetCategory:
        kwargs["company"] = company
    form = form_class(data=data, **kwargs)
    form.instance.company = company
    assert not form.fields[field].required
    assert form.is_valid(), form.errors
    client.force_login(actor)
    page = client.get(reverse(create_url))
    assert page.status_code == 200
    assert "自动" in page.content.decode()
    result = client.post(reverse(create_url), data)
    assert result.status_code == 302, result.content.decode()
    instance = model.objects.get(company=company, name=data["name"])
    original_code = getattr(instance, field)
    assert original_code
    edit_form = form_class(data=data, instance=instance, **kwargs)
    assert edit_form.fields[field].required
    assert not edit_form.is_valid()
    assert field in edit_form.errors
    rejected = client.post(reverse(edit_url, args=[instance.pk]), data)
    assert rejected.status_code == 200
    instance.refresh_from_db()
    assert getattr(instance, field) == original_code


def test_auto_physical_category_works_in_standard_single_item_registration():
    data = _base_context("AUTO-PHYSICAL", initialize=False)
    category = create_asset_category(
        actor=data["admin"], company=data["company"], data={"name": "新实物分类"}
    )
    rule = create_scheme(
        actor=data["admin"], company=data["company"],
        data={"scheme_key": "AUTO-PHYSICAL", "name": "统一编码", "reset_mode": "category_yearly", "category_scope_level": "major", "sequence_start": 1, "effective_from": timezone.localdate()},
        segments=standard_segments(),
    )
    rule = activate_scheme(actor=data["admin"], scheme=rule)
    set_default_scheme(actor=data["admin"], scheme=rule)
    _mark_initialized(data["company"], data["admin"])
    asset = create_registered_asset(
        actor=data["equipment"], company=data["company"],
        data={"asset_name": "逐件管理工器具", "category": category, "unit": "台", "department": data["department"], "responsible_employee": data["employee"], "location": data["location"], "management_attribute": "LV", "acquisition_date": date(2020, 8, 1)},
        idempotency_key="auto-category-single-item",
    )
    assert category.code == "01"
    assert asset.asset_code == "LV-01-2020-000001-00"
    assert asset.identity.category_code == category.code
