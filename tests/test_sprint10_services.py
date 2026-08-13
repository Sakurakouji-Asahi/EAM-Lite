from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.lifecycle_services import loan_asset, transfer_asset
from apps.assets.models import AssetMovement
from apps.audit.models import AuditLog
from apps.offboarding import services as clearance_services
from apps.offboarding.models import (
    EmployeeAssetClearance,
    EmployeeAssetClearanceItem,
)
from tests.test_sprint10_support import (
    active_internal_loan,
    additional_employee,
    formal_asset,
    offboarding_context,
)


pytestmark = pytest.mark.django_db


def _transfer(context, asset, employee, key):
    return transfer_asset(
        actor=context["equipment"],
        asset=asset,
        to_department=employee.department,
        to_responsible_employee=employee,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="Sprint 10 责任转交",
        idempotency_key=key,
        expected_status=asset.asset_status,
    )


def test_initiation_snapshots_responsibility_loan_both_and_pending_label_qr_time():
    context = offboarding_context("S10SRC")
    receiver = additional_employee(context, "S10SRC-R")
    responsibility, _ = formal_asset(context, "S10SRC-RESP")
    loan_only, _ = formal_asset(
        context, "S10SRC-LOAN", employee=receiver
    )
    loan = active_internal_loan(
        context, loan_only, context["employee"], "S10SRC-LOAN"
    )
    both, _ = formal_asset(context, "S10SRC-BOTH")
    both_loan = active_internal_loan(
        context, both, context["employee"], "S10SRC-BOTH"
    )
    pending, pending_qr = formal_asset(
        context, "S10SRC-PEND", activate=False
    )

    clearance = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10SRC-init",
        remark="发起快照",
    )

    items = {item.asset_id: item for item in clearance.items.all()}
    assert set(items) == {
        responsibility.pk,
        loan_only.pk,
        both.pk,
        pending.pk,
    }
    assert items[responsibility.pk].source_type == "responsibility"
    assert items[responsibility.pk].source_loan_id is None
    assert items[loan_only.pk].source_type == "internal_loan"
    assert items[loan_only.pk].source_loan_id == loan.pk
    assert items[both.pk].source_type == "both"
    assert items[both.pk].source_loan_id == both_loan.pk
    assert items[pending.pk].source_type == "responsibility"
    assert items[pending.pk].association_effective_at == pending_qr.issued_at
    loan_effective_at = datetime.combine(
        loan.loan_date, time.min, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    both_loan_effective_at = datetime.combine(
        both_loan.loan_date, time.min, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert items[loan_only.pk].association_effective_at == loan_effective_at
    assert items[both.pk].association_effective_at == max(
        both_loan_effective_at,
        AssetMovement.objects.filter(
            asset=both,
            movement_type="label_activation",
            to_employee=context["employee"],
        ).latest("effective_at").effective_at,
    )
    assert items[responsibility.pk].asset_code_snapshot == responsibility.asset_code
    assert items[responsibility.pk].asset_name_snapshot == responsibility.asset_name
    assert items[responsibility.pk].original_department_id == responsibility.department_id
    assert items[responsibility.pk].original_employee_id == context["employee"].pk
    assert items[responsibility.pk].original_location_id == responsibility.location_id
    assert items[pending.pk].original_status == "pending_label"
    assert clearance.total_assets_snapshot == 4
    assert clearance.unresolved_assets == 4
    assert clearance.status == "blocked"
    context["employee"].refresh_from_db()
    assert context["employee"].employment_status == "leaving"
    assert context["employee"].is_active is False
    assert AuditLog.objects.filter(
        action="employee_offboarding.initiated",
        object_id=str(clearance.pk),
    ).exists()


def test_initiation_is_atomic_idempotent_and_clears_manager_assignment(monkeypatch):
    context = offboarding_context("S10ATM")
    formal_asset(context, "S10ATM-A")
    department = context["department"]
    department.manager_employee = context["employee"]
    department.save(update_fields=["manager_employee", "updated_at"])
    original_create = clearance_services._create_item

    def fail_after_employee_transition(*args, **kwargs):
        raise RuntimeError("forced snapshot failure")

    monkeypatch.setattr(clearance_services, "_create_item", fail_after_employee_transition)
    with pytest.raises(RuntimeError, match="forced snapshot failure"):
        clearance_services.initiate_clearance(
            actor=context["hr"],
            employee=context["employee"],
            idempotency_key="S10ATM-init",
        )

    context["employee"].refresh_from_db()
    department.refresh_from_db()
    assert context["employee"].employment_status == "active"
    assert context["employee"].is_active is True
    assert department.manager_employee_id == context["employee"].pk
    assert EmployeeAssetClearance.objects.count() == 0
    assert not AuditLog.objects.filter(
        action="employee_offboarding.initiated"
    ).exists()

    monkeypatch.setattr(clearance_services, "_create_item", original_create)
    first = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10ATM-init",
    )
    retried = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10ATM-retry-key",
        remark="a later duplicate request",
    )
    department.refresh_from_db()
    assert retried.pk == first.pk
    assert EmployeeAssetClearance.objects.count() == 1
    assert department.manager_employee_id is None


def test_non_hr_cannot_initiate_refresh_supplement_or_complete():
    context = offboarding_context("S10PERM")
    with pytest.raises(PermissionDenied):
        clearance_services.initiate_clearance(
            actor=context["finance"],
            employee=context["employee"],
            idempotency_key="S10PERM-init",
        )
    clearance = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10PERM-init",
    )
    for actor in (
        context["finance"],
        context["equipment"],
        context["management"],
        context["admin"],
    ):
        with pytest.raises(PermissionDenied):
            clearance_services.refresh_clearance(
                actor=actor, clearance=clearance, reason="unauthorized"
            )
        with pytest.raises(PermissionDenied):
            clearance_services.complete_clearance(
                actor=actor,
                clearance=clearance,
                termination_date=timezone.localdate(),
            )


def test_refresh_adds_only_historical_miss_with_reason_and_skips_post_initiation_corruption(
    monkeypatch,
):
    context = offboarding_context("S10REF")
    receiver = additional_employee(context, "S10REF-R")
    historical, _ = formal_asset(context, "S10REF-H")
    post_init, _ = formal_asset(context, "S10REF-P", employee=receiver)
    real_collect = clearance_services._collect_sources
    historical_sources = real_collect(context["employee"])
    monkeypatch.setattr(clearance_services, "_collect_sources", lambda _employee: {})
    clearance = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10REF-init",
    )
    assert clearance.items.count() == 0
    post_entry = {
        "asset": post_init,
        "responsibility_at": clearance.initiated_at + timedelta(seconds=1),
        "loan": None,
        "loan_at": None,
    }
    monkeypatch.setattr(
        clearance_services,
        "_collect_sources",
        lambda _employee: {**historical_sources, post_init.pk: post_entry},
    )

    refreshed = clearance_services.refresh_clearance(
        actor=context["hr"],
        clearance=clearance,
        reason="核对发现历史数据遗漏",
    )
    items = list(refreshed.items.all())
    assert len(items) == 1
    assert items[0].asset_id == historical.pk
    assert items[0].added_during_clearance is True
    assert items[0].addition_reason == "核对发现历史数据遗漏"
    assert items[0].discovered_at > clearance.initiated_at
    audit = AuditLog.objects.get(
        action="employee_offboarding.refreshed",
        object_id=str(clearance.pk),
    )
    assert str(post_init.pk) in audit.new_data_json["post_initiation_asset_ids_skipped"]
    with pytest.raises(ValidationError):
        clearance_services.complete_clearance(
            actor=context["hr"],
            clearance=clearance,
            termination_date=timezone.localdate(),
        )


def test_completed_original_can_create_one_historical_supplement_without_rewriting_date(
    monkeypatch,
):
    context = offboarding_context("S10SUP")
    receiver = additional_employee(context, "S10SUP-R")
    missed, _ = formal_asset(context, "S10SUP-A")
    real_collect = clearance_services._collect_sources
    missed_sources = real_collect(context["employee"])
    monkeypatch.setattr(clearance_services, "_collect_sources", lambda _employee: {})
    original = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10SUP-init",
    )
    termination = timezone.localdate() - timedelta(days=1)
    original = clearance_services.complete_clearance(
        actor=context["hr"],
        clearance=original,
        termination_date=termination,
    )
    assert original.status == "completed"

    monkeypatch.setattr(
        clearance_services,
        "_collect_sources",
        lambda _employee: missed_sources,
    )
    supplement = clearance_services.create_supplemental_clearance(
        actor=context["hr"],
        original_clearance=original,
        reason="历史数据遗漏复核",
        idempotency_key="S10SUP-supplement",
        remark="补充清退",
    )
    assert supplement.supplements_clearance_id == original.pk
    assert supplement.supplement_reason == "历史数据遗漏复核"
    assert supplement.items.get().asset_id == missed.pk
    assert supplement.items.get().added_during_clearance is False
    assert clearance_services.create_supplemental_clearance(
        actor=context["hr"],
        original_clearance=original,
        reason="历史数据遗漏复核",
        idempotency_key="S10SUP-supplement",
        remark="补充清退",
    ).pk == supplement.pk
    with pytest.raises(ValidationError):
        clearance_services.create_supplemental_clearance(
            actor=context["hr"],
            original_clearance=original,
            reason="不同理由",
            idempotency_key="S10SUP-supplement",
        )

    monkeypatch.setattr(clearance_services, "_collect_sources", real_collect)
    clearance_services.transfer_clearance_item(
        actor=context["equipment"],
        item=supplement.items.get(),
        to_department=receiver.department,
        to_responsible_employee=receiver,
        to_location=context["location"],
        effective_at=timezone.now(),
        reason="补充清退转交",
        idempotency_key="S10SUP-transfer",
    )
    clearance_services.complete_clearance(
        actor=context["hr"], clearance=supplement
    )
    context["employee"].refresh_from_db()
    assert context["employee"].employment_status == "resigned"
    assert context["employee"].termination_date == termination


def test_completion_validates_date_and_is_idempotent():
    context = offboarding_context("S10CMP")
    clearance = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10CMP-init",
    )
    with pytest.raises(ValidationError):
        clearance_services.complete_clearance(
            actor=context["hr"], clearance=clearance
        )
    with pytest.raises(ValidationError):
        clearance_services.complete_clearance(
            actor=context["hr"],
            clearance=clearance,
            termination_date=context["employee"].hire_date - timedelta(days=1),
        )
    with pytest.raises(ValidationError):
        clearance_services.complete_clearance(
            actor=context["hr"],
            clearance=clearance,
            termination_date=timezone.localdate() + timedelta(days=1),
        )
    context["employee"].refresh_from_db()
    assert context["employee"].employment_status == "leaving"

    completed = clearance_services.complete_clearance(
        actor=context["hr"],
        clearance=clearance,
        termination_date=timezone.localdate(),
    )
    assert completed.status == "completed"
    assert completed.completed_at is not None
    assert completed.completed_by_id == context["hr"].pk
    assert clearance_services.complete_clearance(
        actor=context["hr"],
        clearance=completed,
        termination_date=timezone.localdate(),
    ).pk == completed.pk
    context["employee"].refresh_from_db()
    assert context["employee"].employment_status == "resigned"
    assert context["employee"].termination_date == timezone.localdate()


def test_leaving_employee_cannot_receive_new_assignment_or_internal_loan():
    context = offboarding_context("S10BYP")
    receiver = additional_employee(context, "S10BYP-R")
    assignment_asset, _ = formal_asset(
        context, "S10BYP-A", employee=receiver
    )
    loan_asset_row, _ = formal_asset(
        context, "S10BYP-L", employee=receiver
    )
    clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10BYP-init",
    )
    with pytest.raises(ValidationError):
        _transfer(
            context,
            assignment_asset,
            context["employee"],
            "S10BYP-illegal-transfer",
        )
    with pytest.raises(ValidationError):
        loan_asset(
            actor=context["equipment"],
            asset=loan_asset_row,
            borrower_type="internal_employee",
            borrower_employee=context["employee"],
            loan_date=timezone.localdate(),
            expected_return_date=timezone.localdate() + timedelta(days=7),
            handled_by=context["equipment"],
            reason="尝试借给离职中员工",
            idempotency_key="S10BYP-illegal-loan",
            expected_status=loan_asset_row.asset_status,
        )


def test_clearance_and_item_snapshots_reject_ordinary_model_mutation():
    context = offboarding_context("S10IMM")
    asset, _ = formal_asset(context, "S10IMM-A")
    clearance = clearance_services.initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S10IMM-init",
    )
    item = clearance.items.get(asset=asset)
    clearance.remark = "mutated"
    with pytest.raises(ValidationError):
        clearance.save()
    with pytest.raises(ValidationError):
        EmployeeAssetClearance.objects.filter(pk=clearance.pk).update(
            status="completed"
        )
    item.asset_name_snapshot = "mutated"
    with pytest.raises(ValidationError):
        item.save()
    with pytest.raises(ValidationError):
        EmployeeAssetClearanceItem.objects.filter(pk=item.pk).update(
            resolution="returned"
        )
    with pytest.raises(ValidationError):
        clearance.delete()
    with pytest.raises(ValidationError):
        item.delete()
