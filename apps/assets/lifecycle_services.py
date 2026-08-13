"""Transactional Sprint 7 asset lifecycle and disposal services.

Public mutations are keyword-only and authorize again after acquiring the
authoritative Company -> Asset row locks.  Model imports are delayed so this
module remains importable while Sprint 7 migrations are being assembled.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection, transaction
from django.db.models import Q, Sum
from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.assets.lifecycle_permissions import (
    TERMINAL_STATUSES,
    can_manage_disposal_attachment,
    require_lifecycle_action,
)
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company


SHANGHAI = ZoneInfo("Asia/Shanghai")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
OPERATION_AUDIT_PREFIX = "asset_lifecycle.idempotency"


def _serializable(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if hasattr(value, "pk"):
        return str(value.pk)
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _request_hash(payload) -> str:
    raw = json.dumps(
        _serializable(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _required(value, field_name, message=None) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValidationError({field_name: message or "不能为空。"})
    return result


def _child_idempotency_key(key, purpose) -> str:
    """Fit deterministic child rows inside their 128-character DB keys."""

    return hashlib.sha256(f"{key}:{purpose}".encode("utf-8")).hexdigest()


def _business_date(value=None, field_name="effective_date") -> date:
    if value is None:
        return timezone.localdate(timezone=SHANGHAI)
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            raise ValidationError({field_name: "日期时间必须包含时区。"})
        return value.astimezone(SHANGHAI).date()
    if not isinstance(value, date):
        raise ValidationError({field_name: "必须是有效日期。"})
    return value


def _business_datetime(value=None, field_name="effective_at") -> datetime:
    if value is None:
        return timezone.now()
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=SHANGHAI)
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError({field_name: "必须是包含时区的日期时间。"})
    return value


def _money(value, field_name="amount") -> Decimal:
    if isinstance(value, float):
        raise ValidationError({field_name: "金额不得经过 float。"})
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field_name: "必须是有效十进制金额。"}) from exc
    if not result.is_finite():
        raise ValidationError({field_name: "金额必须为有限十进制数。"})
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def _company_for_asset(asset):
    from apps.assets.models import Asset
    from apps.masterdata.models import Company

    asset_id = getattr(asset, "pk", asset)
    try:
        company_id = Asset.objects.values_list("company_id", flat=True).get(pk=asset_id)
    except (Asset.DoesNotExist, TypeError, ValueError) as exc:
        raise PermissionDenied("目标资产不存在。") from exc
    selected = current_company()
    if selected is None or selected.pk != company_id:
        raise PermissionDenied("目标资产不属于当前公司。")
    return Company.objects.select_for_update().get(pk=company_id)


def _lock_asset(asset):
    from apps.assets.models import Asset

    company = _company_for_asset(asset)
    queryset = Asset.objects.select_for_update()
    if connection.vendor == "postgresql":
        queryset = Asset.objects.select_for_update(of=("self",))
    return queryset.select_related(
        "company", "department", "responsible_employee", "location",
        "category", "current_issued_code",
    ).get(pk=getattr(asset, "pk", asset), company=company)


def _enable_capability(name):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, true)", [name, "on"])


def _base_update(model, pk, values, capability="eam_lite.controlled_asset_mutation"):
    _enable_capability(capability)
    if QuerySet.update(model._base_manager.filter(pk=pk), **values) != 1:
        raise ValidationError("受控更新未命中唯一业务记录。")


def _save_new(instance, capability=None):
    if capability:
        _enable_capability(capability)
    instance.full_clean()
    try:
        instance.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("保存失败：请求与既有业务记录冲突。") from exc
    return instance


def _audit(*, actor, action, instance, old=None, new=None, request=None):
    return write_business_audit_log(
        company=instance.company,
        user=actor,
        action=action,
        object_type=instance._meta.object_name,
        object_id=instance.pk,
        old_data=old or {},
        new_data=new or {},
        **request_audit_context(request),
    )


def _sync_offboarding_clearances(
    *, actor, asset, movement=None, disposal=None, request=None,
):
    """Keep Sprint 10 clearance state inside this lifecycle transaction."""

    from apps.offboarding.services import sync_clearance_items_for_asset

    return sync_clearance_items_for_asset(
        actor=actor,
        asset=asset,
        movement=movement,
        disposal=disposal,
        request=request,
    )


def _operation_marker(*, company, operation, key):
    """Return an existing action audit used as the Sprint 7 request ledger."""

    from apps.audit.models import AuditLog

    return AuditLog.objects.filter(
        company=company,
        action=f"{OPERATION_AUDIT_PREFIX}.{operation}",
        new_data_json__idempotency_key=key,
    ).order_by("created_at").first()


def _check_operation_idempotency(*, company, operation, key, payload, model):
    key = _required(key, "idempotency_key", "必须提供幂等键。")
    digest = _request_hash(payload)
    marker = _operation_marker(company=company, operation=operation, key=key)
    if marker is None:
        return key, digest, None
    if marker.new_data_json.get("request_hash") != digest:
        raise ValidationError("相同幂等键已用于不同请求参数。")
    result_id = marker.new_data_json.get("result_id")
    result = model._base_manager.filter(pk=result_id, company=company).first()
    if result is None:
        raise ValidationError("幂等结果记录不完整，请停止操作并复核。")
    return key, digest, result


def _write_operation_marker(
    *, actor, operation, result, key, digest, payload, request=None
):
    return write_business_audit_log(
        company=result.company,
        user=actor,
        action=f"{OPERATION_AUDIT_PREFIX}.{operation}",
        object_type=result._meta.object_name,
        object_id=result.pk,
        old_data={},
        new_data={
            "idempotency_key": key,
            "request_hash": digest,
            "result_id": str(result.pk),
            "payload": payload,
        },
        **request_audit_context(request),
    )


def _lock_employee_targets(asset, **targets):
    """Reload target employees after Company -> Asset locks, in PK order.

    HTTP forms and callers may retain an Employee instance from before an
    offboarding transaction committed.  Business mutations must therefore not
    trust status fields on the supplied instance.
    """

    from apps.masterdata.models import Employee

    target_ids = {
        name: getattr(value, "pk", value)
        for name, value in targets.items()
        if value is not None
    }
    invalid = [name for name, value in target_ids.items() if not value]
    if invalid:
        raise ValidationError({name: "员工记录无效。" for name in invalid})
    ids = sorted(set(target_ids.values()), key=str)
    queryset = Employee.objects.select_for_update()
    if connection.vendor == "postgresql":
        queryset = Employee.objects.select_for_update(of=("self",))
    current = {
        employee.pk: employee
        for employee in queryset.filter(
            company_id=asset.company_id, pk__in=ids
        ).order_by("pk")
    }
    missing = {
        name: "员工必须存在且属于当前公司。"
        for name, employee_id in target_ids.items()
        if employee_id not in current
    }
    if missing:
        raise ValidationError(missing)
    return {
        name: None if value is None else current[target_ids[name]]
        for name, value in targets.items()
    }


def _validate_target(
    asset, *, department, employee, location, employee_is_locked=False,
):
    if employee is not None and not employee_is_locked:
        employee = _lock_employee_targets(
            asset, to_responsible_employee=employee
        )["to_responsible_employee"]
    errors = {}
    for name, value in (
        ("to_department", department),
        ("to_responsible_employee", employee),
        ("to_location", location),
    ):
        if value is None:
            errors[name] = "必须填写。"
        elif value.company_id != asset.company_id:
            errors[name] = "必须属于当前公司。"
        elif not value.is_active:
            errors[name] = "已停用，不能用于新业务。"
    if employee is not None:
        if employee.employment_status != "active" or not employee.is_active:
            errors["to_responsible_employee"] = "责任人必须是在职且启用的员工。"
        elif department is not None and employee.department_id != department.pk:
            errors["to_responsible_employee"] = "责任人必须属于目标部门。"
    if location is not None and location.children.exists():
        errors["to_location"] = "必须选择叶级具体位置。"
    if errors:
        raise ValidationError(errors)
    return employee


def _ensure_fresh(
    asset, *, expected_status=None, expected_department_id=None,
    expected_responsible_employee_id=None, expected_location_id=None,
):
    mismatches = []
    checks = (
        ("状态", expected_status, asset.asset_status),
        ("部门", expected_department_id, asset.department_id),
        ("责任人", expected_responsible_employee_id, asset.responsible_employee_id),
        ("位置", expected_location_id, asset.location_id),
    )
    for label, expected, actual in checks:
        if expected is not None and str(expected) != str(actual):
            mismatches.append(label)
    if mismatches:
        raise ValidationError(
            "资产页面已过期，以下当前值已变化：" + "、".join(mismatches) + "；请刷新后重试。"
        )


def _create_movement(
    *, actor, asset, movement_type, effective_at, reason, idempotency_key,
    from_department, to_department, from_employee, to_employee,
    from_location, to_location, from_status, to_status, remark="",
):
    from apps.assets.models import AssetMovement

    effective_at = _business_datetime(effective_at)
    if effective_at > timezone.now():
        raise ValidationError({"effective_at": "生效时间不得晚于当前时间。"})
    movement = AssetMovement(
        company=asset.company, asset=asset, movement_type=movement_type,
        effective_at=effective_at, from_department=from_department,
        to_department=to_department, from_employee=from_employee,
        to_employee=to_employee, from_location=from_location,
        to_location=to_location, from_status=from_status, to_status=to_status,
        reason=_required(reason, "reason", "生命周期动作必须填写原因。"),
        remark=str(remark or "").strip(), idempotency_key=idempotency_key,
        operated_by=actor,
    )
    return _save_new(movement, "eam_lite.controlled_asset_movement_insert")


@transaction.atomic
def change_asset_assignment(
    *, actor, asset, to_department, to_responsible_employee, to_location,
    effective_at, reason, idempotency_key, expected_department_id=None,
    expected_responsible_employee_id=None, expected_location_id=None,
    expected_status=None, to_status=None, remark="", request=None,
    movement_type="transfer",
):
    """Atomically change one asset's department/responsible/location tuple."""

    from apps.assets.models import Asset, AssetMovement

    if movement_type not in {"assignment", "assignment_return", "transfer"}:
        raise ValidationError({"movement_type": "不支持的责任归属变动类型。"})
    asset = _lock_asset(asset)
    require_lifecycle_action(
        actor, asset, movement_type, target_department=to_department
    )
    to_responsible_employee = _lock_employee_targets(
        asset, to_responsible_employee=to_responsible_employee
    )["to_responsible_employee"]
    target_status = asset.asset_status if to_status is None else to_status
    if target_status not in {"in_use", "idle"}:
        raise ValidationError(
            {"to_status": "责任归还或转交后的状态只能是在用或闲置。"}
        )
    if movement_type != "assignment_return" and target_status != asset.asset_status:
        raise ValidationError(
            {"to_status": "只有责任归还动作可以同时明确变更在用/闲置状态。"}
        )
    payload = {
        "asset_id": asset.pk, "movement_type": movement_type,
        "to_department_id": to_department.pk,
        "to_responsible_employee_id": to_responsible_employee.pk,
        "to_location_id": to_location.pk, "effective_at": effective_at,
        "reason": str(reason or "").strip(), "remark": str(remark or "").strip(),
    }
    if to_status is not None:
        payload["to_status"] = target_status
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation="assignment", key=idempotency_key,
        payload=payload, model=AssetMovement,
    )
    if existing is not None:
        return existing
    if asset.record_status != "active" or asset.asset_status not in {"in_use", "idle"}:
        raise ValidationError("只有有效的在用或闲置资产可变更责任归属。")
    _ensure_fresh(
        asset, expected_status=expected_status,
        expected_department_id=expected_department_id,
        expected_responsible_employee_id=expected_responsible_employee_id,
        expected_location_id=expected_location_id,
    )
    _validate_target(
        asset, department=to_department, employee=to_responsible_employee,
        location=to_location, employee_is_locked=True,
    )
    old = {
        "department_id": asset.department_id,
        "responsible_employee_id": asset.responsible_employee_id,
        "location_id": asset.location_id,
        "asset_status": asset.asset_status,
    }
    if (
        asset.department_id == to_department.pk
        and asset.responsible_employee_id == to_responsible_employee.pk
        and asset.location_id == to_location.pk
    ):
        raise ValidationError("部门、责任人和位置均未发生变化。")
    movement = _create_movement(
        actor=actor, asset=asset, movement_type=movement_type,
        effective_at=effective_at, reason=reason, idempotency_key=key,
        from_department=asset.department, to_department=to_department,
        from_employee=asset.responsible_employee, to_employee=to_responsible_employee,
        from_location=asset.location, to_location=to_location,
        from_status=asset.asset_status, to_status=target_status, remark=remark,
    )
    _base_update(Asset, asset.pk, {
        "department_id": to_department.pk,
        "responsible_employee_id": to_responsible_employee.pk,
        "location_id": to_location.pk,
        "asset_status": target_status,
        "updated_by_id": actor.pk,
    })
    _audit(
        actor=actor, action="asset_lifecycle.assignment_changed", instance=asset,
        old=old, new={**payload, "movement_id": str(movement.pk)}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="assignment", result=movement, key=key,
        digest=digest, payload=payload, request=request,
    )
    _sync_offboarding_clearances(
        actor=actor, asset=asset, movement=movement, request=request
    )
    return movement


def assign_asset(**kwargs):
    """Public assignment action using the shared tuple-mutation transaction."""

    return change_asset_assignment(movement_type="assignment", **kwargs)


def return_asset_assignment(**kwargs):
    """Public assignment-return action; warehouse scope is checked by policy."""

    return change_asset_assignment(movement_type="assignment_return", **kwargs)


def transfer_asset(**kwargs):
    """Public department/responsible/location transfer action."""

    return change_asset_assignment(movement_type="transfer", **kwargs)


def _change_status(
    *, actor, asset, expected_from, to_status, movement_type, effective_at,
    reason, idempotency_key, remark="", request=None,
):
    from apps.assets.models import Asset, AssetMovement

    asset = _lock_asset(asset)
    require_lifecycle_action(actor, asset, movement_type)
    payload = {
        "asset_id": asset.pk, "from_status": expected_from,
        "to_status": to_status, "movement_type": movement_type,
        "effective_at": effective_at, "reason": str(reason or "").strip(),
        "remark": str(remark or "").strip(),
    }
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation=movement_type, key=idempotency_key,
        payload=payload, model=AssetMovement,
    )
    if existing is not None:
        return existing
    if asset.record_status != "active" or asset.asset_status != expected_from:
        raise ValidationError(
            f"资产当前状态为 {asset.asset_status}，不能执行该状态动作；请刷新后重试。"
        )
    movement = _create_movement(
        actor=actor, asset=asset, movement_type=movement_type,
        effective_at=effective_at, reason=reason, idempotency_key=key,
        from_department=asset.department, to_department=asset.department,
        from_employee=asset.responsible_employee, to_employee=asset.responsible_employee,
        from_location=asset.location, to_location=asset.location,
        from_status=expected_from, to_status=to_status, remark=remark,
    )
    _base_update(Asset, asset.pk, {"asset_status": to_status, "updated_by_id": actor.pk})
    _audit(
        actor=actor, action=f"asset_lifecycle.{movement_type}", instance=asset,
        old={"asset_status": expected_from},
        new={"asset_status": to_status, "movement_id": str(movement.pk)},
        request=request,
    )
    _write_operation_marker(
        actor=actor, operation=movement_type, result=movement, key=key,
        digest=digest, payload=payload, request=request,
    )
    return movement


@transaction.atomic
def set_asset_idle(
    *, actor, asset, effective_at, reason, idempotency_key,
    expected_status="in_use", remark="", request=None,
):
    if expected_status != "in_use":
        raise ValidationError({"expected_status": "只有在用资产可以转为闲置。"})
    return _change_status(
        actor=actor, asset=asset, expected_from="in_use", to_status="idle",
        movement_type="idle", effective_at=effective_at, reason=reason,
        idempotency_key=idempotency_key, remark=remark, request=request,
    )


@transaction.atomic
def activate_asset(
    *, actor, asset, effective_at, reason, idempotency_key,
    expected_status="idle", remark="", request=None,
):
    if expected_status != "idle":
        raise ValidationError({"expected_status": "只有闲置资产可以转为在用。"})
    return _change_status(
        actor=actor, asset=asset, expected_from="idle", to_status="in_use",
        movement_type="activate", effective_at=effective_at, reason=reason,
        idempotency_key=idempotency_key, remark=remark, request=request,
    )


@transaction.atomic
def send_asset_for_repair(
    *, actor, asset, effective_at, reason, idempotency_key,
    expected_status, remark="", request=None,
):
    if expected_status not in {"in_use", "idle"}:
        raise ValidationError({"expected_status": "送修前状态只能是在用或闲置。"})
    return _change_status(
        actor=actor, asset=asset, expected_from=expected_status,
        to_status="under_repair", movement_type="repair_start",
        effective_at=effective_at, reason=reason,
        idempotency_key=idempotency_key, remark=remark, request=request,
    )


@transaction.atomic
def complete_asset_repair(
    *, actor, asset, effective_at, result, idempotency_key, request=None,
):
    from apps.assets.models import AssetMovement

    asset = _lock_asset(asset)
    start = AssetMovement.objects.select_for_update().filter(
        asset=asset, movement_type="repair_start"
    ).order_by("-effective_at", "-created_at").first()
    if start is None or start.from_status not in {"in_use", "idle"}:
        raise ValidationError("找不到可配对的送修历史。")
    return _change_status(
        actor=actor, asset=asset, expected_from="under_repair",
        to_status=start.from_status, movement_type="repair_complete",
        effective_at=effective_at,
        reason=_required(result, "result", "维修完成必须填写结果。"),
        idempotency_key=idempotency_key,
        remark=f"对应送修变动：{start.pk}", request=request,
    )


@transaction.atomic
def loan_asset(
    *, actor, asset, borrower_type, loan_date, expected_return_date,
    handled_by, reason, idempotency_key, expected_status,
    borrower_employee=None, borrower_name="", borrower_organization="",
    remark="", request=None,
):
    from apps.assets.models import Asset, AssetLoan, AssetMovement

    asset = _lock_asset(asset)
    require_lifecycle_action(actor, asset, "loan")
    if expected_status not in {"in_use", "idle"}:
        raise ValidationError({"expected_status": "只有在用或闲置资产可以借出。"})
    loan_date = _business_date(loan_date, "loan_date")
    expected_return_date = _business_date(expected_return_date, "expected_return_date")
    if expected_return_date < loan_date:
        raise ValidationError({"expected_return_date": "预计归还日不得早于借出日。"})
    borrower_name = str(borrower_name or "").strip()
    borrower_organization = str(borrower_organization or "").strip()
    if borrower_type == "internal_employee":
        if borrower_employee is None or borrower_name or borrower_organization:
            raise ValidationError("内部借用必须选择员工，且外部借用字段必须为空。")
        borrower_employee = _lock_employee_targets(
            asset, borrower_employee=borrower_employee
        )["borrower_employee"]
        if (
            borrower_employee.company_id != asset.company_id
            or borrower_employee.employment_status != "active"
            or not borrower_employee.is_active
            or not borrower_employee.department.is_active
        ):
            raise ValidationError({"borrower_employee": "内部借用人必须是同公司在职启用员工。"})
        borrower_snapshot = borrower_employee.name
    elif borrower_type == "external":
        if borrower_employee is not None or not borrower_name:
            raise ValidationError("外部借用必须填写借用人，且不得关联内部员工。")
        borrower_snapshot = ""
    else:
        raise ValidationError({"borrower_type": "借用方类型无效。"})
    if handled_by is None or handled_by.pk != actor.pk:
        raise PermissionDenied("经办账号必须是当前操作用户。")
    payload = {
        "asset_id": asset.pk, "borrower_type": borrower_type,
        "borrower_employee_id": getattr(borrower_employee, "pk", None),
        "borrower_name": borrower_name,
        "borrower_organization": borrower_organization,
        "loan_date": loan_date, "expected_return_date": expected_return_date,
        "reason": str(reason or "").strip(), "expected_status": expected_status,
    }
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation="loan", key=idempotency_key,
        payload=payload, model=AssetLoan,
    )
    if existing is not None:
        return existing
    if asset.record_status != "active" or asset.asset_status != expected_status:
        raise ValidationError("资产状态已变化，不能借出；请刷新后重试。")
    if AssetLoan.objects.select_for_update().filter(asset=asset, status="active").exists():
        raise ValidationError("该资产已有未归还借出记录。")
    movement = _create_movement(
        actor=actor, asset=asset, movement_type="loan",
        effective_at=datetime.combine(loan_date, time.min, tzinfo=SHANGHAI),
        reason=reason, idempotency_key=key,
        from_department=asset.department, to_department=asset.department,
        from_employee=asset.responsible_employee, to_employee=asset.responsible_employee,
        from_location=asset.location, to_location=asset.location,
        from_status=asset.asset_status, to_status="loaned", remark=remark,
    )
    loan = AssetLoan(
        company=asset.company, asset=asset, borrower_type=borrower_type,
        borrower_employee=borrower_employee,
        borrower_name_snapshot=borrower_snapshot, borrower_name=borrower_name,
        borrower_organization=borrower_organization, loan_date=loan_date,
        expected_return_date=expected_return_date, handled_by=handled_by,
        previous_asset_status=expected_status,
        reason=_required(reason, "reason", "借出必须填写原因。"),
        status="active", loan_movement=movement, loan_idempotency_key=key,
        created_by=actor,
    )
    _save_new(loan, "eam_lite.controlled_asset_loan_mutation")
    _base_update(Asset, asset.pk, {"asset_status": "loaned", "updated_by_id": actor.pk})
    _audit(
        actor=actor, action="asset_lifecycle.loaned", instance=loan,
        old={"asset_status": expected_status},
        new={"asset_status": "loaned", "movement_id": str(movement.pk)},
        request=request,
    )
    _write_operation_marker(
        actor=actor, operation="loan", result=loan, key=key, digest=digest,
        payload=payload, request=request,
    )
    return loan


@transaction.atomic
def return_loan(
    *, actor, loan, returned_at, received_by_employee, return_department,
    return_responsible_employee, return_location, return_asset_status,
    idempotency_key, remark="", request=None,
):
    from apps.assets.models import Asset, AssetLoan, AssetMovement

    asset = _lock_asset(loan.asset_id)
    require_lifecycle_action(
        actor, asset, "loan_return", target_department=return_department
    )
    target_employees = _lock_employee_targets(
        asset,
        received_by_employee=received_by_employee,
        return_responsible_employee=return_responsible_employee,
    )
    received_by_employee = target_employees["received_by_employee"]
    return_responsible_employee = target_employees[
        "return_responsible_employee"
    ]
    loan = AssetLoan.objects.select_for_update().get(
        pk=loan.pk, company=asset.company, asset=asset
    )
    returned_at = _business_datetime(returned_at, "returned_at")
    if returned_at > timezone.now():
        raise ValidationError({"returned_at": "实际归还时间不得晚于当前时间。"})
    if return_asset_status not in {"in_use", "idle"}:
        raise ValidationError({"return_asset_status": "归还后状态只能是在用或闲置。"})
    _validate_target(
        asset, department=return_department,
        employee=return_responsible_employee, location=return_location,
        employee_is_locked=True,
    )
    if (
        received_by_employee is None
        or received_by_employee.company_id != asset.company_id
        or received_by_employee.employment_status != "active"
        or not received_by_employee.is_active
    ):
        raise ValidationError({"received_by_employee": "接收人必须是同公司在职启用员工。"})
    payload = {
        "loan_id": loan.pk, "returned_at": returned_at,
        "received_by_employee_id": received_by_employee.pk,
        "return_department_id": return_department.pk,
        "return_responsible_employee_id": return_responsible_employee.pk,
        "return_location_id": return_location.pk,
        "return_asset_status": return_asset_status, "remark": str(remark or "").strip(),
    }
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation="loan_return", key=idempotency_key,
        payload=payload, model=AssetLoan,
    )
    if existing is not None:
        return existing
    if asset.asset_status != "loaned" or loan.status != "active":
        raise ValidationError("资产不是未归还借出状态，不能重复归还。")
    movement = _create_movement(
        actor=actor, asset=asset, movement_type="loan_return",
        effective_at=returned_at, reason="借出归还", idempotency_key=key,
        from_department=asset.department, to_department=return_department,
        from_employee=asset.responsible_employee, to_employee=return_responsible_employee,
        from_location=asset.location, to_location=return_location,
        from_status="loaned", to_status=return_asset_status, remark=remark,
    )
    _base_update(AssetLoan, loan.pk, {
        "status": "returned", "returned_at": returned_at,
        "received_by_employee_id": received_by_employee.pk,
        "return_department_id": return_department.pk,
        "return_responsible_employee_id": return_responsible_employee.pk,
        "return_location_id": return_location.pk,
        "return_asset_status": return_asset_status,
        "return_remark": str(remark or "").strip(),
        "return_movement_id": movement.pk, "return_idempotency_key": key,
    }, "eam_lite.controlled_asset_loan_mutation")
    _base_update(Asset, asset.pk, {
        "asset_status": return_asset_status,
        "department_id": return_department.pk,
        "responsible_employee_id": return_responsible_employee.pk,
        "location_id": return_location.pk, "updated_by_id": actor.pk,
    })
    _audit(
        actor=actor, action="asset_lifecycle.loan_returned", instance=loan,
        old={"status": "active", "asset_status": "loaned"},
        new={"status": "returned", "asset_status": return_asset_status,
             "movement_id": str(movement.pk)}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="loan_return", result=loan, key=key,
        digest=digest, payload=payload, request=request,
    )
    loan.refresh_from_db()
    _sync_offboarding_clearances(
        actor=actor, asset=asset, movement=movement, request=request
    )
    return loan


@transaction.atomic
def upload_disposal_attachment(
    *, actor, disposal, uploaded_file, security_class="A0", request=None,
):
    """Validate, privately store and link evidence to the Disposal itself."""

    from apps.assets.models import AssetDisposal, AttachmentLink
    from apps.assets.services import (
        MIME_BY_EXTENSION,
        _detect_mime,
        _read_upload,
        _validate_filename,
    )
    from apps.masterdata.models import Attachment
    from apps.masterdata.services import get_system_setting

    asset = _lock_asset(disposal.asset_id)
    disposal = AssetDisposal.objects.select_for_update().get(
        pk=disposal.pk, company=asset.company, asset=asset
    )
    if security_class not in {
        AttachmentLink.SecurityClass.A0,
        AttachmentLink.SecurityClass.A1,
    }:
        raise ValidationError({"security_class": "附件安全分类无效。"})
    if not can_manage_disposal_attachment(
        actor, disposal, security_class=security_class
    ):
        raise PermissionDenied("您没有上传此处置附件的权限。")
    original_name, extension = _validate_filename(uploaded_file.name)
    allowed = set(get_system_setting(
        company=asset.company, key="attachment_allowed_extensions"
    ))
    if extension not in allowed or extension not in MIME_BY_EXTENSION:
        raise ValidationError("当前公司未允许该附件扩展名。")
    limit = get_system_setting(
        company=asset.company, key="attachment_max_size_bytes"
    )
    data = _read_upload(uploaded_file, limit)
    detected_mime = _detect_mime(extension, data)
    client_mime = str(getattr(uploaded_file, "content_type", "") or "").lower()
    if client_mime and client_mime != detected_mime:
        raise ValidationError("客户端 MIME 与文件实际类型不一致。")

    storage_key = (
        f"private/disposals/{asset.company_id}/"
        f"{uuid.uuid4().hex}.{extension}"
    )
    saved_key = default_storage.save(storage_key, ContentFile(data))
    linked = False
    try:
        attachment = Attachment(
            company=asset.company,
            storage_key=saved_key,
            original_filename=original_name[:255],
            safe_filename=(
                get_valid_filename(original_name) or f"attachment.{extension}"
            )[:255],
            file_size=len(data),
            mime_type=detected_mime,
            sha256=hashlib.sha256(data).hexdigest(),
            uploaded_by=actor,
            malware_scan_status=Attachment.MalwareScanStatus.POLICY_LIMITED,
            is_available=False,
        )
        _save_new(attachment)
        link = AttachmentLink(
            company=asset.company,
            attachment=attachment,
            asset=None,
            asset_disposal=disposal,
            role=AttachmentLink.Role.DISPOSAL,
            security_class=security_class,
            created_by=actor,
        )
        _save_new(link)
        _base_update(Attachment, attachment.pk, {"is_available": True})
        _audit(
            actor=actor,
            action="asset_disposal.attachment_uploaded",
            instance=link,
            new={
                "disposal_id": str(disposal.pk),
                "security_class": security_class,
                "file_size": len(data),
                "mime_type": detected_mime,
                "sha256": attachment.sha256,
            },
            request=request,
        )
        linked = True
        return link
    finally:
        if not linked and default_storage.exists(saved_key):
            default_storage.delete(saved_key)


@transaction.atomic
def void_disposal_attachment(*, actor, link, reason, request=None):
    """Void the business link while preserving the stored file and metadata."""

    from apps.assets.models import AssetDisposal, AttachmentLink

    disposal_id = getattr(link, "asset_disposal_id", None)
    if disposal_id is None:
        raise ValidationError("目标不是处置附件。")
    raw = AttachmentLink._base_manager.select_related(
        "asset_disposal"
    ).get(pk=link.pk)
    asset = _lock_asset(raw.asset_disposal.asset_id)
    disposal = AssetDisposal.objects.select_for_update().get(
        pk=disposal_id, company=asset.company, asset=asset
    )
    link = AttachmentLink._base_manager.select_for_update().get(
        pk=raw.pk, company=asset.company, asset_disposal=disposal,
        role=AttachmentLink.Role.DISPOSAL,
    )
    if link.status == AttachmentLink.Status.VOIDED:
        return link
    if not can_manage_disposal_attachment(
        actor, disposal, security_class=link.security_class
    ):
        raise PermissionDenied("您没有作废此处置附件的权限。")
    explanation = _required(reason, "reason", "作废附件必须填写原因。")
    now = timezone.now()
    _base_update(AttachmentLink, link.pk, {
        "status": AttachmentLink.Status.VOIDED,
        "void_reason": explanation,
        "voided_by_id": actor.pk,
        "voided_at": now,
    })
    link.refresh_from_db()
    _audit(
        actor=actor,
        action="asset_disposal.attachment_voided",
        instance=link,
        old={"status": "active"},
        new={"status": "voided", "reason": explanation},
        request=request,
    )
    return link


@transaction.atomic
def initiate_disposal(
    *, actor, asset, disposal_type, application_date, planned_disposal_date,
    reason, idempotency_key, description="", recipient_name="",
    handled_by=None, expected_status=None, request=None,
):
    from apps.assets.models import Asset, AssetDisposal, AssetLoan

    asset = _lock_asset(asset)
    require_lifecycle_action(actor, asset, "disposal_start")
    if disposal_type not in {"scrap", "sale", "other"}:
        raise ValidationError({"disposal_type": "处置类型无效。"})
    application_date = _business_date(application_date, "application_date")
    planned_disposal_date = _business_date(planned_disposal_date, "planned_disposal_date")
    if planned_disposal_date < application_date:
        raise ValidationError({"planned_disposal_date": "拟处置日期不得早于申请日期。"})
    if handled_by is not None and handled_by.pk != actor.pk:
        raise PermissionDenied("发起时经办账号必须是当前操作用户。")
    existing_disposal = AssetDisposal.objects.select_for_update().filter(
        company=asset.company, idempotency_key=str(idempotency_key or "").strip()
    ).first()
    previous_status = (
        existing_disposal.previous_asset_status
        if existing_disposal is not None
        else (expected_status or asset.asset_status)
    )
    payload = {
        "asset_id": asset.pk, "disposal_type": disposal_type,
        "application_date": application_date,
        "planned_disposal_date": planned_disposal_date,
        "reason": str(reason or "").strip(),
        "description": str(description or "").strip(),
        "recipient_name": str(recipient_name or "").strip(),
        "previous_asset_status": previous_status,
    }
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation="disposal_start", key=idempotency_key,
        payload=payload, model=AssetDisposal,
    )
    if existing is not None:
        return existing
    if asset.record_status != "active" or asset.asset_status not in {"in_use", "idle", "under_repair"}:
        raise ValidationError("当前资产状态不能发起处置。")
    _ensure_fresh(asset, expected_status=expected_status)
    if AssetLoan.objects.select_for_update().filter(asset=asset, status="active").exists():
        raise ValidationError("借出资产必须先归还，不能直接发起处置。")
    if AssetDisposal.objects.select_for_update().filter(
        asset=asset, status__in=("draft", "finance_locked", "confirmed")
    ).exists():
        raise ValidationError("该资产已有进行中或已确认处置记录。")
    disposal = AssetDisposal(
        company=asset.company, asset=asset, disposal_type=disposal_type,
        application_date=application_date,
        planned_disposal_date=planned_disposal_date,
        actual_disposal_date=None,
        reason=_required(reason, "reason", "发起处置必须填写原因。"),
        description=str(description or "").strip(),
        recipient_name=str(recipient_name or "").strip(),
        previous_asset_status=asset.asset_status, status="draft",
        initiated_by=actor, handled_by=handled_by or actor,
        idempotency_key=key,
    )
    _save_new(disposal, "eam_lite.controlled_asset_disposal_mutation")
    movement = _create_movement(
        actor=actor, asset=asset, movement_type="disposal_start",
        effective_at=datetime.combine(application_date, time.min, tzinfo=SHANGHAI),
        reason=reason, idempotency_key=_child_idempotency_key(key, "movement"),
        from_department=asset.department, to_department=asset.department,
        from_employee=asset.responsible_employee, to_employee=asset.responsible_employee,
        from_location=asset.location, to_location=asset.location,
        from_status=asset.asset_status, to_status="pending_disposal",
        remark=f"处置记录：{disposal.pk}",
    )
    _base_update(Asset, asset.pk, {"asset_status": "pending_disposal", "updated_by_id": actor.pk})
    _audit(
        actor=actor, action="asset_disposal.initiated", instance=disposal,
        old={"asset_status": asset.asset_status},
        new={"asset_status": "pending_disposal", "movement_id": str(movement.pk)},
        request=request,
    )
    _write_operation_marker(
        actor=actor, operation="disposal_start", result=disposal, key=key,
        digest=digest, payload=payload, request=request,
    )
    _sync_offboarding_clearances(
        actor=actor,
        asset=asset,
        movement=movement,
        disposal=disposal,
        request=request,
    )
    return disposal


@transaction.atomic
def record_disposal_actual_details(
    *, actor, disposal, actual_disposal_date, idempotency_key,
    recipient_name=None, handled_by=None, request=None,
):
    from apps.assets.models import AssetDisposal

    asset = _lock_asset(disposal.asset_id)
    require_lifecycle_action(actor, asset, "disposal_actual_details")
    disposal = AssetDisposal.objects.select_for_update().get(pk=disposal.pk, company=asset.company, asset=asset)
    actual = _business_date(actual_disposal_date, "actual_disposal_date")
    if actual < disposal.application_date or actual > _business_date():
        raise ValidationError({"actual_disposal_date": "实际日期不得早于申请日或晚于当前业务日。"})
    if handled_by is not None and handled_by.pk != actor.pk:
        raise PermissionDenied("实际处置经办账号必须是当前操作用户。")
    payload = {
        "disposal_id": disposal.pk, "actual_disposal_date": actual,
        "recipient_name": str(recipient_name if recipient_name is not None else disposal.recipient_name).strip(),
        "handled_by_id": actor.pk,
    }
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation="disposal_actual_details",
        key=idempotency_key, payload=payload, model=AssetDisposal,
    )
    if existing is not None:
        return existing
    if asset.asset_status != "pending_disposal" or disposal.status != "draft":
        raise ValidationError("只有处置处理中且未锁定财务快照的记录可登记实际信息。")
    old = {"actual_disposal_date": disposal.actual_disposal_date,
           "recipient_name": disposal.recipient_name}
    _base_update(AssetDisposal, disposal.pk, {
        "actual_disposal_date": actual,
        "recipient_name": payload["recipient_name"], "handled_by_id": actor.pk,
    }, "eam_lite.controlled_asset_disposal_mutation")
    disposal.refresh_from_db()
    _audit(actor=actor, action="asset_disposal.actual_details_recorded",
           instance=disposal, old=old, new=payload, request=request)
    _write_operation_marker(actor=actor, operation="disposal_actual_details",
                            result=disposal, key=key, digest=digest,
                            payload=payload, request=request)
    return disposal


def _required_depreciation_cutoff(*, asset, actual_date):
    """Block a snapshot while an eligible depreciation period is unconfirmed."""

    from apps.finance.domain import resolve_stop_date
    from apps.finance.models import (
        AssetDepreciationProfile,
        DepreciationBatchItem,
    )

    profiles = list(AssetDepreciationProfile.objects.select_for_update().filter(
        asset=asset,
        status__in=("active", "suspended"),
        effective_from__lte=actual_date,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=actual_date)))
    cutoff = actual_date
    missing = []
    for profile in profiles:
        stop_date = resolve_stop_date(
            event_date=actual_date, stop_rule=profile.stop_rule
        )
        cutoff = max(cutoff, stop_date)
        # Batch periods are half-open.  A period which starts before the stop
        # boundary must have one confirmed regular item unless it is an
        # approved no-depreciation profile.
        if profile.method == "no_depreciation":
            continue
        confirmed = DepreciationBatchItem.objects.filter(
            asset=asset,
            depreciation_profile=profile,
            batch__batch_type="regular",
            batch__status="confirmed",
            batch__period_start__lt=stop_date,
            batch__period_end__gt=profile.effective_from,
        ).values_list("batch__period_start", "batch__period_end")
        confirmed_periods = set(confirmed)
        if profile.method in {"manual", "units_of_production"}:
            # Input-driven methods deliberately have no persistent Schedule;
            # require a confirmed period covering the actual disposal date.
            if not any(start <= actual_date < end for start, end in confirmed_periods):
                missing.append(str(profile.pk))
            continue
        required = profile.schedules.filter(
            status="planned",
            period_start__lt=stop_date,
            period_end__gt=profile.effective_from,
        ).values_list("period_start", "period_end")
        if any(period not in confirmed_periods for period in required):
            missing.append(str(profile.pk))
    if missing:
        raise ValidationError(
            "实际处置日对应的必需折旧期间尚未全部确认，不能锁定财务快照："
            + "、".join(missing)
        )
    return cutoff


def _balances_at(*, asset, cutoff, depreciation_cutoff=None):
    from apps.finance.models import AssetFinance, AssetValueAdjustment, DepreciationEntry

    finance = AssetFinance.objects.select_for_update().get(
        company=asset.company, asset=asset, finance_confirmed_at__isnull=False
    )
    if finance.original_cost is None:
        raise ValidationError("资产缺少已确认原值，不能锁定处置快照。")
    later_cost = AssetValueAdjustment.objects.filter(
        asset=asset, adjustment_type="cost_correction",
        effective_date__gt=cutoff, status__in=("confirmed", "reversed"),
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    original = _money(finance.original_cost - later_cost, "original_cost")
    depreciation_cutoff = depreciation_cutoff or cutoff
    accumulated = DepreciationEntry.objects.filter(asset=asset).filter(
        Q(source_type="batch", period_end__lte=depreciation_cutoff)
        | Q(source_type="opening", entry_date__lte=cutoff)
        | Q(source_type="adjustment", value_adjustment__effective_date__lte=cutoff)
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    impairment = ZERO
    for adjustment in AssetValueAdjustment.objects.select_for_update().filter(
        asset=asset, effective_date__lte=cutoff,
        status__in=("confirmed", "reversed"),
        adjustment_type__in=("opening_impairment", "impairment", "impairment_reversal"),
    ):
        impairment += (-adjustment.amount if adjustment.adjustment_type == "impairment_reversal" else adjustment.amount)
    accumulated = _money(accumulated, "actual_accumulated_depreciation")
    impairment = _money(impairment, "impairment")
    if finance.accounting_treatment == "controlled_non_fixed":
        if accumulated != ZERO or impairment != ZERO:
            raise ValidationError("受控非固定资产的累计折旧和减值必须为 0。")
    book = _money(original - accumulated - impairment, "book_value")
    if min(original, accumulated, impairment, book) < ZERO:
        raise ValidationError("处置日财务余额不满足非负勾稽关系。")
    return original, accumulated, impairment, book


@transaction.atomic
def lock_disposal_financial_snapshot(
    *, actor, disposal, disposal_income, idempotency_key, request=None,
):
    from apps.assets.models import AssetDisposal

    asset = _lock_asset(disposal.asset_id)
    require_lifecycle_action(actor, asset, "disposal_finance_lock")
    disposal = AssetDisposal.objects.select_for_update().get(pk=disposal.pk, company=asset.company, asset=asset)
    if disposal.actual_disposal_date is None:
        raise ValidationError("必须先登记实际处置日期，拟处置日期不能替代。")
    income = _money(disposal_income, "disposal_income")
    if income < ZERO:
        raise ValidationError({"disposal_income": "处置收入不得为负数。"})
    key = _required(idempotency_key, "idempotency_key", "必须提供幂等键。")
    marker = _operation_marker(
        company=asset.company, operation="disposal_finance_lock", key=key
    )
    if marker is not None:
        saved = marker.new_data_json.get("payload", {})
        if (
            str(saved.get("disposal_id")) != str(disposal.pk)
            or str(saved.get("actual_disposal_date"))
            != disposal.actual_disposal_date.isoformat()
            or _money(saved.get("disposal_income"), "disposal_income") != income
        ):
            raise ValidationError("相同幂等键已用于不同请求参数。")
        result = AssetDisposal._base_manager.filter(
            pk=marker.new_data_json.get("result_id"), company=asset.company
        ).first()
        if result is None:
            raise ValidationError("幂等结果记录不完整，请停止操作并复核。")
        return result
    if asset.asset_status != "pending_disposal" or disposal.status != "draft":
        raise ValidationError("只有处置处理中且未锁定的记录可核对财务快照。")
    depreciation_cutoff = _required_depreciation_cutoff(
        asset=asset, actual_date=disposal.actual_disposal_date
    )
    original, accumulated, impairment, book = _balances_at(
        asset=asset,
        cutoff=disposal.actual_disposal_date,
        depreciation_cutoff=depreciation_cutoff,
    )
    payload = {
        "disposal_id": disposal.pk,
        "actual_disposal_date": disposal.actual_disposal_date,
        "disposal_income": income, "original_cost_snapshot": original,
        "actual_accumulated_depreciation_snapshot": accumulated,
        "impairment_snapshot": impairment, "book_value_snapshot": book,
    }
    digest = _request_hash(payload)
    _base_update(AssetDisposal, disposal.pk, {
        "status": "finance_locked", "disposal_income": income,
        "original_cost_snapshot": original,
        "actual_accumulated_depreciation_snapshot": accumulated,
        "impairment_snapshot": impairment, "book_value_snapshot": book,
        "finance_locked_by_id": actor.pk, "finance_locked_at": timezone.now(),
    }, "eam_lite.controlled_asset_disposal_mutation")
    disposal.refresh_from_db()
    _audit(actor=actor, action="asset_disposal.finance_locked", instance=disposal,
           old={"status": "draft"}, new=payload, request=request)
    _write_operation_marker(actor=actor, operation="disposal_finance_lock",
                            result=disposal, key=key, digest=digest,
                            payload=payload, request=request)
    return disposal


@transaction.atomic
def cancel_disposal(
    *, actor, disposal, reason, idempotency_key, request=None,
):
    from apps.assets.models import Asset, AssetDisposal

    asset = _lock_asset(disposal.asset_id)
    require_lifecycle_action(actor, asset, "disposal_cancel")
    disposal = AssetDisposal.objects.select_for_update().get(pk=disposal.pk, company=asset.company, asset=asset)
    reason = _required(reason, "reason", "取消处置必须填写原因。")
    payload = {"disposal_id": disposal.pk, "reason": reason,
               "restored_status": disposal.previous_asset_status}
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation="disposal_cancel", key=idempotency_key,
        payload=payload, model=AssetDisposal,
    )
    if existing is not None:
        return existing
    if asset.asset_status != "pending_disposal" or disposal.status not in {"draft", "finance_locked"}:
        raise ValidationError("只有未完成的处置可以取消。")
    movement = _create_movement(
        actor=actor, asset=asset, movement_type="disposal_cancel",
        effective_at=timezone.now(), reason=reason,
        idempotency_key=_child_idempotency_key(key, "movement"),
        from_department=asset.department, to_department=asset.department,
        from_employee=asset.responsible_employee, to_employee=asset.responsible_employee,
        from_location=asset.location, to_location=asset.location,
        from_status="pending_disposal", to_status=disposal.previous_asset_status,
        remark=f"处置记录：{disposal.pk}",
    )
    _base_update(AssetDisposal, disposal.pk, {
        "status": "cancelled", "cancelled_by_id": actor.pk,
        "cancelled_at": timezone.now(), "cancellation_reason": reason,
    }, "eam_lite.controlled_asset_disposal_mutation")
    _base_update(Asset, asset.pk, {"asset_status": disposal.previous_asset_status,
                                   "updated_by_id": actor.pk})
    disposal.refresh_from_db()
    _audit(actor=actor, action="asset_disposal.cancelled", instance=disposal,
           old={"asset_status": "pending_disposal"},
           new={**payload, "movement_id": str(movement.pk)}, request=request)
    _write_operation_marker(actor=actor, operation="disposal_cancel", result=disposal,
                            key=key, digest=digest, payload=payload, request=request)
    _sync_offboarding_clearances(
        actor=actor,
        asset=asset,
        movement=movement,
        disposal=disposal,
        request=request,
    )
    return disposal


def _terminal_status(disposal_type):
    return {"scrap": "disposed", "sale": "sold", "other": "other_disposed"}[disposal_type]


@transaction.atomic
def complete_disposal(
    *, actor, disposal, idempotency_key, request=None,
):
    from apps.assets.models import Asset, AssetDisposal, AttachmentLink
    from apps.finance.domain import resolve_stop_date
    from apps.finance.models import AssetDepreciationProfile, DepreciationProfileEvent
    from apps.maintenance.models import MaintenancePlan

    asset = _lock_asset(disposal.asset_id)
    require_lifecycle_action(actor, asset, "disposal_complete")
    disposal = AssetDisposal.objects.select_for_update().get(pk=disposal.pk, company=asset.company, asset=asset)
    target = _terminal_status(disposal.disposal_type)
    payload = {"disposal_id": disposal.pk, "target_status": target,
               "actual_disposal_date": disposal.actual_disposal_date}
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation="disposal_complete", key=idempotency_key,
        payload=payload, model=AssetDisposal,
    )
    if existing is not None:
        return existing
    if asset.asset_status != "pending_disposal" or disposal.status != "finance_locked":
        raise ValidationError("处置财务快照尚未锁定，不能完成。")
    if disposal.actual_disposal_date is None or disposal.handled_by_id is None:
        raise ValidationError("实际日期或经办人不完整，不能完成处置。")
    active_evidence = AttachmentLink.objects.filter(
        asset_disposal=disposal,
        status="active",
        attachment__is_available=True,
        attachment__malware_scan_status__in=("policy_limited", "clean"),
    )
    if disposal.disposal_type == "scrap":
        active_evidence = active_evidence.filter(attachment__mime_type__startswith="image/")
    if not active_evidence.exists():
        raise ValidationError("完成处置前必须上传有效处置证据。")
    if disposal.disposal_type in {"sale", "other"} and not disposal.recipient_name.strip():
        raise ValidationError("出售或其他处置必须填写接收方/去向。")
    profiles = list(AssetDepreciationProfile.objects.select_for_update().filter(
        asset=asset, status__in=("active", "suspended")
    ))
    maintenance_plans = list(
        MaintenancePlan._base_manager.select_for_update().filter(
            asset=asset, status__in=("active", "suspended")
        )
    )
    for profile in profiles:
        stop_date = resolve_stop_date(
            event_date=disposal.actual_disposal_date,
            stop_rule=profile.stop_rule,
        )
        event = DepreciationProfileEvent(
            company=asset.company, asset=asset, depreciation_profile=profile,
            event_type="disposal_stop", effective_date=stop_date,
            reason="资产处置完成自动停止折旧", source_disposal=disposal,
            previous_profile_status=profile.status, created_by=actor,
        )
        _save_new(event)
        _base_update(AssetDepreciationProfile, profile.pk, {"status": "stopped"},
                     "eam_lite.controlled_finance_profile_status")
    movement = _create_movement(
        actor=actor, asset=asset, movement_type="disposal_complete",
        effective_at=datetime.combine(disposal.actual_disposal_date, time.min, tzinfo=SHANGHAI),
        reason=disposal.reason,
        idempotency_key=_child_idempotency_key(key, "movement"),
        from_department=asset.department, to_department=asset.department,
        from_employee=asset.responsible_employee, to_employee=asset.responsible_employee,
        from_location=asset.location, to_location=asset.location,
        from_status="pending_disposal", to_status=target,
        remark=f"处置记录：{disposal.pk}",
    )
    now = timezone.now()
    _base_update(AssetDisposal, disposal.pk, {
        "status": "confirmed", "confirmed_by_id": actor.pk,
        "confirmed_at": now,
    }, "eam_lite.controlled_asset_disposal_mutation")
    for plan in maintenance_plans:
        previous_status = plan.status
        _base_update(
            MaintenancePlan,
            plan.pk,
            {
                "status": "ended",
                "ended_reason": "asset_disposal",
                "ended_by_disposal_id": disposal.pk,
                "status_before_disposal": plan.status,
                "ended_at": now,
            },
            "eam_lite.controlled_maintenance_plan_mutation",
        )
        plan.refresh_from_db()
        _audit(
            actor=actor,
            action="maintenance.plan_ended_by_disposal",
            instance=plan,
            old={"status": previous_status},
            new={
                "status": "ended",
                "ended_reason": "asset_disposal",
                "ended_by_disposal_id": str(disposal.pk),
            },
            request=request,
        )
    _base_update(Asset, asset.pk, {"asset_status": target, "updated_by_id": actor.pk})
    disposal.refresh_from_db()
    _audit(actor=actor, action="asset_disposal.completed", instance=disposal,
           old={"asset_status": "pending_disposal"},
           new={**payload, "movement_id": str(movement.pk),
                "stopped_profiles": [str(item.pk) for item in profiles],
                "ended_maintenance_plans": [str(item.pk) for item in maintenance_plans]}, request=request)
    _write_operation_marker(actor=actor, operation="disposal_complete", result=disposal,
                            key=key, digest=digest, payload=payload, request=request)
    _sync_offboarding_clearances(
        actor=actor,
        asset=asset,
        movement=movement,
        disposal=disposal,
        request=request,
    )
    return disposal


@transaction.atomic
def reverse_disposal(
    *, actor, disposal, reason, idempotency_key,
    replacement_responsible_employee=None, request=None,
):
    """Reverse a confirmed terminal disposal and only its own stop events."""

    from apps.assets.models import (
        Asset,
        AssetDisposal,
        AssetDisposalReversal,
        AssetMovement,
    )
    from apps.finance.models import (
        AssetDepreciationProfile,
        DepreciationEntry,
        DepreciationProfileEvent,
    )
    from apps.maintenance.models import MaintenancePlan
    from apps.masterdata.models import Employee

    asset = _lock_asset(disposal.asset_id)
    require_lifecycle_action(actor, asset, "disposal_reversal")
    if replacement_responsible_employee is not None:
        replacement_responsible_employee = _lock_employee_targets(
            asset,
            replacement_responsible_employee=replacement_responsible_employee,
        )["replacement_responsible_employee"]
    disposal = AssetDisposal.objects.select_for_update().get(
        pk=disposal.pk, company=asset.company, asset=asset
    )
    from apps.offboarding.services import require_disposal_not_used_by_clearance

    require_disposal_not_used_by_clearance(disposal)
    explanation = _required(reason, "reason", "处置冲销必须填写原因。")
    replacement_id = getattr(replacement_responsible_employee, "pk", None)
    payload = {
        "disposal_id": disposal.pk,
        "reason": explanation,
        "restored_asset_status": disposal.previous_asset_status,
        "replacement_responsible_employee_id": replacement_id,
    }
    key, digest, existing = _check_operation_idempotency(
        company=asset.company,
        operation="disposal_reversal",
        key=idempotency_key,
        payload=payload,
        model=AssetDisposalReversal,
    )
    if existing is not None:
        return existing
    if asset.record_status != "active":
        raise ValidationError("已归档处置必须先恢复显示，才能执行终态冲销。")
    if disposal.status != "confirmed" or asset.asset_status != _terminal_status(
        disposal.disposal_type
    ):
        raise ValidationError("只有与资产终态一致的已完成处置可以冲销。")
    if AssetDisposalReversal.objects.select_for_update().filter(
        asset_disposal=disposal
    ).exists():
        raise ValidationError("该处置已经冲销。")

    stops = list(DepreciationProfileEvent.objects.select_for_update().filter(
        source_disposal=disposal, event_type="disposal_stop"
    ).select_related("depreciation_profile"))
    maintenance_plans = list(
        MaintenancePlan._base_manager.select_for_update().filter(
            ended_by_disposal=disposal,
        )
    )
    if disposal.confirmed_at is None:
        raise ValidationError("处置完成时间缺失，不能安全判断后续业务。")
    later_movements = AssetMovement.objects.select_for_update().filter(
        asset=asset, created_at__gt=disposal.confirmed_at
    )
    if later_movements.exists():
        raise ValidationError("处置完成后已有新的资产变动，不能直接冲销。")
    for stop in stops:
        profile = stop.depreciation_profile
        if AssetDepreciationProfile.objects.select_for_update().filter(
            asset=asset, version__gt=profile.version
        ).exists():
            raise ValidationError("处置停止后已有新折旧 Profile，不能直接冲销。")
        if DepreciationProfileEvent.objects.select_for_update().filter(
            depreciation_profile=profile,
            created_at__gt=stop.created_at,
        ).exclude(event_type="disposal_restore").exists():
            raise ValidationError("处置停止后已有人工折旧事件，不能直接冲销。")
        if DepreciationEntry.objects.select_for_update().filter(
            asset=asset, created_at__gt=stop.created_at
        ).exists():
            raise ValidationError("处置停止后已有确认折旧分录，不能直接冲销。")
        if profile.status != "stopped":
            raise ValidationError("处置停止后的折旧 Profile 状态已变化，不能猜测恢复。")
    for plan in maintenance_plans:
        if (
            plan.status != "ended"
            or plan.ended_reason != "asset_disposal"
            or plan.status_before_disposal not in {"active", "suspended"}
        ):
            raise ValidationError("处置自动终止的保养计划来源状态不完整，不能猜测恢复。")
        plan_responsible = Employee.objects.select_for_update().select_related(
            "department"
        ).get(pk=plan.responsible_employee_id)
        if (
            plan_responsible.company_id != plan.company_id
            or plan_responsible.employment_status != "active"
            or not plan_responsible.is_active
            or not plan_responsible.department.is_active
        ):
            raise ValidationError("保养计划责任人已失效，不能猜测恢复处置前计划状态。")

    responsible = asset.responsible_employee
    responsible_valid = bool(
        responsible is not None
        and responsible.company_id == asset.company_id
        and responsible.employment_status == "active"
        and responsible.is_active
        and responsible.department_id == asset.department_id
    )
    if not responsible_valid:
        replacement = replacement_responsible_employee
        if (
            replacement is None
            or replacement.company_id != asset.company_id
            or replacement.employment_status != "active"
            or not replacement.is_active
            or replacement.department_id != asset.department_id
        ):
            raise ValidationError({
                "replacement_responsible_employee": (
                    "原责任人已不合法，必须选择同公司、同部门的在职启用替代责任人。"
                )
            })
        responsible = replacement
    elif replacement_responsible_employee is not None:
        if (
            replacement_responsible_employee.company_id != asset.company_id
            or replacement_responsible_employee.employment_status != "active"
            or not replacement_responsible_employee.is_active
            or replacement_responsible_employee.department_id != asset.department_id
        ):
            raise ValidationError({
                "replacement_responsible_employee": "替代责任人不满足同公司同部门在职要求。"
            })
        responsible = replacement_responsible_employee

    reversal = AssetDisposalReversal(
        company=asset.company,
        asset_disposal=disposal,
        reason=explanation,
        restored_asset_status=disposal.previous_asset_status,
        idempotency_key=key,
        reversed_by=actor,
        reversed_at=timezone.now(),
    )
    _save_new(reversal, "eam_lite.controlled_asset_disposal_reversal_insert")
    restored_events = []
    for stop in stops:
        restore = DepreciationProfileEvent(
            company=asset.company,
            asset=asset,
            depreciation_profile=stop.depreciation_profile,
            event_type="disposal_restore",
            effective_date=stop.effective_date,
            reason=f"处置冲销恢复：{explanation}",
            source_disposal=disposal,
            previous_profile_status="",
            reverses_event=stop,
            created_by=actor,
        )
        _save_new(restore)
        restored_events.append(restore)
        _base_update(
            AssetDepreciationProfile,
            stop.depreciation_profile_id,
            {"status": stop.previous_profile_status},
            "eam_lite.controlled_finance_profile_status",
        )
    movement = _create_movement(
        actor=actor,
        asset=asset,
        movement_type="disposal_reversal",
        effective_at=timezone.now(),
        reason=explanation,
        idempotency_key=_child_idempotency_key(key, "movement"),
        from_department=asset.department,
        to_department=asset.department,
        from_employee=asset.responsible_employee,
        to_employee=responsible,
        from_location=asset.location,
        to_location=asset.location,
        from_status=asset.asset_status,
        to_status=disposal.previous_asset_status,
        remark=f"冲销处置：{disposal.pk}",
    )
    _base_update(
        AssetDisposal,
        disposal.pk,
        {"status": "reversed"},
        "eam_lite.controlled_asset_disposal_mutation",
    )
    restored_maintenance_plan_ids = []
    for plan in maintenance_plans:
        restored_status = plan.status_before_disposal
        latest = plan.records.filter(status="confirmed").order_by(
            "-completed_date", "-created_at", "-pk"
        ).first()
        last_date = latest.completed_date if latest else None
        if latest:
            from apps.maintenance.domain import add_calendar_cycle

            next_date = add_calendar_cycle(
                latest.completed_date, plan.cycle_value, plan.cycle_unit
            )
        else:
            next_date = plan.first_due_date
        _base_update(
            MaintenancePlan,
            plan.pk,
            {
                "status": restored_status,
                "ended_reason": None,
                "ended_by_disposal_id": None,
                "status_before_disposal": None,
                "ended_at": None,
                "last_maintenance_date": last_date,
                "next_maintenance_date": next_date,
            },
            "eam_lite.controlled_maintenance_plan_mutation",
        )
        plan.refresh_from_db()
        _audit(
            actor=actor,
            action="maintenance.plan_restored_by_disposal",
            instance=plan,
            old={
                "status": "ended",
                "ended_reason": "asset_disposal",
                "ended_by_disposal_id": str(disposal.pk),
            },
            new={
                "status": restored_status,
                "last_maintenance_date": last_date.isoformat() if last_date else None,
                "next_maintenance_date": next_date.isoformat(),
            },
            request=request,
        )
        restored_maintenance_plan_ids.append(str(plan.pk))
    _base_update(Asset, asset.pk, {
        "asset_status": disposal.previous_asset_status,
        "responsible_employee_id": responsible.pk,
        "updated_by_id": actor.pk,
    })
    _audit(
        actor=actor,
        action="asset_disposal.reversed",
        instance=reversal,
        old={"disposal_status": "confirmed", "asset_status": asset.asset_status},
        new={
            **payload,
            "movement_id": str(movement.pk),
            "restored_event_ids": [str(event.pk) for event in restored_events],
            "restored_maintenance_plan_ids": restored_maintenance_plan_ids,
        },
        request=request,
    )
    _write_operation_marker(
        actor=actor,
        operation="disposal_reversal",
        result=reversal,
        key=key,
        digest=digest,
        payload=payload,
        request=request,
    )
    return reversal


@transaction.atomic
def correct_asset_code(
    *, actor, asset, effective_date, idempotency_key, reason,
    coding_scheme=None, request=None,
):
    """Issue a new official code and rotate QR identity in one transaction."""

    from apps.assets.models import (
        Asset,
        AssetCodeHistory,
        AssetLabelPrintItem,
        AssetQrIdentity,
    )
    from apps.assets.qr_services import generate_public_token
    from apps.coding.domain import (
        build_scope_key,
        is_effective,
        normalize_code,
        render_code,
        validate_scheme_structure,
    )
    from apps.finance.services import _insert_counter_if_missing
    from apps.masterdata.models import AssetCodingScheme, IssuedCode

    asset = _lock_asset(asset)
    require_lifecycle_action(actor, asset, "code_correction")
    business_date = _business_date(effective_date, "effective_date")
    if business_date > _business_date():
        raise ValidationError({"effective_date": "编号更正生效日不得晚于当前业务日。"})
    explanation = _required(reason, "reason", "编号更正必须填写原因。")
    key = _required(idempotency_key, "idempotency_key", "编号更正必须提供幂等键。")
    old = IssuedCode.objects.select_for_update().filter(
        pk=asset.current_issued_code_id,
        company=asset.company,
    ).first()
    if old is None:
        raise ValidationError("正式资产缺少当前有效编号登记，不能执行更正。")
    requested_scheme_id = getattr(coding_scheme, "pk", coding_scheme) or old.coding_scheme_id
    payload = {
        "asset_id": asset.pk,
        "old_issued_code_id": old.pk,
        "effective_date": business_date,
        "reason": explanation,
        "coding_scheme_id": requested_scheme_id,
    }
    existing = IssuedCode.objects.select_for_update().filter(
        company=asset.company, idempotency_key=key
    ).first()
    if existing is not None:
        history = AssetCodeHistory.objects.select_related("old_issued_code").filter(
            asset=asset,
            event_type="corrected",
            new_issued_code=existing,
            reason=explanation,
            effective_at=datetime.combine(business_date, time.min, tzinfo=SHANGHAI),
        ).first()
        # After a successful correction the current issued code is the replay
        # result, so the original request identity must be reconstructed from
        # immutable history rather than compared with the new current row.
        if (
            history is None
            or existing.coding_scheme_id != requested_scheme_id
            or asset.current_issued_code_id != existing.pk
            or history.old_issued_code_id == existing.pk
        ):
            raise ValidationError("相同幂等键已用于不同的编号更正请求。")
        return existing
    if asset.record_status != "active" or asset.asset_status in TERMINAL_STATUSES:
        raise ValidationError("只有未归档的非终态正式资产可以更正编号。")
    if asset.asset_status in {"draft", "pending_finance"}:
        raise ValidationError("草稿或待财务确认资产不属于正式编号更正范围。")
    if old.status != "active" or old.pk != asset.current_issued_code_id:
        raise ValidationError("当前编号登记已变化，请刷新后重试。")
    scheme = AssetCodingScheme.objects.select_for_update().prefetch_related(
        "segments"
    ).get(pk=requested_scheme_id)
    if scheme.company_id != asset.company_id or not is_effective(scheme, business_date):
        raise ValidationError({"coding_scheme": "编码方案在更正生效日不可用。"})
    validate_scheme_structure(scheme)
    category_scoped = scheme.reset_mode in {
        "category_yearly", "category_monthly"
    }
    scope_key = build_scope_key(
        asset.company_id,
        scheme.pk,
        scheme.reset_mode,
        business_date,
        category=asset.category if category_scoped else None,
        category_scope_level=(
            scheme.category_scope_level if category_scoped else None
        ),
    )
    counter = _insert_counter_if_missing(
        company=asset.company, scheme=scheme, scope_key=scope_key
    )
    next_value = counter.current_value + 1
    display = render_code(
        list(scheme.segments.order_by("sequence_order")),
        {
            "company": asset.company,
            "category": asset.category,
            "department": asset.department,
            "effective_date": business_date,
        },
        next_value,
    )
    normalized = normalize_code(display)
    if IssuedCode.objects.filter(
        company=asset.company, normalized_code=normalized
    ).exists():
        raise ValidationError("发号引擎生成的新编号已被永久占用，请复核计数器。")
    _enable_capability("eam_lite.controlled_sequence_counter_increment")
    counter.current_value = next_value
    counter.save(update_fields=["current_value", "updated_at"])
    issued = IssuedCode(
        company=asset.company,
        coding_scheme=scheme,
        scope_key=scope_key,
        sequence_value=next_value,
        display_code=display,
        normalized_code=normalized,
        effective_date=business_date,
        effective_date_reason=explanation,
        status="active",
        idempotency_key=key,
        issued_by=actor,
    )
    _save_new(issued)
    now = timezone.now()
    _base_update(IssuedCode, old.pk, {
        "status": "replaced",
        "replaced_or_voided_reason": explanation,
        "replaced_or_voided_at": now,
    })
    history = AssetCodeHistory(
        company=asset.company,
        asset=asset,
        event_type="corrected",
        old_issued_code=old,
        new_issued_code=issued,
        reason=explanation,
        effective_at=datetime.combine(business_date, time.min, tzinfo=SHANGHAI),
        operated_by=actor,
    )
    _save_new(history)

    old_qr = AssetQrIdentity.objects.select_for_update().filter(
        asset=asset, status="active"
    ).first()
    if old_qr is None:
        raise ValidationError("正式资产缺少当前有效二维码身份，编号更正已回滚。")
    if AssetLabelPrintItem.objects.filter(
        qr_identity=old_qr,
        batch__status="generated",
        print_status="generated",
    ).exists():
        raise ValidationError("当前二维码存在未确认打印批次，请先确认或取消。")
    _base_update(AssetQrIdentity, old_qr.pk, {
        "status": "revoked",
        "revoked_at": now,
        "revoked_by_id": actor.pk,
        "revoke_reason": f"正式编号更正：{explanation}",
    }, "eam_lite.controlled_qr_identity_mutation")
    new_qr = AssetQrIdentity(
        company=asset.company,
        asset=asset,
        public_token=generate_public_token(),
        status="active",
        label_status="ready_to_print",
        issued_by=actor,
        version=old_qr.version + 1,
    )
    _save_new(new_qr, "eam_lite.controlled_qr_identity_mutation")
    _base_update(Asset, asset.pk, {
        "asset_code": display,
        "current_issued_code_id": issued.pk,
        "updated_by_id": actor.pk,
    })
    _audit(
        actor=actor,
        action="asset_code.corrected",
        instance=asset,
        old={
            "asset_code": old.display_code,
            "issued_code_id": str(old.pk),
            "qr_identity_id": str(old_qr.pk),
        },
        new={
            **payload,
            "asset_code": display,
            "issued_code_id": str(issued.pk),
            "qr_identity_id": str(new_qr.pk),
            "qr_label_status": "ready_to_print",
        },
        request=request,
    )
    return issued


@transaction.atomic
def archive_asset(
    *, actor, asset, reason, idempotency_key, request=None,
):
    return _set_record_status(
        actor=actor, asset=asset, from_status="active", to_status="archived",
        operation="archive", reason=reason, idempotency_key=idempotency_key,
        request=request,
    )


@transaction.atomic
def restore_asset_visibility(
    *, actor, asset, reason, idempotency_key, request=None,
):
    return _set_record_status(
        actor=actor, asset=asset, from_status="archived", to_status="active",
        operation="restore_visibility", reason=reason,
        idempotency_key=idempotency_key, request=request,
    )


def _set_record_status(
    *, actor, asset, from_status, to_status, operation, reason,
    idempotency_key, request=None,
):
    from apps.assets.models import Asset

    asset = _lock_asset(asset)
    require_lifecycle_action(actor, asset, operation)
    reason = _required(reason, "reason", "归档或恢复显示必须填写原因。")
    payload = {"asset_id": asset.pk, "from_record_status": from_status,
               "to_record_status": to_status, "reason": reason}
    key, digest, existing = _check_operation_idempotency(
        company=asset.company, operation=operation, key=idempotency_key,
        payload=payload, model=Asset,
    )
    if existing is not None:
        return existing
    if asset.asset_status not in TERMINAL_STATUSES:
        raise ValidationError("只有处置终态正式资产可以归档或恢复显示。")
    if asset.record_status != from_status:
        raise ValidationError("记录显示状态已变化，请刷新后重试。")
    _base_update(Asset, asset.pk, {"record_status": to_status, "updated_by_id": actor.pk})
    asset.refresh_from_db()
    _audit(actor=actor, action=f"asset_lifecycle.{operation}", instance=asset,
           old={"record_status": from_status},
           new={"record_status": to_status, "reason": reason}, request=request)
    _write_operation_marker(actor=actor, operation=operation, result=asset,
                            key=key, digest=digest, payload=payload, request=request)
    return asset


__all__ = [
    "activate_asset", "archive_asset", "assign_asset", "cancel_disposal",
    "change_asset_assignment", "complete_asset_repair", "complete_disposal",
    "correct_asset_code",
    "initiate_disposal", "loan_asset", "lock_disposal_financial_snapshot",
    "record_disposal_actual_details", "restore_asset_visibility",
    "return_asset_assignment", "return_loan", "reverse_disposal",
    "send_asset_for_repair", "set_asset_idle", "transfer_asset",
    "upload_disposal_attachment", "void_disposal_attachment",
]
