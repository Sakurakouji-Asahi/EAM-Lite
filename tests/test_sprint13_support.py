from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from apps.masterdata.models import Company, Department, Employee, Location
from apps.supplies.models import SupplyCategory, SupplyItem, SupplyWarehouse


def make_company(code="S13", *, active=True):
    return Company.objects.create(
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 公司",
        short_name=code,
        is_active=active,
    )


def make_user(username, *roles):
    user = get_user_model().objects.create_user(
        username=username,
        password="Valid-Password-2026!",
        display_name=username,
    )
    for role in roles:
        user.groups.add(Group.objects.get(name=role))
    return user


def make_department(company, code="D1"):
    return Department.objects.create(
        company=company,
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 部门",
    )


def make_employee(company, department, number="E1", **overrides):
    values = {
        "company": company,
        "department": department,
        "employee_no": number,
        "normalized_employee_no": number.casefold(),
        "name": f"员工 {number}",
        "employment_status": "active",
        "is_active": True,
    }
    values.update(overrides)
    return Employee.objects.create(**values)


def make_location(company, code="L1", **overrides):
    values = {
        "company": company,
        "code": code,
        "normalized_code": code.casefold(),
        "name": f"位置 {code}",
        "location_type": "position",
    }
    values.update(overrides)
    return Location.objects.create(**values)


def make_supply_category(company, code="OFFICE", **overrides):
    values = {
        "company": company,
        "code": code,
        "normalized_code": code.casefold(),
        "name": f"分类 {code}",
    }
    values.update(overrides)
    return SupplyCategory.objects.create(**values)


def make_supply_warehouse(company, code="WH", **overrides):
    values = {
        "company": company,
        "code": code,
        "normalized_code": code.casefold(),
        "name": f"仓库 {code}",
    }
    values.update(overrides)
    return SupplyWarehouse.objects.create(**values)


def make_supply_item(company, category, code="ITEM", **overrides):
    values = {
        "company": company,
        "item_code": code,
        "normalized_item_code": code.casefold(),
        "name": f"物品 {code}",
        "category": category,
        "item_type": "consumable",
        "unit": "个",
    }
    values.update(overrides)
    return SupplyItem.objects.create(**values)
