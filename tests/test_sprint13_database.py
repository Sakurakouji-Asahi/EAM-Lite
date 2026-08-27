from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction

from apps.supplies.models import SupplyCategory, SupplyItem, SupplyWarehouse
from tests.test_sprint13_support import (
    make_company,
    make_department,
    make_employee,
    make_location,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
)


pytestmark = pytest.mark.django_db


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 13 database guards require PostgreSQL")


def test_postgresql_supply_guards_are_installed():
    require_postgresql()
    expected = {
        "trg_supplies_category_company_immutable",
        "trg_supplies_warehouse_company_immutable",
        "trg_supplies_item_company_immutable",
        "trg_supplies_category_tree",
        "trg_supplies_warehouse_references",
        "trg_supplies_item_references",
        "trg_supplies_manager_employee_validity",
        "trg_supplies_manager_department_validity",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND NOT tgisinternal",
            [list(expected)],
        )
        actual = {row[0] for row in cursor.fetchall()}
    assert expected <= actual


def test_database_rejects_deep_category_cycle_and_cross_company_parent():
    require_postgresql()
    company = make_company()
    root = make_supply_category(company, "ROOT")
    child = make_supply_category(company, "CHILD", parent=root)
    leaf = make_supply_category(company, "LEAF", parent=child)
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplyCategory.objects.filter(pk=root.pk).update(parent=leaf)

    other = make_company("OTHER", active=False)
    foreign = make_supply_category(other, "FOREIGN")
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplyCategory.objects.filter(pk=child.pk).update(parent=foreign)


def test_database_rejects_cross_company_and_inactive_warehouse_references():
    require_postgresql()
    company = make_company()
    other = make_company("OTHER", active=False)
    location = make_location(company)
    department = make_department(company)
    employee = make_employee(company, department)
    foreign_location = make_location(other, "FOREIGN-L")
    foreign_department = make_department(other, "FOREIGN-D")
    foreign_employee = make_employee(other, foreign_department, "FOREIGN-E")

    for values in (
        {"location": foreign_location},
        {"manager_employee": foreign_employee},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplyWarehouse.objects.create(
                company=company,
                code=f"BAD-{len(values)}-{next(iter(values))}",
                name="非法仓库",
                **values,
            )

    location.is_active = False
    location.save(update_fields=["is_active"])
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplyWarehouse.objects.create(
            company=company,
            code="INACTIVE-L",
            name="停用位置仓库",
            location=location,
        )
    employee.is_active = False
    employee.save(update_fields=["is_active"])
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplyWarehouse.objects.create(
            company=company,
            code="INACTIVE-E",
            name="停用负责人仓库",
            manager_employee=employee,
        )


def test_database_rejects_supply_item_cross_company_and_check_constraints():
    require_postgresql()
    company = make_company()
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    other = make_company("OTHER", active=False)
    foreign_category = make_supply_category(other, "FOREIGN")
    foreign_warehouse = make_supply_warehouse(other, "FOREIGN-WH")

    for values in (
        {"category": foreign_category},
        {"category": category, "default_warehouse": foreign_warehouse},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplyItem.objects.create(
                company=company,
                item_code=f"BAD-{len(values)}",
                name="跨公司物品",
                item_type="consumable",
                unit="个",
                **values,
            )

    invalid_rows = (
        {"item_type": "serialized", "minimum_stock_quantity": Decimal("0")},
        {"item_type": "consumable", "minimum_stock_quantity": Decimal("-0.0001")},
    )
    for index, invalid in enumerate(invalid_rows):
        with pytest.raises(IntegrityError), transaction.atomic():
            SupplyItem.objects.create(
                company=company,
                item_code=f"CHECK-{index}",
                name="约束物品",
                category=category,
                unit="个",
                **invalid,
            )

    item = make_supply_item(company, category, "IMMUTABLE", default_warehouse=warehouse)
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplyItem.objects.filter(pk=item.pk).update(company=other)


def test_database_rejects_bypassing_warehouse_manager_employee_state_service():
    require_postgresql()
    company = make_company()
    department = make_department(company)
    employee = make_employee(company, department)
    make_supply_warehouse(company, manager_employee=employee)

    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE masterdata_employee SET is_active=false WHERE id=%s",
                [employee.pk],
            )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
