"""Real service examples for parent-derived master-data numbers."""
import pytest
from django.core.exceptions import ValidationError

from apps.masterdata.models import Department
from apps.masterdata.services import (
    create_asset_category,
    create_department,
    create_location,
    update_asset_category,
)
from apps.supplies.models import SupplyCategory
from apps.supplies.services import create_supply_category
from tests.test_sprint3_support import make_company, make_user


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def context():
    return make_company("HIERARCHY"), make_user("hierarchy-admin", "system_admin", "warehouse")


def test_department_numbers_follow_each_parent_and_keep_manual_codes(context):
    company, actor = context
    root = create_department(actor=actor, company=company, data={"name": "总部"})
    other_root = create_department(actor=actor, company=company, data={"name": "分部"})
    assert root.code == "DP000001"
    assert other_root.code == "DP000002"

    first = create_department(actor=actor, company=company, data={"name": "财务部", "parent": root})
    second = create_department(actor=actor, company=company, data={"name": "生产部", "parent": root})
    grandchild = create_department(actor=actor, company=company, data={"name": "一车间", "parent": first})
    other_child = create_department(actor=actor, company=company, data={"name": "采购部", "parent": other_root})
    assert [item.code for item in (first, second, grandchild, other_child)] == [
        "DP000001-01", "DP000001-02", "DP000001-01-01", "DP000002-01"
    ]
    manual = create_department(actor=actor, company=company, data={
        "code": "DP000001-09", "name": "手工编号部门", "parent": root,
    })
    later = create_department(actor=actor, company=company, data={"name": "新部门", "parent": root})
    assert manual.code == "DP000001-09"
    assert later.code == "DP000001-10"


def test_location_numbers_follow_actual_parent_with_three_levels(context):
    company, actor = context
    building = create_location(actor=actor, company=company, data={
        "name": "一号楼", "location_type": "site",
    })
    floor = create_location(actor=actor, company=company, data={
        "name": "一楼", "location_type": "other", "parent": building,
    })
    room = create_location(actor=actor, company=company, data={
        "name": "办公室", "location_type": "position", "parent": floor,
    })
    assert [building.code, floor.code, room.code] == [
        "LOC000001", "LOC000001-01", "LOC000001-01-01"
    ]


def test_asset_categories_keep_two_digit_major_and_parent_derived_children(context):
    company, actor = context
    root = create_asset_category(actor=actor, company=company, data={"name": "电子设备"})
    legacy = create_asset_category(actor=actor, company=company, data={
        "code": "CAT000001", "name": "原有格式小类", "parent": root,
    })
    first = create_asset_category(actor=actor, company=company, data={"name": "电脑", "parent": root})
    grandchild = create_asset_category(actor=actor, company=company, data={"name": "笔记本", "parent": first})
    assert [root.code, legacy.code, first.code, grandchild.code] == [
        "01", "CAT000001", "01-01", "01-01-01"
    ]
    second_root = create_asset_category(actor=actor, company=company, data={"name": "仪器"})
    moved = update_asset_category(actor=actor, category=first, data={"parent": second_root})
    assert moved.code == "01-01"
    assert moved.parent_id == second_root.pk
    grandchild.refresh_from_db()
    assert grandchild.code == "01-01-01"


def test_supply_categories_keep_independent_sequences_per_parent(context):
    company, actor = context
    root = create_supply_category(actor=actor, company=company, data={"name": "办公用品"})
    second_root = create_supply_category(actor=actor, company=company, data={"name": "工具"})
    child = create_supply_category(actor=actor, company=company, data={"name": "笔", "parent": root})
    second_child = create_supply_category(actor=actor, company=company, data={"name": "纸", "parent": root})
    grandchild = create_supply_category(actor=actor, company=company, data={"name": "签字笔", "parent": child})
    other_child = create_supply_category(actor=actor, company=company, data={"name": "扳手", "parent": second_root})
    assert [item.code for item in (root, second_root, child, second_child, grandchild, other_child)] == [
        "SC000001", "SC000002", "SC000001-01", "SC000001-02", "SC000001-01-01", "SC000002-01"
    ]
    child.is_active = False
    child.save(update_fields=["is_active", "updated_at"])
    assert create_supply_category(actor=actor, company=company, data={"name": "文件夹", "parent": root}).code == "SC000001-03"
    assert SupplyCategory.objects.filter(company=company, parent=root).count() == 3


def test_child_length_and_sequence_limit_fail_clearly_without_changing_parent(context):
    company, actor = context
    long_root = create_department(actor=actor, company=company, data={"code": "L" * 98, "name": "长编码部门"})
    with pytest.raises(ValidationError, match="上级编码过长"):
        create_department(actor=actor, company=company, data={"name": "下级", "parent": long_root})
    assert Department.objects.filter(company=company, parent=long_root).count() == 0

    root = create_department(actor=actor, company=company, data={"code": "ROOT", "name": "人工根号"})
    create_department(actor=actor, company=company, data={
        "code": "ROOT-99", "name": "末位手工号", "parent": root,
    })
    with pytest.raises(ValidationError, match="两位序号已用完"):
        create_department(actor=actor, company=company, data={"name": "自动末位之后", "parent": root})
    assert create_department(actor=actor, company=company, data={
        "code": "自编下级", "name": "仍可手工填写", "parent": root,
    }).code == "自编下级"
