from decimal import Decimal
from uuid import UUID

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError

from apps.audit.models import AuditLog
from apps.masterdata.services import set_employee_active
from apps.supplies.models import SupplyCategory, SupplyItem, SupplyWarehouse
from apps.supplies.services import (
    create_supply_category,
    create_supply_item,
    create_supply_warehouse,
    deactivate_supply_category,
    deactivate_supply_item,
    deactivate_supply_warehouse,
    supply_item_has_business_history,
    update_supply_category,
    update_supply_item,
    update_supply_warehouse,
)
from tests.test_sprint13_support import (
    make_company,
    make_department,
    make_employee,
    make_location,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
)


pytestmark = pytest.mark.django_db


def test_supply_category_normalization_uniqueness_tree_and_uuid():
    company = make_company()
    actor = make_user("s13-admin", "system_admin")
    root = create_supply_category(
        actor=actor,
        company=company,
        data={
            "code": " ＢＧ-01 ",
            "name": "办公用品",
            "default_item_type": "consumable",
        },
    )
    assert isinstance(root.pk, UUID)
    assert root.code == "BG-01"
    assert root.normalized_code == "bg-01"

    with pytest.raises(ValidationError):
        create_supply_category(
            actor=actor,
            company=company,
            data={"code": " bg-01 ", "name": "重复"},
        )

    child = create_supply_category(
        actor=actor,
        company=company,
        data={"code": "CHILD", "name": "纸品", "parent": root},
    )
    with pytest.raises(ValidationError):
        update_supply_category(
            actor=actor,
            category=root,
            data={"parent": child},
        )

    other = make_company("OTHER", active=False)
    foreign = make_supply_category(other, "FOREIGN")
    with pytest.raises(ValidationError):
        update_supply_category(
            actor=actor,
            category=child,
            data={"parent": foreign},
        )


def test_supply_warehouse_validates_company_location_and_active_employee():
    company = make_company()
    actor = make_user("s13-warehouse", "warehouse")
    department = make_department(company)
    employee = make_employee(company, department)
    location = make_location(company)
    warehouse = create_supply_warehouse(
        actor=actor,
        company=company,
        data={
            "code": " WH-01 ",
            "name": "办公用品仓",
            "location": location,
            "manager_employee": employee,
        },
    )
    assert isinstance(warehouse.pk, UUID)
    assert warehouse.normalized_code == "wh-01"

    other = make_company("OTHER", active=False)
    foreign_department = make_department(other, "FD")
    foreign_employee = make_employee(other, foreign_department, "FE")
    foreign_location = make_location(other, "FL")
    for data in (
        {"location": foreign_location},
        {"manager_employee": foreign_employee},
    ):
        with pytest.raises(ValidationError):
            update_supply_warehouse(
                actor=actor,
                warehouse=warehouse,
                data=data,
            )

    inactive = make_employee(company, department, "INACTIVE", is_active=False)
    with pytest.raises(ValidationError):
        update_supply_warehouse(
            actor=actor,
            warehouse=warehouse,
            data={"manager_employee": inactive},
        )


def test_supply_item_rules_decimal_company_and_protected_references():
    company = make_company()
    actor = make_user("s13-finance", "finance")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = create_supply_item(
        actor=actor,
        company=company,
        data={
            "item_code": " ＰＡＰＥＲ-A4 ",
            "name": "A4 复印纸",
            "category": category,
            "item_type": "consumable",
            "unit": "箱",
            "minimum_stock_quantity": Decimal("5.1250"),
            "default_warehouse": warehouse,
        },
    )
    assert isinstance(item.pk, UUID)
    assert item.item_code == "PAPER-A4"
    assert item.minimum_stock_quantity == Decimal("5.1250")
    assert supply_item_has_business_history(item) is False

    with pytest.raises(ValidationError):
        create_supply_item(
            actor=actor,
            company=company,
            data={
                "item_code": "NEG",
                "name": "负库存阈值",
                "category": category,
                "item_type": "consumable",
                "unit": "个",
                "minimum_stock_quantity": Decimal("-0.0001"),
            },
        )
    with pytest.raises(ValidationError):
        create_supply_item(
            actor=actor,
            company=company,
            data={
                "item_code": "BAD-TYPE",
                "name": "非法模式",
                "category": category,
                "item_type": "serialized",
                "unit": "个",
            },
        )

    other = make_company("OTHER", active=False)
    foreign_category = make_supply_category(other, "FOREIGN")
    with pytest.raises(ValidationError):
        update_supply_item(
            actor=actor,
            item=item,
            data={"category": foreign_category},
        )

    with pytest.raises(ProtectedError):
        category.delete()
    with pytest.raises(ProtectedError):
        warehouse.delete()


def test_services_enforce_role_boundaries_and_write_audit():
    company = make_company()
    admin = make_user("s13-admin", "system_admin")
    equipment = make_user("s13-equipment", "equipment")
    employee = make_user("s13-employee", "employee")
    category = create_supply_category(
        actor=admin,
        company=company,
        data={"code": "TOOLS", "name": "工具"},
    )
    warehouse = create_supply_warehouse(
        actor=admin,
        company=company,
        data={"code": "TOOLS-WH", "name": "工具仓"},
    )

    durable = create_supply_item(
        actor=equipment,
        company=company,
        data={
            "item_code": "CHAIR",
            "name": "普通办公椅",
            "category": category,
            "item_type": "durable_quantity",
            "unit": "把",
            "default_warehouse": warehouse,
        },
    )
    update_supply_item(
        actor=equipment,
        item=durable,
        data={"brand": "示例品牌"},
    )
    deactivate_supply_item(
        actor=equipment,
        item=durable,
        reason="停止采购",
    )
    with pytest.raises(PermissionDenied):
        create_supply_item(
            actor=equipment,
            company=company,
            data={
                "item_code": "PAPER",
                "name": "复印纸",
                "category": category,
                "item_type": "consumable",
                "unit": "箱",
            },
        )
    with pytest.raises(PermissionDenied):
        create_supply_category(
            actor=equipment,
            company=company,
            data={"code": "NO", "name": "无权"},
        )
    with pytest.raises(PermissionDenied):
        create_supply_warehouse(
            actor=employee,
            company=company,
            data={"code": "NO", "name": "无权"},
        )

    update_supply_category(
        actor=admin, category=category, data={"remark": "分类说明"}
    )
    update_supply_warehouse(
        actor=admin, warehouse=warehouse, data={"remark": "仓库说明"}
    )
    deactivate_supply_category(actor=admin, category=category, reason="整理分类")
    deactivate_supply_warehouse(actor=admin, warehouse=warehouse, reason="暂停使用")
    assert set(
        AuditLog.objects.filter(company=company).values_list("action", flat=True)
    ) >= {
        "supply_category_create",
        "supply_category_update",
        "supply_category_deactivate",
        "supply_warehouse_create",
        "supply_warehouse_update",
        "supply_warehouse_deactivate",
        "supply_item_create",
        "supply_item_update",
        "supply_item_deactivate",
    }


def test_disabling_employee_clears_supply_warehouse_manager_in_same_transaction():
    company = make_company()
    warehouse_actor = make_user("s13-manager-warehouse", "warehouse")
    hr = make_user("s13-manager-hr", "hr")
    department = make_department(company)
    manager = make_employee(company, department)
    warehouse = create_supply_warehouse(
        actor=warehouse_actor,
        company=company,
        data={
            "code": "MANAGED-WH",
            "name": "有负责人仓库",
            "manager_employee": manager,
        },
    )

    set_employee_active(actor=hr, employee=manager, is_active=False)

    warehouse.refresh_from_db()
    manager.refresh_from_db()
    assert manager.is_active is False
    assert warehouse.manager_employee_id is None
    assert AuditLog.objects.filter(
        company=company,
        action="supply_warehouse_manager_cleared",
        object_type="SupplyWarehouse",
        object_id=str(warehouse.pk),
    ).exists()
