"""Constructed cases for allocation, rollback and real PostgreSQL races."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction

from apps.audit.models import AuditLog
from apps.core.numbering import prepare_auto_number
from apps.masterdata.models import AssetCategory
from apps.masterdata.services import create_asset_category
from apps.supplies.models import SupplyWarehouse
from apps.supplies.services import create_supply_warehouse, update_supply_warehouse
from tests.test_sprint3_support import make_company, make_user


pytestmark = pytest.mark.django_db(transaction=True)


def test_parallel_automatic_numbers_are_distinct_and_audited():
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL transaction locks")
    company = make_company("PARALLEL")
    actor = make_user("parallel-warehouse", "warehouse")
    barrier = Barrier(4)

    def create(index):
        close_old_connections()
        try:
            barrier.wait(timeout=15)
            return create_supply_warehouse(
                actor=actor, company=company, data={"name": f"测试仓库 {index}"}
            ).code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=4) as executor:
        codes = list(executor.map(create, range(4)))
    assert set(codes) == {"WH000001", "WH000002", "WH000003", "WH000004"}
    assert SupplyWarehouse.objects.filter(company=company).count() == 4
    recorded = AuditLog.objects.filter(action="supply_warehouse_create").values_list(
        "new_data_json", flat=True
    )
    assert {row["code"] for row in recorded} == set(codes)


def test_manual_and_automatic_creation_cannot_commit_duplicate_codes():
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL transaction locks")
    company = make_company("MIXED")
    actor = make_user("mixed-warehouse", "warehouse")
    barrier = Barrier(2)

    def create(manual):
        close_old_connections()
        try:
            barrier.wait(timeout=15)
            data = {"name": "测试手填" if manual else "测试自动"}
            if manual:
                data["code"] = "WH000001"
            try:
                return create_supply_warehouse(actor=actor, company=company, data=data).code
            except ValidationError:
                if manual:
                    return "duplicate-rejected"
                raise
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        manual, automatic = list(executor.map(create, (True, False)))
    assert (manual, automatic) in {
        ("WH000001", "WH000002"), ("duplicate-rejected", "WH000001")
    }
    codes = list(SupplyWarehouse.objects.values_list("normalized_code", flat=True))
    assert len(codes) == len(set(codes))


def test_automatic_insert_retries_when_manual_number_commits_after_validation(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL transaction locks")
    company = make_company("INSERTRACE")
    actor = make_user("insert-race-warehouse", "warehouse")
    validated = Event()
    manual_finished = Event()
    insert_attempts = []
    database_conflicts = []
    original_clean = SupplyWarehouse.full_clean
    original_save = SupplyWarehouse.save

    def clean_and_pause(instance, *args, **kwargs):
        original_clean(instance, *args, **kwargs)
        if instance.name == "测试自动抢号" and instance.code == "WH000001":
            validated.set()
            assert manual_finished.wait(timeout=20), "手工保存未在预期时间内结束"

    def observe_insert(instance, *args, **kwargs):
        if instance.name != "测试自动抢号":
            return original_save(instance, *args, **kwargs)
        insert_attempts.append(instance.code)
        try:
            return original_save(instance, *args, **kwargs)
        except IntegrityError as exc:
            database_conflicts.append(exc.__cause__.diag.constraint_name)
            raise

    monkeypatch.setattr(SupplyWarehouse, "full_clean", clean_and_pause)
    monkeypatch.setattr(SupplyWarehouse, "save", observe_insert)

    def create_automatic():
        close_old_connections()
        try:
            return create_supply_warehouse(
                actor=actor, company=company, data={"name": "测试自动抢号"}
            ).code
        finally:
            close_old_connections()

    def occupy_number_manually():
        close_old_connections()
        try:
            assert validated.wait(timeout=20), "自动档案未完成保存前校验"
            return create_supply_warehouse(
                actor=actor,
                company=company,
                data={"code": "WH000001", "name": "测试手工抢先提交"},
            ).code
        finally:
            # The service transaction has committed (or failed) before the
            # automatic thread is allowed to attempt its first real INSERT.
            manual_finished.set()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        automatic = executor.submit(create_automatic)
        manual = executor.submit(occupy_number_manually)
        assert manual.result(timeout=30) == "WH000001"
        assert automatic.result(timeout=30) == "WH000002"

    assert insert_attempts == ["WH000001", "WH000002"]
    assert database_conflicts == ["uq_supply_warehouse_company_code"]
    assert set(SupplyWarehouse.objects.values_list("code", flat=True)) == {
        "WH000001", "WH000002"
    }
    audit_codes = AuditLog.objects.filter(
        action="supply_warehouse_create"
    ).values_list("new_data_json", flat=True)
    assert sorted(row["code"] for row in audit_codes) == ["WH000001", "WH000002"]


def test_automatic_create_and_manual_edit_cannot_commit_duplicate_codes():
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL transaction locks")
    company = make_company("EDITRACE")
    actor = make_user("edit-race-warehouse", "warehouse")
    original = create_supply_warehouse(actor=actor, company=company, data={"name": "测试原仓库"})
    barrier = Barrier(2)

    def write(edit):
        close_old_connections()
        try:
            barrier.wait(timeout=15)
            try:
                if edit:
                    return update_supply_warehouse(
                        actor=actor, warehouse=original, data={"code": "WH000002"}
                    ).code
                return create_supply_warehouse(
                    actor=actor, company=company, data={"name": "测试新增仓库"}
                ).code
            except ValidationError:
                if edit:
                    return "duplicate-rejected"
                raise
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        edited, automatic = list(executor.map(write, (True, False)))
    assert (edited, automatic) in {
        ("WH000002", "WH000003"), ("duplicate-rejected", "WH000002")
    }
    original.refresh_from_db()
    assert original.code == ("WH000001" if edited == "duplicate-rejected" else "WH000002")


def test_failed_save_does_not_reserve_number_or_leave_audit():
    company = make_company("ROLLBACK")
    actor = make_user("rollback-warehouse", "warehouse")
    with pytest.raises(ValidationError):
        create_supply_warehouse(actor=actor, company=company, data={"name": ""})
    assert not SupplyWarehouse.objects.exists()
    assert not AuditLog.objects.filter(action="supply_warehouse_create").exists()
    item = create_supply_warehouse(actor=actor, company=company, data={"name": "测试有效仓库"})
    assert item.code == "WH000001"


def test_number_scope_includes_company_and_inactive_records():
    first = make_company("ONE")
    second = make_company("TWO", active=False)
    SupplyWarehouse.objects.create(company=first, code="ＷＨ０００００９", name="测试旧仓库", is_active=False)
    first_new = SupplyWarehouse(company=first, name="测试新仓库")
    second_new = SupplyWarehouse(company=second, name="测试另一公司")
    with transaction.atomic():
        prepare_auto_number(first_new)
        prepare_auto_number(second_new)
    assert first_new.code == "WH000010"
    assert second_new.code == "WH000001"


def test_exhausted_automatic_range_reports_error_but_allows_manual():
    company = make_company("FULL")
    actor = make_user("full-warehouse", "warehouse")
    create_supply_warehouse(actor=actor, company=company, data={"code": "WH999999", "name": "测试末号"})
    with pytest.raises(ValidationError, match="流水已用完"):
        create_supply_warehouse(actor=actor, company=company, data={"name": "测试越界"})
    manual = create_supply_warehouse(actor=actor, company=company, data={"code": "仓库自编-1", "name": "测试手填"})
    assert manual.code == "仓库自编-1"


def test_major_category_two_digit_range_respects_child_and_inactive_codes():
    company = make_company("MAJOR")
    actor = make_user("major-admin", "system_admin")
    root = create_asset_category(actor=actor, company=company, data={"code": "99", "name": "其他"})
    create_asset_category(actor=actor, company=company, data={"code": "01", "name": "测试停用分类", "is_active": False})
    create_asset_category(actor=actor, company=company, data={"code": "02", "name": "测试子类", "parent": root})
    result = create_asset_category(actor=actor, company=company, data={"name": "测试自动一级分类"})
    assert result.code == "03"
    child = create_asset_category(actor=actor, company=company, data={"name": "测试自动子类", "parent": result})
    assert child.code == "CAT000001"
    for number in range(4, 99):
        AssetCategory.objects.create(company=company, code=f"{number:02d}", name=f"测试分类 {number}")
    with pytest.raises(ValidationError, match="全部占用"):
        create_asset_category(actor=actor, company=company, data={"name": "测试超限分类"})


def test_allocator_rejects_use_outside_transaction():
    company = make_company("ATOMIC")
    with pytest.raises(RuntimeError, match="保存事务"):
        prepare_auto_number(SupplyWarehouse(company=company, name="测试仓库"))
