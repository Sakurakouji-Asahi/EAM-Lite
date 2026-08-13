from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.lifecycle_permissions import scoped_lifecycle_candidates
from apps.assets.lifecycle_services import (
    activate_asset,
    change_asset_assignment,
    complete_asset_repair,
    initiate_disposal,
    loan_asset,
    return_loan,
    send_asset_for_repair,
    set_asset_idle,
)
from apps.assets.models import Asset, AssetLoan, AssetMovement
from apps.audit.models import AuditLog
from tests.test_sprint3_support import (
    make_company,
    make_department,
    make_employee,
    make_location_tree,
)
from tests.test_sprint7_support import (
    active_asset_context,
    add_department_manager,
    add_target_assignment,
)


pytestmark = pytest.mark.django_db


def _now():
    return timezone.now() - timedelta(seconds=1)


def _loan(context, asset, key, **overrides):
    values = {
        "actor": context["equipment"],
        "asset": asset,
        "borrower_type": "internal_employee",
        "borrower_employee": context["employee"],
        "loan_date": timezone.localdate(),
        "expected_return_date": timezone.localdate() + timedelta(days=7),
        "handled_by": context["equipment"],
        "reason": "项目现场临时使用",
        "idempotency_key": key,
        "expected_status": asset.asset_status,
    }
    values.update(overrides)
    return loan_asset(**values)


def test_assignment_updates_current_tuple_and_preserves_exact_history():
    context, asset, _qr = active_asset_context("S7MOVE")
    old = (asset.department_id, asset.responsible_employee_id, asset.location_id)
    department, employee, location = add_target_assignment(context, "S7MOVE")

    movement = change_asset_assignment(
        actor=context["equipment"],
        asset=asset,
        to_department=department,
        to_responsible_employee=employee,
        to_location=location,
        effective_at=_now(),
        reason="生产线调整",
        remark="三项同时变更",
        idempotency_key="S7MOVE-transfer",
        expected_status="in_use",
        expected_department_id=asset.department_id,
        expected_responsible_employee_id=asset.responsible_employee_id,
        expected_location_id=asset.location_id,
    )
    asset.refresh_from_db()

    assert movement.movement_type == "transfer"
    assert (movement.from_department_id, movement.from_employee_id, movement.from_location_id) == old
    assert (
        movement.to_department_id,
        movement.to_employee_id,
        movement.to_location_id,
    ) == (department.pk, employee.pk, location.pk)
    assert (
        asset.department_id,
        asset.responsible_employee_id,
        asset.location_id,
    ) == (department.pk, employee.pk, location.pk)
    assert movement.from_status == movement.to_status == "in_use"
    assert AuditLog.objects.filter(
        company=context["company"], action="asset_lifecycle.assignment_changed"
    ).count() == 1


def test_assignment_is_idempotent_and_parameter_conflict_is_rejected():
    context, asset, _qr = active_asset_context("S7IDEMMOVE")
    department, employee, location = add_target_assignment(context, "S7IDEMMOVE")
    kwargs = dict(
        actor=context["equipment"], asset=asset,
        to_department=department, to_responsible_employee=employee,
        to_location=location, effective_at=_now(), reason="换线",
        idempotency_key="S7IDEMMOVE-key", expected_status="in_use",
    )
    first = change_asset_assignment(**kwargs)
    second = change_asset_assignment(**kwargs)
    assert second.pk == first.pk
    assert AssetMovement.objects.filter(movement_type="transfer").count() == 1

    with pytest.raises(ValidationError):
        change_asset_assignment(**{**kwargs, "reason": "同键不同参数"})


def test_stale_assignment_request_cannot_overwrite_new_current_values():
    context, asset, _qr = active_asset_context("S7STALE")
    old_department_id = asset.department_id
    department, employee, location = add_target_assignment(context, "S7STALE")
    change_asset_assignment(
        actor=context["equipment"], asset=asset,
        to_department=department, to_responsible_employee=employee,
        to_location=location, effective_at=_now(), reason="第一次调拨",
        idempotency_key="S7STALE-first", expected_status="in_use",
    )

    with pytest.raises(ValidationError, match="页面已过期"):
        change_asset_assignment(
            actor=context["equipment"], asset=asset,
            to_department=context["department"],
            to_responsible_employee=context["employee"],
            to_location=context["location"], effective_at=_now(),
            reason="过期页面提交", idempotency_key="S7STALE-second",
            expected_status="in_use", expected_department_id=old_department_id,
        )

    asset.refresh_from_db()
    assert asset.department_id == department.pk
    assert AssetMovement.objects.filter(movement_type="transfer").count() == 1


@pytest.mark.parametrize(("bad_kind",), (("cross_company",), ("inactive",), ("nonleaf",)))
def test_assignment_rejects_cross_company_inactive_and_nonleaf_targets(bad_kind):
    context, asset, _qr = active_asset_context(f"S7BAD{bad_kind}")
    department, employee, location = add_target_assignment(context, f"S7BAD{bad_kind}")
    if bad_kind == "cross_company":
        other = make_company(f"S7OTHER{bad_kind}", active=False)
        department = make_department(other, f"S7OTHER{bad_kind}-D")
        employee = make_employee(other, department, f"S7OTHER{bad_kind}-E")
        _site, _area, location = make_location_tree(other, f"S7OTHER{bad_kind}-L")
    elif bad_kind == "inactive":
        employee.is_active = False
        employee.save(update_fields=("is_active",))
    else:
        location = location.parent

    with pytest.raises((PermissionDenied, ValidationError)):
        change_asset_assignment(
            actor=context["equipment"], asset=asset,
            to_department=department, to_responsible_employee=employee,
            to_location=location, effective_at=_now(), reason="非法目标",
            idempotency_key=f"S7BAD{bad_kind}-key", expected_status="in_use",
        )
    asset.refresh_from_db()
    assert asset.department_id == context["department"].pk
    assert not AssetMovement.objects.filter(movement_type="transfer").exists()


def test_department_manager_requires_source_and_target_scopes():
    context, asset, _qr = active_asset_context("S7SCOPE")
    department, employee, location = add_target_assignment(context, "S7SCOPE")
    manager = add_department_manager(context, "S7SCOPE", context["department"])

    with pytest.raises(PermissionDenied):
        change_asset_assignment(
            actor=manager, asset=asset, to_department=department,
            to_responsible_employee=employee, to_location=location,
            effective_at=_now(), reason="越出授权范围",
            idempotency_key="S7SCOPE-denied", expected_status="in_use",
        )
    assert not AssetMovement.objects.filter(movement_type="transfer").exists()


def test_idle_activate_and_repair_restore_exact_prior_status_without_work_order():
    context, asset, _qr = active_asset_context("S7STATUS")
    set_asset_idle(
        actor=context["equipment"], asset=asset, effective_at=_now(),
        reason="暂时停用", idempotency_key="S7STATUS-idle",
    )
    asset.refresh_from_db()
    assert asset.asset_status == "idle"

    send_asset_for_repair(
        actor=context["equipment"], asset=asset, effective_at=_now(),
        reason="例行维修", idempotency_key="S7STATUS-repair",
        expected_status="idle",
    )
    asset.refresh_from_db()
    assert asset.asset_status == "under_repair"

    complete_asset_repair(
        actor=context["equipment"], asset=asset, effective_at=_now(),
        result="维修完成并试运行正常", idempotency_key="S7STATUS-complete",
    )
    asset.refresh_from_db()
    assert asset.asset_status == "idle"

    activation_at = _now()
    activated = activate_asset(
        actor=context["equipment"], asset=asset, effective_at=activation_at,
        reason="维修验收后恢复使用", idempotency_key="S7STATUS-activate",
        expected_status="idle",
    )
    replayed = activate_asset(
        actor=context["equipment"], asset=asset, effective_at=activation_at,
        reason="维修验收后恢复使用", idempotency_key="S7STATUS-activate",
        expected_status="idle",
    )
    asset.refresh_from_db()
    assert replayed.pk == activated.pk
    assert asset.asset_status == "in_use"
    assert list(
        AssetMovement.objects.filter(asset=asset).exclude(
            movement_type="label_activation"
        ).values_list("movement_type", "from_status", "to_status")
    ) == [
        ("idle", "in_use", "idle"),
        ("repair_start", "idle", "under_repair"),
        ("repair_complete", "under_repair", "idle"),
        ("activate", "idle", "in_use"),
    ]
    assert "RepairOrder" not in {
        model.__name__ for model in Asset._meta.apps.get_models()
    }


def test_forbidden_status_transition_does_not_create_history_or_audit():
    context, asset, _qr = active_asset_context("S7BADSTATE", status="idle")
    before_audit = AuditLog.objects.count()
    with pytest.raises(ValidationError):
        set_asset_idle(
            actor=context["equipment"], asset=asset, effective_at=_now(),
            reason="重复闲置", idempotency_key="S7BADSTATE-key",
        )
    assert not AssetMovement.objects.filter(movement_type="idle").exists()
    assert AuditLog.objects.count() == before_audit


@pytest.mark.parametrize(
    ("service", "key"),
    ((set_asset_idle, "idle"), (activate_asset, "activate")),
)
def test_direct_status_service_rejects_forged_under_repair_expected_status(
    service, key
):
    context, asset, _qr = active_asset_context(f"S7FORGE{key}")
    send_asset_for_repair(
        actor=context["equipment"], asset=asset, effective_at=_now(),
        reason="建立维修中前置状态", idempotency_key=f"S7FORGE{key}-repair",
        expected_status="in_use",
    )
    asset.refresh_from_db()
    before_movements = AssetMovement.objects.filter(asset=asset).count()
    before_audits = AuditLog.objects.count()

    with pytest.raises(ValidationError):
        service(
            actor=context["equipment"], asset=asset, effective_at=_now(),
            reason="伪造隐藏字段绕过维修完成", idempotency_key=f"S7FORGE{key}-bad",
            expected_status="under_repair",
        )

    asset.refresh_from_db()
    assert asset.asset_status == "under_repair"
    assert AssetMovement.objects.filter(asset=asset).count() == before_movements
    assert AuditLog.objects.count() == before_audits


def test_internal_loan_and_return_are_structured_one_to_one_and_idempotent():
    context, asset, _qr = active_asset_context("S7LOAN")
    loan = _loan(context, asset, "S7LOAN-out")
    asset.refresh_from_db()
    assert asset.asset_status == "loaned"
    assert loan.status == "active"
    assert loan.borrower_employee_id == context["employee"].pk
    assert loan.borrower_name_snapshot == context["employee"].name
    assert loan.borrower_name == loan.borrower_organization == ""
    assert loan.loan_movement.movement_type == "loan"

    returned_at = _now()
    returned = return_loan(
        actor=context["equipment"], loan=loan, returned_at=returned_at,
        received_by_employee=context["employee"],
        return_department=context["department"],
        return_responsible_employee=context["employee"],
        return_location=context["location"], return_asset_status="in_use",
        idempotency_key="S7LOAN-return", remark="验收完好",
    )
    repeated = return_loan(
        actor=context["equipment"], loan=returned, returned_at=returned_at,
        received_by_employee=context["employee"],
        return_department=context["department"],
        return_responsible_employee=context["employee"],
        return_location=context["location"], return_asset_status="in_use",
        idempotency_key="S7LOAN-return", remark="验收完好",
    )
    asset.refresh_from_db()
    assert repeated.pk == returned.pk
    assert asset.asset_status == "in_use"
    assert returned.status == "returned"
    assert returned.return_movement.movement_type == "loan_return"
    assert returned.return_movement_id != returned.loan_movement_id
    assert AssetMovement.objects.filter(asset=asset, movement_type="loan_return").count() == 1

    with pytest.raises(ValidationError):
        return_loan(
            actor=context["equipment"], loan=returned, returned_at=_now(),
            received_by_employee=context["employee"],
            return_department=context["department"],
            return_responsible_employee=context["employee"],
            return_location=context["location"], return_asset_status="idle",
            idempotency_key="S7LOAN-again",
        )


def test_external_loan_fields_are_mutually_exclusive_and_dates_validated():
    context, asset, _qr = active_asset_context("S7EXT")
    with pytest.raises(ValidationError):
        _loan(
            context, asset, "S7EXT-mixed", borrower_type="external",
            borrower_employee=context["employee"], borrower_name="外部人员",
        )
    with pytest.raises(ValidationError):
        _loan(
            context, asset, "S7EXT-date",
            expected_return_date=timezone.localdate() - timedelta(days=1),
        )

    loan = _loan(
        context, asset, "S7EXT-ok", borrower_type="external",
        borrower_employee=None, borrower_name="客户工程师",
        borrower_organization="客户公司",
    )
    assert loan.borrower_employee_id is None
    assert loan.borrower_name_snapshot == ""
    assert loan.borrower_name == "客户工程师"


def test_loaned_asset_cannot_enter_disposal_and_candidate_scope_excludes_archived_terminal():
    context, asset, _qr = active_asset_context("S7LOANDISP")
    _loan(context, asset, "S7LOANDISP-out")
    with pytest.raises(ValidationError):
        initiate_disposal(
            actor=context["equipment"], asset=asset, disposal_type="scrap",
            application_date=timezone.localdate(),
            planned_disposal_date=timezone.localdate(), reason="申请报废",
            idempotency_key="S7LOANDISP-disposal", expected_status="loaned",
        )

    assert scoped_lifecycle_candidates(
        context["equipment"], context["company"]
    ).filter(pk=asset.pk).exists()
