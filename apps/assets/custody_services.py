"""Return third-party physical custody without fabricating a disposal valuation."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.assets.domain import TERMINAL_ASSET_STATUSES
from apps.assets.services import _audit, _controlled_update
from apps.assets.trace_support import (
    trace_request_fingerprint, validate_trace_request, lock_trace_assets, insert_trace_record,
)


def effective_movements(queryset):
    """Voided custody events remain auditable but do not describe business history."""
    return queryset.exclude(movement_type="custody_return_reversal").exclude(
        assetcustodyreturn__reversal__isnull=False,
    )


def _movement(*, actor, asset, kind, effective_at, target_status, reason, key):
    from apps.assets.models import AssetMovement
    if AssetMovement.objects.filter(company=asset.company, idempotency_key=key).exists():
        raise ValidationError("该请求标识已用于其他资产操作。")
    movement = AssetMovement(company=asset.company, asset=asset, movement_type=kind, effective_at=effective_at,
        from_department=asset.department, to_department=asset.department,
        from_employee=asset.responsible_employee, to_employee=asset.responsible_employee,
        from_location=asset.location, to_location=asset.location,
        from_status=asset.asset_status, to_status=target_status, reason=reason,
        idempotency_key=key, operated_by=actor)
    movement.full_clean()
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('eam_lite.controlled_asset_movement_insert','on',true)")
    movement.save()
    return movement


@transaction.atomic
def return_custody_asset(*, actor, asset, returned_on, counterparty, contract_reference,
                        acceptance_evidence, reason, idempotency_key, request=None):
    from apps.assets.models import Asset, AssetCustodyReturn, AssetDisposal, AssetMovement
    from apps.finance.models import AssetFinance, AssetDepreciationProfile, DepreciationEntry, AssetValueAdjustment

    key, reason = validate_trace_request(idempotency_key, reason)
    company, rows = lock_trace_assets(actor, [asset])
    asset = rows[asset.pk]
    if isinstance(returned_on, datetime) or not isinstance(returned_on, date) or returned_on > timezone.localdate():
        raise ValidationError({"returned_on": "请填写已经发生的归还日期。"})
    if asset.acquisition_date and returned_on < asset.acquisition_date:
        raise ValidationError({"returned_on": "归还日期不能早于已记录的取得日期。"})
    values = {"counterparty": str(counterparty or "").strip(), "contract_reference": str(contract_reference or "").strip(),
              "acceptance_evidence": str(acceptance_evidence or "").strip()}
    if any(not value or len(value) > (1000 if field == "acceptance_evidence" else 200) for field, value in values.items()):
        raise ValidationError("请填写接收方、合同或受托依据，以及归还验收依据。")
    payload = {"asset": str(asset.pk), "returned_on": returned_on.isoformat(), "reason": reason, **values}
    fingerprint = trace_request_fingerprint(payload)
    existing = AssetCustodyReturn.objects.filter(company=company, idempotency_key=key).first()
    if existing:
        if existing.request_hash != fingerprint:
            raise ValidationError("相同请求已用于不同的归还记录。")
        return existing
    if asset.management_attribute != "LS":
        raise ValidationError("该操作仅适用于 LS 租入或受托管理资产。")
    if asset.record_status != "active" or AssetCustodyReturn.objects.filter(asset=asset, reversal__isnull=True).exists():
        raise ValidationError("资产已归档或已经登记归还。")
    if asset.components.filter(record_status="active").exclude(asset_status__in=TERMINAL_ASSET_STATUSES).exists():
        raise ValidationError("请先分别处理仍在管理的独立组件，再确认主资产归还。")
    prior_status = asset.asset_status
    movement = None
    plans = []
    if prior_status == "other_disposed":
        if not AssetDisposal.objects.filter(asset=asset, status="confirmed").exists():
            raise ValidationError("缺少已完成的处置依据，不能补登记归还。")
    else:
        if prior_status not in {"pending_label", "in_use", "idle", "under_repair"}:
            raise ValidationError("当前资产状态不能办理归还。")
        accounted = AssetFinance.objects.filter(asset=asset, finance_confirmed_at__isnull=False, accounting_treatment="fixed_asset").exists()
        has_history = DepreciationEntry.objects.filter(asset=asset).exists() or AssetValueAdjustment.objects.filter(asset=asset).exists()
        has_profile = AssetDepreciationProfile.objects.filter(asset=asset, status__in=("active", "suspended")).exists()
        if accounted or has_history or has_profile:
            raise ValidationError("该资产涉及已确认财务，请由财务完成既有处置流程后，再补登记归还依据。")
        tz = ZoneInfo(company.timezone or "Asia/Shanghai")
        latest = effective_movements(AssetMovement.objects.filter(asset=asset)).order_by("-effective_at", "-created_at", "-pk").first()
        if latest and returned_on < latest.effective_at.astimezone(tz).date():
            raise ValidationError({"returned_on": "归还日期不能早于资产最近一次有效业务变动。"})
        if asset.maintenance_records.filter(status="confirmed", completed_date__gt=returned_on).exists():
            raise ValidationError({"returned_on": "该日期之后还有有效保养记录，请先核对归还日期。"})
        effective_at = datetime.combine(returned_on, time.min, tzinfo=tz)
        if latest:
            effective_at = max(effective_at, latest.effective_at)
        from apps.maintenance.models import MaintenancePlan
        plans = list(MaintenancePlan.objects.select_for_update().filter(asset=asset, status__in=("active", "suspended")).order_by("pk"))
        movement = _movement(actor=actor, asset=asset, kind="custody_return", effective_at=effective_at,
            target_status="other_disposed", reason=reason, key=key)
    result = insert_trace_record(AssetCustodyReturn(company=company, asset=asset, issued_code=asset.current_issued_code,
        movement=movement, returned_on=returned_on, composition_revision=asset.composition_revisions.first(),
        maintenance_plan_states=[{"id": str(plan.pk), "status": plan.status} for plan in plans],
        reason=reason, idempotency_key=key, request_hash=fingerprint, recorded_by=actor, **values),
        capability="eam_lite.controlled_asset_custody_return")
    from apps.maintenance.services import _base_update
    for plan in plans:
        _base_update(type(plan), plan.pk, {"status": "ended", "ended_reason": "other", "ended_at": result.recorded_at},
                     "controlled_maintenance_plan_mutation")
        _audit(actor=actor, action="maintenance.plan_ended_by_custody_return", instance=plan,
               old_data={"status": plan.status}, new_data={"status": "ended", "custody_return": str(result.pk)}, request=request)
    if movement is not None:
        _controlled_update(Asset, pk=asset.pk, values={"asset_status": "other_disposed", "updated_by_id": actor.pk, "updated_at": timezone.now()})
    _audit(actor=actor, action="asset_custody_returned", instance=result,
           old_data={"asset_status": prior_status}, new_data={**payload, "asset_status": "other_disposed",
               "original_code": asset.asset_code, "financial_values_changed": False}, request=request)
    return result


@transaction.atomic
def reverse_custody_return(*, actor, custody_return, reason, idempotency_key, request=None):
    from apps.assets.models import Asset, AssetCustodyReturn, AssetCustodyReturnReversal, AssetMovement
    from apps.maintenance.models import MaintenancePlan
    from apps.maintenance.services import _base_update, _validate_plan_inputs
    key, reason = validate_trace_request(idempotency_key, reason)
    stored = AssetCustodyReturn.objects.only("asset_id").get(pk=custody_return.pk)
    company, assets = lock_trace_assets(actor, [stored.asset_id])
    asset = assets[stored.asset_id]
    receipt = AssetCustodyReturn.objects.select_for_update(of=("self",)).select_related("movement").get(pk=custody_return.pk, company=company)
    payload = {"custody_return": str(receipt.pk), "reason": reason}
    fingerprint = trace_request_fingerprint(payload)
    existing = AssetCustodyReturnReversal.objects.filter(company=company, idempotency_key=key).first()
    if existing:
        if existing.request_hash != fingerprint:
            raise ValidationError("相同请求已用于不同的归还撤销。")
        return existing
    if AssetCustodyReturnReversal.objects.filter(custody_return=receipt).exists():
        raise ValidationError("该归还登记已经撤销。")
    if asset.record_status != "active":
        raise ValidationError("请先恢复资产可见性，再办理撤销。")
    movement = None
    plans = []
    if receipt.movement_id:
        if asset.component_of_id and (asset.component_of.record_status != "active"
                or asset.component_of.asset_status in TERMINAL_ASSET_STATUSES):
            raise ValidationError("请先处理主资产的归还或处置状态，再恢复该组件。")
        latest = effective_movements(AssetMovement.objects.filter(asset=asset)).order_by("-effective_at", "-created_at", "-pk").first()
        if asset.asset_status != "other_disposed" or latest is None or latest.pk != receipt.movement_id:
            raise ValidationError("归还后已有其他业务变动，不能直接撤销。")
        if (asset.department_id, asset.responsible_employee_id, asset.location_id) != (
            receipt.movement.to_department_id, receipt.movement.to_employee_id, receipt.movement.to_location_id):
            raise ValidationError("归还后的责任或位置资料已变化，请先核对。")
        snapshots = {row["id"]: row["status"] for row in receipt.maintenance_plan_states}
        plans = list(MaintenancePlan.objects.select_for_update().filter(asset=asset, pk__in=snapshots).order_by("pk"))
        if len(plans) != len(snapshots) or any(plan.status != "ended" or plan.ended_reason != "other"
                or plan.ended_at != receipt.recorded_at for plan in plans):
            raise ValidationError("关联保养计划已发生变化，不能自动恢复，请先核对。")
        target = receipt.movement.from_status
        for plan in plans:
            asset.asset_status = target
            _validate_plan_inputs(company=company, asset=asset, responsible_employee=plan.responsible_employee)
        asset.asset_status = "other_disposed"
        movement = _movement(actor=actor, asset=asset, kind="custody_return_reversal", effective_at=timezone.now(),
            target_status=target, reason=reason, key=key)
    result = insert_trace_record(AssetCustodyReturnReversal(company=company, custody_return=receipt, movement=movement,
        reason=reason, idempotency_key=key, request_hash=fingerprint, recorded_by=actor),
        capability="eam_lite.controlled_asset_trace_reversal")
    if movement:
        _controlled_update(Asset, pk=asset.pk, values={"asset_status": movement.to_status, "updated_by_id": actor.pk, "updated_at": timezone.now()})
        for plan in plans:
            _base_update(MaintenancePlan, plan.pk, {"status": snapshots[str(plan.pk)], "ended_reason": None, "ended_at": None},
                         "controlled_maintenance_plan_mutation")
    _audit(actor=actor, action="asset_custody_return_reversed", instance=result,
           new_data={**payload, "restored_asset_status": movement.to_status if movement else None,
                     "restored_maintenance_plans": [str(plan.pk) for plan in plans], "financial_values_changed": False}, request=request)
    return result
