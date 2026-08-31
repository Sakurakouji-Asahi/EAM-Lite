from datetime import date, timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections, close_old_connections
from django.urls import reverse
from django.utils import timezone

from apps.masterdata.models import Department, Employee, UserDepartmentScope
from apps.offboarding.services import complete_clearance, initiate_clearance
from apps.supplies import services as supply_services
from apps.supplies.domain import quantize_money, quantize_quantity, quantize_unit_cost
from apps.supplies.models import (
    EmployeeSupplyClearanceItem,
    SupplyCountTask,
    SupplyCustody,
    SupplyItem,
    SupplyStockBalance,
)
from apps.supplies.reconciliation import rebuild_custodies, rebuild_stock_balances
from apps.supplies.services import (
    deactivate_supply_item,
    deactivate_supply_warehouse,
    post_supply_document,
    return_custody_to_warehouse,
    reverse_supply_document,
    transfer_custody,
    update_draft_document,
)
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import (
    make_company,
    make_department,
    make_employee,
    make_issue_document,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
    seed_supply_stock,
)


pytestmark = pytest.mark.django_db


def _thread_call(function):
    close_old_connections()
    try:
        return function()
    finally:
        connections.close_all()


@pytest.mark.parametrize(
    "quantizer",
    (quantize_quantity, quantize_unit_cost, quantize_money),
)
def test_decimal_quantizers_reject_values_outside_supported_context(quantizer):
    with pytest.raises(ValidationError, match="超出系统支持"):
        quantizer(Decimal("1E+1000"))


def _issued_durable(*, quantity="2", key_prefix="hardening"):
    company, actor, department, employee, source, target, _, durable = (
        supply_context()
    )
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity=quantity,
        unit_cost="80",
        key=f"{key_prefix}-stock",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity=quantity,
        key=f"{key_prefix}-issue",
    )
    post_supply_document(document=issue, actor=actor)
    custody = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    return company, actor, department, employee, source, target, durable, issue, custody


def test_transferred_durable_return_draft_can_be_updated_without_issue_source():
    company, actor, _, _, _, target_warehouse, durable, _, custody = (
        _issued_durable(key_prefix="hardening-edit-return")
    )
    target_department = make_department(company, "RETURN-TARGET")
    target_employee = make_employee(company, target_department, "RETURN-E")
    transferred = transfer_custody(
        custody=custody,
        quantity=Decimal("1"),
        target_department=target_department,
        target_employee=target_employee,
        business_date=date(2026, 8, 31),
        reason="转交后归还",
        actor=actor,
        idempotency_key="hardening-edit-return-transfer",
    )
    assert transferred.origin_issue_line_id is None

    document = return_custody_to_warehouse(
        custody=transferred,
        target_warehouse=target_warehouse,
        quantity=Decimal("0.5"),
        business_date=date(2026, 8, 31),
        reason="原归还原因",
        actor=actor,
        idempotency_key="hardening-edit-return-document",
    )
    updated = update_draft_document(
        actor=actor,
        document=document,
        data={"remark": "修正后的归还原因"},
        lines=[
            {
                "item": durable,
                "quantity": Decimal("0.5"),
                "entered_unit_cost": None,
                "source_issue_line": None,
                "source_custody": transferred,
                "line_remark": "修正后的归还原因",
            }
        ],
    )

    assert updated.remark == "修正后的归还原因"
    line = updated.lines.get()
    assert line.source_custody_id == transferred.pk
    assert line.source_issue_line_id is None


def test_issue_post_revalidates_and_rejects_disabled_responsibility_department():
    company, actor, department, _, source, _, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity="2",
        unit_cost="80",
        key="hardening-disabled-department-stock",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=None,
        quantity="1",
        key="hardening-disabled-department-issue",
    )
    department.is_active = False
    department.save(update_fields=("is_active", "updated_at"))

    with pytest.raises(ValidationError, match="领用部门.*启用"):
        post_supply_document(document=issue, actor=actor)

    issue.refresh_from_db()
    balance = SupplyStockBalance.objects.get(warehouse=source, item=durable)
    assert issue.status == "draft"
    assert balance.quantity_on_hand == Decimal("2.0000")
    assert not SupplyCustody.objects.filter(origin_issue_line__document=issue).exists()


def test_deactivation_cannot_strand_stock_or_open_durable_custody():
    company, actor, department, employee, source, _, _, durable = supply_context()
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity="2",
        unit_cost="80",
        key="hardening-deactivate-stock",
    )

    with pytest.raises(ValidationError, match="仍有库存数量"):
        deactivate_supply_warehouse(
            actor=actor,
            warehouse=source,
            reason="错误停用",
        )
    with pytest.raises(ValidationError, match="仍有仓库库存"):
        deactivate_supply_item(
            actor=actor,
            item=durable,
            reason="错误停用",
        )

    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity="2",
        key="hardening-deactivate-issue",
    )
    post_supply_document(document=issue, actor=actor)
    assert SupplyStockBalance.objects.get(
        warehouse=source, item=durable
    ).quantity_on_hand == Decimal("0.0000")
    with pytest.raises(ValidationError, match="未结清.*保管"):
        deactivate_supply_item(
            actor=actor,
            item=durable,
            reason="仍有个人保管",
        )

    source.refresh_from_db()
    durable.refresh_from_db()
    assert source.is_active is True
    assert durable.is_active is True


@pytest.mark.django_db(transaction=True)
def test_issue_post_and_offboarding_serialize_on_responsible_employee(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("责任员工行锁并发门禁需要 PostgreSQL。")
    company, actor, department, employee, source, _, _, durable = supply_context()
    employee.hire_date = timezone.localdate() - timedelta(days=30)
    employee.save(update_fields=("hire_date", "updated_at"))
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        quantity="2",
        unit_cost="80",
        key="hardening-concurrent-offboarding-stock",
    )
    issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=source,
        item=durable,
        department=department,
        employee=employee,
        quantity="1",
        key="hardening-concurrent-offboarding-issue",
    )
    hr = make_user("hardening-concurrent-offboarding-hr", "hr")
    responsibility_locked = Event()
    release_post = Event()
    original_lock = supply_services._lock_and_validate_issue_responsibility

    def paused_lock(document):
        result = original_lock(document)
        responsibility_locked.set()
        assert release_post.wait(10)
        return result

    monkeypatch.setattr(
        supply_services,
        "_lock_and_validate_issue_responsibility",
        paused_lock,
    )

    def run_post():
        local_issue = supply_services.SupplyDocument.objects.get(pk=issue.pk)
        local_actor = get_user_model().objects.get(pk=actor.pk)
        return post_supply_document(document=local_issue, actor=local_actor)

    def run_offboarding():
        local_employee = Employee.objects.get(pk=employee.pk)
        local_hr = get_user_model().objects.get(pk=hr.pk)
        return initiate_clearance(
            actor=local_hr,
            employee=local_employee,
            idempotency_key="hardening-concurrent-offboarding-clearance",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        post_future = pool.submit(_thread_call, run_post)
        assert responsibility_locked.wait(10)
        clearance_future = pool.submit(_thread_call, run_offboarding)
        time.sleep(0.2)
        assert not clearance_future.done()
        release_post.set()
        post_future.result(timeout=15)
        clearance = clearance_future.result(timeout=15)

    custody = SupplyCustody.objects.get(origin_issue_line__document=issue)
    assert EmployeeSupplyClearanceItem.objects.filter(
        clearance=clearance,
        custody=custody,
        resolution="pending",
    ).exists()
    clearance.refresh_from_db()
    assert clearance.unresolved_supply_custodies == 1


@pytest.mark.parametrize("competing_action", ("transfer", "count_publish"))
@pytest.mark.django_db(transaction=True)
def test_employee_department_lock_order_avoids_deadlock(
    monkeypatch, competing_action
):
    if connection.vendor != "postgresql":
        pytest.skip("Employee→Department 死锁回归需要 PostgreSQL。")
    company, actor, source_department, source_employee, warehouse, _, _, durable = (
        supply_context()
    )
    target_department = make_department(company, f"LOCK-{competing_action}")
    target_employee = make_employee(
        company, target_department, f"LOCK-{competing_action}-E"
    )
    equipment = make_user(f"lock-{competing_action}-equipment", "equipment")
    seed_supply_stock(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="6",
        unit_cost="80",
        key=f"lock-{competing_action}-stock",
    )
    source_issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=source_department,
        employee=source_employee,
        quantity="1",
        key=f"lock-{competing_action}-source",
    )
    post_supply_document(document=source_issue, actor=actor)
    source_custody = SupplyCustody.objects.get(
        origin_issue_line=source_issue.lines.get()
    )
    task = None
    if competing_action == "count_publish":
        target_seed = make_issue_document(
            actor=actor,
            company=company,
            warehouse=warehouse,
            item=durable,
            department=target_department,
            employee=target_employee,
            quantity="1",
            key="lock-count-publish-target-seed",
        )
        post_supply_document(document=target_seed, actor=actor)
        task = supply_services.create_supply_count_task(
            actor=equipment,
            company=company,
            data={
                "name": "员工保管锁序盘点",
                "count_domain": "custody",
                "department": target_department,
                "employee": target_employee,
                "planned_start": date(2026, 8, 31),
                "planned_end": date(2026, 8, 31),
                "idempotency_key": "lock-count-publish-task",
            },
        )
    pending_issue = make_issue_document(
        actor=actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=target_department,
        employee=target_employee,
        quantity="1",
        key=f"lock-{competing_action}-pending",
    )

    employee_locked = Event()
    release_issue = Event()
    original_lock = supply_services._lock_supply_employee

    def pause_first_employee_lock(**kwargs):
        result = original_lock(**kwargs)
        if not employee_locked.is_set():
            employee_locked.set()
            assert release_issue.wait(10)
        return result

    monkeypatch.setattr(
        supply_services,
        "_lock_supply_employee",
        pause_first_employee_lock,
    )

    def run_issue():
        local_document = supply_services.SupplyDocument.objects.get(
            pk=pending_issue.pk
        )
        local_actor = get_user_model().objects.get(pk=actor.pk)
        return post_supply_document(document=local_document, actor=local_actor)

    def run_competitor():
        if competing_action == "transfer":
            return transfer_custody(
                custody=SupplyCustody.objects.get(pk=source_custody.pk),
                quantity=Decimal("1"),
                target_department=Department.objects.get(pk=target_department.pk),
                target_employee=Employee.objects.get(pk=target_employee.pk),
                business_date=date(2026, 8, 31),
                reason="验证员工部门锁序",
                actor=get_user_model().objects.get(pk=actor.pk),
                idempotency_key="lock-transfer-competitor",
            )
        return supply_services.publish_supply_count_task(
            task=SupplyCountTask.objects.get(pk=task.pk),
            actor=get_user_model().objects.get(pk=equipment.pk),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        issue_future = pool.submit(_thread_call, run_issue)
        assert employee_locked.wait(10)
        competing_future = pool.submit(_thread_call, run_competitor)
        time.sleep(0.2)
        was_blocked = not competing_future.done()
        release_issue.set()
        assert was_blocked
        issue_future.result(timeout=15)
        competing_future.result(timeout=15)


@pytest.mark.parametrize("count_action", ("add_item", "close_count"))
@pytest.mark.django_db(transaction=True)
def test_warehouse_item_lock_order_avoids_deadlock(monkeypatch, count_action):
    if connection.vendor != "postgresql":
        pytest.skip("Warehouse→Item 死锁回归需要 PostgreSQL。")
    company, actor, _, _, warehouse, _, consumable, _ = supply_context()
    if count_action == "close_count":
        seed_supply_stock(
            actor=actor,
            company=company,
            warehouse=warehouse,
            item=consumable,
            quantity="1",
            unit_cost="80",
            key="lock-close-count-stock",
        )
    receipt = supply_services.create_supply_document(
        actor=actor,
        company=company,
        document_type="receipt",
        data={
            "business_date": date(2026, 8, 31),
            "target_warehouse": warehouse,
            "idempotency_key": f"lock-{count_action}-receipt",
        },
        lines=[
            {
                "item": consumable,
                "quantity": Decimal("1"),
                "entered_unit_cost": Decimal("80"),
            }
        ],
    )
    task = supply_services.create_supply_count_task(
        actor=actor,
        company=company,
        data={
            "name": f"仓库物品锁序 {count_action}",
            "count_domain": "warehouse_stock",
            "warehouse": warehouse,
            "planned_start": date(2026, 8, 31),
            "planned_end": date(2026, 8, 31),
            "idempotency_key": f"lock-{count_action}-task",
        },
    )
    supply_services.publish_supply_count_task(task=task, actor=actor)
    if count_action == "close_count":
        line = task.lines.get(item=consumable)
        supply_services.record_supply_count(
            line=line,
            counted_quantity=Decimal("2"),
            remark="盘盈一件",
            actor=actor,
        )
        supply_services.stop_supply_count_entry(task=task, actor=actor)

    warehouse_locked = Event()
    release_count = Event()
    original_lock = supply_services._lock_supply_warehouse

    def pause_first_warehouse_lock(**kwargs):
        result = original_lock(**kwargs)
        if not warehouse_locked.is_set():
            warehouse_locked.set()
            assert release_count.wait(10)
        return result

    monkeypatch.setattr(
        supply_services,
        "_lock_supply_warehouse",
        pause_first_warehouse_lock,
    )

    def run_count_action():
        local_task = SupplyCountTask.objects.get(pk=task.pk)
        local_actor = get_user_model().objects.get(pk=actor.pk)
        if count_action == "add_item":
            return supply_services.add_supply_count_item(
                task=local_task,
                item=SupplyItem.objects.get(pk=consumable.pk),
                actor=local_actor,
            )
        return supply_services.close_supply_count_task(
            task=local_task,
            actor=local_actor,
        )

    def run_post():
        return post_supply_document(
            document=supply_services.SupplyDocument.objects.get(pk=receipt.pk),
            actor=get_user_model().objects.get(pk=actor.pk),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        count_future = pool.submit(_thread_call, run_count_action)
        assert warehouse_locked.wait(10)
        post_future = pool.submit(_thread_call, run_post)
        time.sleep(0.2)
        was_blocked = not post_future.done()
        release_count.set()
        assert was_blocked
        count_future.result(timeout=15)
        if count_action == "add_item":
            with pytest.raises(ValidationError, match="盘点"):
                post_future.result(timeout=15)
        else:
            post_future.result(timeout=15)
            balance = SupplyStockBalance.objects.get(
                warehouse=warehouse,
                item=consumable,
            )
            assert balance.quantity_on_hand == Decimal("3.0000")


def test_issue_reversal_is_blocked_after_custody_enters_offboarding_clearance():
    _, actor, _, employee, _, _, _, issue, custody = _issued_durable(
        key_prefix="hardening-clearance-reversal"
    )
    employee.hire_date = timezone.localdate() - timedelta(days=30)
    employee.save(update_fields=("hire_date", "updated_at"))
    hr = make_user("hardening-clearance-hr", "hr")
    clearance = initiate_clearance(
        actor=hr,
        employee=employee,
        idempotency_key="hardening-clearance-init",
    )
    assert EmployeeSupplyClearanceItem.objects.filter(
        clearance=clearance, custody=custody, resolution="pending"
    ).exists()

    with pytest.raises(ValidationError, match="离职清退"):
        reverse_supply_document(
            document=issue,
            actor=actor,
            idempotency_key="hardening-clearance-issue-reversal",
            reason="错误领用",
        )

    issue.refresh_from_db()
    custody.refresh_from_db()
    clearance.refresh_from_db()
    assert issue.status == "posted"
    assert custody.status == "open"
    assert clearance.unresolved_supply_custodies == 1


@pytest.mark.django_db(transaction=True)
def test_return_reversal_does_not_restore_custody_to_resigned_employee():
    _, actor, _, employee, _, target, _, _, custody = _issued_durable(
        key_prefix="hardening-resigned-return"
    )
    returned = return_custody_to_warehouse(
        custody=custody,
        target_warehouse=target,
        quantity=Decimal("2"),
        business_date=timezone.localdate(),
        reason="全部归还",
        actor=actor,
        idempotency_key="hardening-resigned-return-document",
    )
    post_supply_document(document=returned, actor=actor)
    custody.refresh_from_db()
    assert custody.status == "closed"

    employee.hire_date = timezone.localdate() - timedelta(days=30)
    employee.save(update_fields=("hire_date", "updated_at"))
    hr = make_user("hardening-resigned-hr", "hr")
    clearance = initiate_clearance(
        actor=hr,
        employee=employee,
        idempotency_key="hardening-resigned-clearance",
    )
    assert clearance.total_supply_custodies_snapshot == 0
    complete_clearance(
        actor=hr,
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    employee.refresh_from_db()
    assert employee.employment_status == "resigned"

    with pytest.raises(ValidationError, match="责任员工.*离职|不能恢复"):
        reverse_supply_document(
            document=returned,
            actor=actor,
            idempotency_key="hardening-resigned-return-reversal",
            reason="尝试恢复",
        )

    returned.refresh_from_db()
    custody.refresh_from_db()
    assert returned.status == "posted"
    assert custody.status == "closed"
    assert custody.current_quantity == Decimal("0.0000")


def test_employee_custody_detail_hides_out_of_scope_responsibility_chain(client):
    company = make_company("HARDENING-SCOPE")
    warehouse_actor = make_user("hardening-scope-warehouse", "warehouse")
    user_a = make_user("hardening-scope-a", "employee")
    user_b = make_user("hardening-scope-b", "employee")
    department_a = make_department(company, "SCOPE-A")
    department_b = make_department(company, "SCOPE-B")
    employee_a = make_employee(company, department_a, "SCOPE-A", user=user_a)
    employee_b = make_employee(company, department_b, "SCOPE-B", user=user_b)
    category = make_supply_category(company, "SCOPE-CATEGORY")
    warehouse = make_supply_warehouse(company, "SCOPE-WH")
    durable = make_supply_item(
        company,
        category,
        "SCOPE-CHAIR",
        item_type="durable_quantity",
        unit="把",
    )
    seed_supply_stock(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        quantity="2",
        unit_cost="80",
        key="hardening-scope-stock",
    )
    issue = make_issue_document(
        actor=warehouse_actor,
        company=company,
        warehouse=warehouse,
        item=durable,
        department=department_a,
        employee=employee_a,
        quantity="2",
        key="hardening-scope-issue",
    )
    post_supply_document(document=issue, actor=warehouse_actor)
    source = SupplyCustody.objects.get(origin_issue_line=issue.lines.get())
    target = transfer_custody(
        custody=source,
        quantity=Decimal("1"),
        target_department=department_b,
        target_employee=employee_b,
        business_date=timezone.localdate(),
        reason="跨部门责任转交",
        actor=warehouse_actor,
        idempotency_key="hardening-scope-transfer",
    )

    client.force_login(user_a)
    source_page = client.get(reverse("supplies:custody-detail", args=[source.pk]))
    assert source_page.status_code == 200
    assert employee_b.name.encode() not in source_page.content
    assert department_b.name.encode() not in source_page.content
    assert str(target.pk).encode() not in source_page.content
    count_list = client.get(reverse("supplies:count-task-list"))
    assert count_list.status_code == 200
    assert warehouse.name.encode() not in count_list.content

    manager = make_user("hardening-scope-manager", "department_manager")
    UserDepartmentScope.objects.create(
        company=company,
        user=manager,
        department=department_a,
        include_descendants=False,
        assigned_by=warehouse_actor,
    )
    client.force_login(manager)
    transfer_form = client.get(
        reverse("supplies:custody-transfer", args=[source.pk])
    )
    assert transfer_form.status_code == 200
    assert employee_a.name.encode() in transfer_form.content
    assert employee_b.name.encode() not in transfer_form.content

    client.force_login(user_b)
    target_page = client.get(reverse("supplies:custody-detail", args=[target.pk]))
    assert target_page.status_code == 200
    assert employee_a.name.encode() not in target_page.content
    assert department_a.name.encode() not in target_page.content
    assert str(source.pk).encode() not in target_page.content


def test_balance_rebuild_services_enforce_backend_role_permission():
    company, warehouse_actor, *_ = supply_context()

    with pytest.raises(PermissionDenied, match="系统管理员或财务"):
        rebuild_stock_balances(
            company=company,
            actor=warehouse_actor,
            reason="越权库存重建",
            confirm=False,
        )
    with pytest.raises(PermissionDenied, match="系统管理员或财务"):
        rebuild_custodies(
            company=company,
            actor=warehouse_actor,
            reason="越权保管重建",
            confirm=False,
        )
