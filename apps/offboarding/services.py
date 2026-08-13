"""Transactional domain services for Sprint 10 employee offboarding.

Every public mutation is keyword-only, re-authorizes after acquiring the
authoritative company row lock, and keeps clearance counters derived from item
state.  Physical asset changes are delegated to the Sprint 7 lifecycle
services; this module only coordinates their evidence with the clearance.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, time, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Q
from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company, role_names_for
from apps.offboarding.domain import (
    ACTIVE_CLEARANCE_STATUSES,
    FORMAL_NON_TERMINAL_ASSET_STATUSES,
    SHANGHAI,
    TERMINAL_ASSET_STATUSES,
    UNRESOLVED_ITEM_RESOLUTIONS,
    clearance_status_for_unresolved,
    location_path,
    validate_termination_date,
)
from apps.offboarding.permissions import (
    can_manage_clearance_attachment,
    require_complete_clearance,
    require_create_supplemental_clearance,
    require_initiate_clearance,
    require_refresh_clearance,
    require_view_clearance_attachment,
    scoped_clearance_items,
)


RESOLUTION_MOVEMENT_TYPES = frozenset(
    {"assignment_return", "transfer", "loan_return"}
)
SYNC_ROLES = frozenset(
    {"finance", "equipment", "warehouse", "department_manager", "hr"}
)


def _locked_self(queryset):
    """Lock only the base row when nullable select_related joins are present."""

    if connection.vendor == "postgresql":
        return queryset.select_for_update(of=("self",))
    return queryset.select_for_update()


def _required(value, field_name, message=None):
    result = str(value or "").strip()
    if not result:
        raise ValidationError({field_name: message or "不能为空。"})
    return result


def _idempotency_key(value):
    key = _required(value, "idempotency_key", "必须提供幂等键。")
    if len(key) > 128:
        raise ValidationError({"idempotency_key": "幂等键长度不能超过 128。"})
    return key


def _enable_capability(name):
    if connection.vendor != "postgresql":
        return
    setting = name if name.startswith("eam_lite.") else f"eam_lite.{name}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [setting, "on"])


def _base_update(model, pk, values, capability):
    _enable_capability(capability)
    if QuerySet.update(model._base_manager.filter(pk=pk), **values) != 1:
        raise ValidationError("受控更新未命中唯一清退记录。")


def _save_new(instance, capability):
    _enable_capability(capability)
    instance.full_clean()
    try:
        instance.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("保存失败：请求与既有清退记录冲突。") from exc
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


def _selected_company(company_id, *, serialize_manager_write=False):
    from apps.masterdata.models import Company

    selected = current_company()
    if selected is None or selected.pk != company_id:
        raise PermissionDenied("目标记录不属于当前公司。")
    if serialize_manager_write:
        # Employee writes participate in masterdata's manager-reference
        # protocol.  Acquire its advisory locks before the shared Company and
        # Employee row-lock order, matching update_employee() and preventing an
        # advisory-lock/row-lock inversion.
        from apps.masterdata.services import _lock_manager_write

        _lock_manager_write(company_id)
    return Company.objects.select_for_update().get(pk=company_id)


def _lock_employee(employee):
    from apps.masterdata.models import Employee

    employee_id = getattr(employee, "pk", employee)
    try:
        company_id = Employee.objects.values_list("company_id", flat=True).get(
            pk=employee_id
        )
    except (Employee.DoesNotExist, TypeError, ValueError) as exc:
        raise PermissionDenied("目标员工不存在。") from exc
    company = _selected_company(company_id, serialize_manager_write=True)
    queryset = _locked_self(Employee.objects.all())
    return queryset.select_related(
        "company", "department", "user"
    ).get(pk=employee_id, company=company)


def _lock_clearance(clearance, *, serialize_initial_employee_write=False):
    from apps.offboarding.models import EmployeeAssetClearance

    clearance_id = getattr(clearance, "pk", clearance)
    try:
        raw = EmployeeAssetClearance.objects.values(
            "company_id", "employee_id", "supplements_clearance_id", "status"
        ).get(pk=clearance_id)
    except (EmployeeAssetClearance.DoesNotExist, TypeError, ValueError) as exc:
        raise PermissionDenied("目标清退单不存在。") from exc
    company = _selected_company(
        raw["company_id"],
        serialize_manager_write=(
            serialize_initial_employee_write
            and raw["supplements_clearance_id"] is None
            and raw["status"] in ACTIVE_CLEARANCE_STATUSES
        ),
    )
    # Company is the common serialization root used by lifecycle services.
    from apps.masterdata.models import Employee

    Employee.objects.select_for_update().get(
        pk=raw["employee_id"], company=company
    )
    queryset = _locked_self(EmployeeAssetClearance.objects.all())
    return queryset.select_related(
        "company", "employee__department", "supplements_clearance"
    ).get(pk=clearance_id, company=company)


def _require_sync_actor(actor):
    if not role_names_for(actor).intersection(SYNC_ROLES):
        raise PermissionDenied("当前用户不能同步离职清退项目。")


def _responsibility_effective_at(asset, employee):
    """Return durable evidence for the employee's current responsibility."""

    from apps.assets.models import AssetMovement, AssetQrIdentity

    if asset.asset_status == "pending_label":
        identity = AssetQrIdentity.objects.filter(
            company=asset.company,
            asset=asset,
            status=AssetQrIdentity.Status.ACTIVE,
        ).only("issued_at").first()
        if identity is None:
            raise ValidationError(
                {"asset": f"待贴标资产 {asset.asset_code} 缺少有效二维码签发证据。"}
            )
        return identity.issued_at

    movement = (
        AssetMovement.objects.filter(
            company=asset.company,
            asset=asset,
            to_employee=employee,
        )
        .filter(
            Q(movement_type="label_activation")
            | ~Q(from_employee_id=F("to_employee_id"))
        )
        .order_by("-effective_at", "-created_at", "-pk")
        .first()
    )
    if movement is None:
        raise ValidationError(
            {"asset": f"资产 {asset.asset_code} 缺少建立当前责任关系的变动证据。"}
        )
    return movement.effective_at


def _collect_sources(employee):
    """Collect current structured responsibility/loan sources by asset id."""

    from apps.assets.models import Asset, AssetLoan

    asset_qs = Asset.objects.select_related(
        "company", "department", "responsible_employee", "location"
    ).filter(
        company=employee.company,
        record_status="active",
        asset_status__in=FORMAL_NON_TERMINAL_ASSET_STATUSES,
    )
    result = {}
    for asset in asset_qs.filter(responsible_employee=employee).order_by("pk"):
        result[asset.pk] = {
            "asset": asset,
            "responsibility_at": _responsibility_effective_at(asset, employee),
            "loan": None,
            "loan_at": None,
        }

    loans = AssetLoan.objects.select_related(
        "asset__company",
        "asset__department",
        "asset__responsible_employee",
        "asset__location",
        "loan_movement",
    ).filter(
        company=employee.company,
        borrower_type=AssetLoan.BorrowerType.INTERNAL_EMPLOYEE,
        borrower_employee=employee,
        status=AssetLoan.Status.ACTIVE,
        asset__record_status="active",
        asset__asset_status__in=FORMAL_NON_TERMINAL_ASSET_STATUSES,
    ).order_by("asset_id", "pk")
    for loan in loans:
        entry = result.setdefault(
            loan.asset_id,
            {
                "asset": loan.asset,
                "responsibility_at": None,
                "loan": None,
                "loan_at": None,
            },
        )
        if entry["loan"] is not None:
            raise ValidationError(
                {"asset": f"资产 {loan.asset.asset_code} 存在多条活动内部借用记录。"}
            )
        entry["loan"] = loan
        entry["loan_at"] = datetime.combine(
            loan.loan_date, time.min, tzinfo=SHANGHAI
        )
    return result


def _source_values(entry):
    responsibility_at = entry["responsibility_at"]
    loan_at = entry["loan_at"]
    if responsibility_at is not None and loan_at is not None:
        return "both", entry["loan"], max(responsibility_at, loan_at)
    if responsibility_at is not None:
        return "responsibility", None, responsibility_at
    if loan_at is not None:
        return "internal_loan", entry["loan"], loan_at
    raise ValidationError("清退来源不能为空。")


def _create_item(
    *, clearance, entry, discovered_at, added_during_clearance=False,
    addition_reason="",
):
    from apps.offboarding.models import EmployeeAssetClearanceItem

    asset = entry["asset"]
    if not asset.department_id or not asset.responsible_employee_id or not asset.location_id:
        raise ValidationError(
            {"asset": f"正式资产 {asset.asset_code} 的部门、责任人或位置不完整。"}
        )
    source_type, source_loan, association_at = _source_values(entry)
    item = EmployeeAssetClearanceItem(
        company=clearance.company,
        clearance=clearance,
        asset=asset,
        source_type=source_type,
        source_loan=source_loan,
        association_effective_at=association_at,
        discovered_at=discovered_at,
        addition_reason=addition_reason,
        asset_code_snapshot=asset.asset_code,
        asset_name_snapshot=asset.asset_name,
        original_department=asset.department,
        original_employee=asset.responsible_employee,
        original_location=asset.location,
        original_department_snapshot=asset.department.name,
        original_employee_snapshot=asset.responsible_employee.name,
        original_location_path_snapshot=location_path(asset.location),
        original_status=asset.asset_status,
        added_during_clearance=added_during_clearance,
        resolution=EmployeeAssetClearanceItem.Resolution.PENDING,
    )
    return _save_new(item, "controlled_clearance_item_insert")


def _recount(clearance):
    from apps.offboarding.models import (
        EmployeeAssetClearance,
        EmployeeAssetClearanceItem,
    )

    # Terminal clearance snapshots and their cached counters are immutable.
    # Later lifecycle hooks may still encounter those historical rows, but a
    # read-only sync must not issue even a no-op UPDATE against them.
    if clearance.status not in ACTIVE_CLEARANCE_STATUSES:
        clearance.refresh_from_db()
        return clearance

    items = EmployeeAssetClearanceItem.objects.filter(clearance=clearance)
    total = items.count()
    unresolved = items.filter(
        resolution__in=UNRESOLVED_ITEM_RESOLUTIONS
    ).count()
    values = {
        "total_assets_snapshot": total,
        "unresolved_assets": unresolved,
    }
    if clearance.status in ACTIVE_CLEARANCE_STATUSES:
        values["status"] = clearance_status_for_unresolved(unresolved)
    _base_update(
        EmployeeAssetClearance,
        clearance.pk,
        values,
        "controlled_clearance_mutation",
    )
    clearance.refresh_from_db()
    return clearance


def _set_employee_offboarding(employee, *, status, termination_date=None):
    from apps.masterdata.models import Employee

    _base_update(
        Employee,
        employee.pk,
        {
            "employment_status": status,
            "termination_date": termination_date,
            "is_active": False,
            "updated_at": timezone.now(),
        },
        "controlled_employee_offboarding",
    )
    employee.refresh_from_db()
    return employee


def _existing_key(company, key, employee):
    from apps.offboarding.models import EmployeeAssetClearance

    existing = EmployeeAssetClearance.objects.filter(
        company=company, idempotency_key=key
    ).first()
    if existing is not None and existing.employee_id != employee.pk:
        raise ValidationError({"idempotency_key": "该幂等键已用于其他员工清退。"})
    return existing


@transaction.atomic
def initiate_clearance(
    *, actor, employee, idempotency_key, remark="", request=None,
):
    """Atomically move active -> leaving and create its immutable snapshot."""

    from apps.masterdata.services import _clear_manager_assignments
    from apps.offboarding.models import EmployeeAssetClearance

    employee = _lock_employee(employee)
    require_initiate_clearance(actor, employee)
    key = _idempotency_key(idempotency_key)
    normalized_remark = str(remark or "").strip()
    active = EmployeeAssetClearance.objects.select_for_update().filter(
        company=employee.company,
        employee=employee,
        status__in=ACTIVE_CLEARANCE_STATUSES,
    ).first()
    if active is not None:
        if active.idempotency_key == key and active.remark != normalized_remark:
            raise ValidationError(
                {"idempotency_key": "同一幂等键的备注与原发起请求不一致。"}
            )
        return active
    existing = _existing_key(employee.company, key, employee)
    if existing is not None:
        if existing.remark != normalized_remark:
            raise ValidationError(
                {"idempotency_key": "同一幂等键的备注与原发起请求不一致。"}
            )
        return existing
    if employee.employment_status != "active":
        raise ValidationError(
            {"employee": "只有在职员工可以发起首次离职清退。"}
        )

    initiated_at = timezone.now()
    sources = _collect_sources(employee)
    _clear_manager_assignments(employee=employee, actor=actor, request=request)
    _set_employee_offboarding(employee, status="leaving")
    clearance = _save_new(
        EmployeeAssetClearance(
            company=employee.company,
            employee=employee,
            initiated_at=initiated_at,
            initiated_by=actor,
            total_assets_snapshot=0,
            unresolved_assets=0,
            status=EmployeeAssetClearance.Status.OPEN,
            remark=normalized_remark,
            idempotency_key=key,
        ),
        "controlled_clearance_insert",
    )
    for entry in sources.values():
        _, _, association_at = _source_values(entry)
        if association_at > initiated_at:
            raise ValidationError("资产关联生效时间不得晚于清退发起时间。")
        _create_item(
            clearance=clearance,
            entry=entry,
            discovered_at=initiated_at,
        )
    _recount(clearance)
    _audit(
        actor=actor,
        action="employee_offboarding.initiated",
        instance=clearance,
        new={
            "employee_id": employee.pk,
            "employment_status": "leaving",
            "item_count": clearance.total_assets_snapshot,
            "unresolved_assets": clearance.unresolved_assets,
        },
        request=request,
    )
    return clearance


def _active_internal_loan(item, asset):
    from apps.assets.models import AssetLoan

    return AssetLoan.objects.filter(
        company=item.company,
        asset=asset,
        borrower_type=AssetLoan.BorrowerType.INTERNAL_EMPLOYEE,
        borrower_employee_id=item.clearance.employee_id,
        status=AssetLoan.Status.ACTIVE,
    ).first()


def _resolution_movement(item, supplied=None):
    from apps.assets.models import AssetMovement

    employee_id = item.clearance.employee_id
    candidates = []
    if (
        supplied is not None
        and supplied.asset_id == item.asset_id
        and supplied.movement_type in RESOLUTION_MOVEMENT_TYPES
    ):
        candidates.append(supplied)
    if item.source_loan_id:
        loan = item.source_loan
        if loan.return_movement_id:
            candidates.append(loan.return_movement)
    movement = AssetMovement.objects.filter(
        company=item.company,
        asset_id=item.asset_id,
        movement_type__in=RESOLUTION_MOVEMENT_TYPES,
        from_employee_id=employee_id,
    ).exclude(to_employee_id=employee_id).order_by(
        "-effective_at", "-created_at", "-pk"
    ).first()
    if movement is not None:
        candidates.append(movement)
    if not candidates:
        return None
    return max(candidates, key=lambda value: (value.effective_at, value.created_at))


def _item_values_for_state(item, asset, *, actor, movement=None, disposal=None):
    from apps.offboarding.models import EmployeeAssetClearanceItem

    employee_id = item.clearance.employee_id
    active_responsibility = bool(
        asset.record_status == "active"
        and asset.asset_status in FORMAL_NON_TERMINAL_ASSET_STATUSES
        and asset.responsible_employee_id == employee_id
    )
    active_loan = _active_internal_loan(item, asset)
    evidence_disposal = disposal
    if evidence_disposal is None and item.disposal_id:
        evidence_disposal = item.disposal
        evidence_disposal.refresh_from_db()

    if evidence_disposal is not None:
        if (
            evidence_disposal.asset_id != asset.pk
            or evidence_disposal.company_id != item.company_id
        ):
            raise ValidationError({"disposal": "处置记录与清退资产不匹配。"})
        if (
            evidence_disposal.status == "confirmed"
            and asset.asset_status in TERMINAL_ASSET_STATUSES
            and active_loan is None
        ):
            return {
                "resolution": EmployeeAssetClearanceItem.Resolution.DISPOSED,
                "resolved_by_id": evidence_disposal.confirmed_by_id,
                "resolved_at": evidence_disposal.confirmed_at or timezone.now(),
                "movement_id": None,
                "disposal_id": evidence_disposal.pk,
            }
        if evidence_disposal.status in {"draft", "finance_locked"}:
            return {
                "resolution": EmployeeAssetClearanceItem.Resolution.DISPOSAL_IN_PROGRESS,
                "resolved_by_id": None,
                "resolved_at": None,
                "movement_id": None,
                "disposal_id": evidence_disposal.pk,
            }

    if not active_responsibility and active_loan is None:
        evidence = _resolution_movement(item, movement)
        if evidence is not None:
            resolution = (
                EmployeeAssetClearanceItem.Resolution.RETURNED
                if evidence.movement_type in {"assignment_return", "loan_return"}
                else EmployeeAssetClearanceItem.Resolution.TRANSFERRED
            )
            return {
                "resolution": resolution,
                "resolved_by_id": evidence.operated_by_id,
                "resolved_at": evidence.effective_at,
                "movement_id": evidence.pk,
                "disposal_id": None,
            }

    return {
        "resolution": EmployeeAssetClearanceItem.Resolution.PENDING,
        "resolved_by_id": None,
        "resolved_at": None,
        "movement_id": None,
        "disposal_id": None,
    }


def _sync_locked_item(item, *, actor, movement=None, disposal=None, request=None):
    from apps.assets.models import Asset
    from apps.offboarding.models import EmployeeAssetClearanceItem

    if item.clearance.status not in ACTIVE_CLEARANCE_STATUSES:
        return item
    asset_qs = _locked_self(Asset.objects.all())
    asset = asset_qs.select_related(
        "company", "department", "responsible_employee", "location"
    ).get(pk=item.asset_id, company=item.company)
    values = _item_values_for_state(
        item, asset, actor=actor, movement=movement, disposal=disposal
    )
    old = {
        "resolution": item.resolution,
        "movement_id": item.movement_id,
        "disposal_id": item.disposal_id,
    }
    comparable = {
        "resolution": values["resolution"],
        "movement_id": values["movement_id"],
        "disposal_id": values["disposal_id"],
    }
    if old != comparable:
        _base_update(
            EmployeeAssetClearanceItem,
            item.pk,
            values,
            "controlled_clearance_item_resolution",
        )
        item.refresh_from_db()
        _audit(
            actor=actor,
            action="employee_offboarding.item_synchronized",
            instance=item,
            old=old,
            new={**comparable, "resolved_at": item.resolved_at},
            request=request,
        )
    return item


@transaction.atomic
def sync_clearance_item(
    *, actor, item, movement=None, disposal=None, request=None,
):
    """Synchronize one existing item from authoritative lifecycle evidence."""

    from apps.offboarding.models import EmployeeAssetClearanceItem

    _require_sync_actor(actor)
    raw = EmployeeAssetClearanceItem.objects.select_related("clearance").get(
        pk=getattr(item, "pk", item)
    )
    clearance = _lock_clearance(raw.clearance_id)
    item_qs = _locked_self(EmployeeAssetClearanceItem.objects.all())
    item = item_qs.select_related(
        "company", "clearance", "source_loan__return_movement", "disposal"
    ).get(pk=raw.pk, clearance=clearance)
    if not scoped_clearance_items(actor, item.company).filter(pk=item.pk).exists():
        raise PermissionDenied("您没有同步此离职清退项目的权限。")
    _sync_locked_item(
        item, actor=actor, movement=movement, disposal=disposal, request=request
    )
    _recount(clearance)
    item.refresh_from_db()
    return item


@transaction.atomic
def sync_clearance_items_for_asset(
    *, actor, asset, movement=None, disposal=None, request=None,
):
    """Lifecycle hook: update every active clearance item for one asset."""

    from apps.assets.models import Asset
    from apps.offboarding.models import EmployeeAssetClearanceItem

    _require_sync_actor(actor)
    asset_id = getattr(asset, "pk", asset)
    company_id = Asset.objects.values_list("company_id", flat=True).get(pk=asset_id)
    _selected_company(company_id)
    items = list(
        _locked_self(EmployeeAssetClearanceItem.objects.all())
        .select_related(
            "company", "clearance", "source_loan__return_movement", "disposal"
        )
        .filter(
            company_id=company_id,
            asset_id=asset_id,
            clearance__status__in=ACTIVE_CLEARANCE_STATUSES,
        )
        .order_by("pk")
    )
    clearances = {}
    for item in items:
        _sync_locked_item(
            item, actor=actor, movement=movement, disposal=disposal, request=request
        )
        clearances[item.clearance_id] = item.clearance
    for clearance in clearances.values():
        _recount(clearance)
    return items


def _refresh_locked(clearance, *, actor, reason, request=None):
    from apps.offboarding.models import EmployeeAssetClearanceItem

    if clearance.status not in ACTIVE_CLEARANCE_STATUSES:
        raise ValidationError("只有处理中清退单可以刷新。")
    explanation = _required(reason, "reason", "刷新清退单必须填写原因。")
    sources = _collect_sources(clearance.employee)
    existing = {
        item.asset_id: item
        for item in _locked_self(EmployeeAssetClearanceItem.objects.all())
        .select_related(
            "company", "clearance", "source_loan__return_movement", "disposal"
        )
        .filter(clearance=clearance)
    }
    changed, skipped = [], []
    for item in existing.values():
        _sync_locked_item(item, actor=actor, request=request)
    discovered_at = timezone.now()
    if discovered_at <= clearance.initiated_at:
        discovered_at = clearance.initiated_at + timedelta(microseconds=1)
    for asset_id, entry in sources.items():
        _, _, association_at = _source_values(entry)
        if association_at > clearance.initiated_at:
            skipped.append(str(asset_id))
            continue
        item = existing.get(asset_id)
        if item is None:
            item = _create_item(
                clearance=clearance,
                entry=entry,
                discovered_at=discovered_at,
                added_during_clearance=True,
                addition_reason=explanation,
            )
            changed.append(str(item.pk))
        # An existing item's source and original tuple are immutable snapshot
        # evidence. A newly rediscovered second source still blocks resolution
        # through the authoritative current-relationship check, but does not
        # rewrite that historical snapshot.
    _recount(clearance)
    _audit(
        actor=actor,
        action="employee_offboarding.refreshed",
        instance=clearance,
        new={
            "reason": explanation,
            "changed_item_ids": changed,
            "post_initiation_asset_ids_skipped": skipped,
            "unresolved_assets": clearance.unresolved_assets,
        },
        request=request,
    )
    return clearance


@transaction.atomic
def refresh_clearance(*, actor, clearance, reason, request=None):
    clearance = _lock_clearance(clearance)
    require_refresh_clearance(actor, clearance)
    return _refresh_locked(
        clearance, actor=actor, reason=reason, request=request
    )


@transaction.atomic
def create_supplemental_clearance(
    *, actor, original_clearance, reason, idempotency_key, remark="", request=None,
):
    from apps.offboarding.models import EmployeeAssetClearance

    original = _lock_clearance(original_clearance)
    require_create_supplemental_clearance(actor, original)
    explanation = _required(reason, "reason", "补充清退原因必填。")
    key = _idempotency_key(idempotency_key)
    if original.supplements_clearance_id is not None:
        raise ValidationError("补充清退必须直接指向首次清退单。")
    if original.status != EmployeeAssetClearance.Status.COMPLETED:
        raise ValidationError("只能为已完成的首次清退建立补充清退。")
    employee = original.employee
    employee.refresh_from_db()
    if employee.employment_status != "resigned" or employee.termination_date is None:
        raise ValidationError("只有已完成离职的人事记录可以建立补充清退。")
    normalized_remark = str(remark or "").strip()
    active = EmployeeAssetClearance.objects.select_for_update().filter(
        company=original.company,
        employee=employee,
        status__in=ACTIVE_CLEARANCE_STATUSES,
    ).first()
    if active is not None:
        if active.supplements_clearance_id != original.pk:
            raise ValidationError("该员工已有其他处理中清退单。")
        if active.idempotency_key == key and (
            active.supplement_reason != explanation
            or active.remark != normalized_remark
        ):
            raise ValidationError(
                {"idempotency_key": "同一幂等键的补充原因或备注与原请求不一致。"}
            )
        return active
    existing = _existing_key(original.company, key, employee)
    if existing is not None:
        if (
            existing.supplements_clearance_id != original.pk
            or existing.supplement_reason != explanation
            or existing.remark != normalized_remark
        ):
            raise ValidationError(
                {"idempotency_key": "该幂等键对应另一补充清退请求。"}
            )
        return existing
    initiated_at = timezone.now()
    sources = _collect_sources(employee)
    if not sources:
        raise ValidationError("当前未发现需要补充清退的异常资产。")
    original_asset_ids = set(
        original.items.values_list("asset_id", flat=True)
    )
    duplicated = sorted(str(value) for value in original_asset_ids.intersection(sources))
    if duplicated:
        raise ValidationError(
            "当前异常资产已被原清退单覆盖，不能重复建立补充项目："
            + "、".join(duplicated)
        )
    post_original = []
    for asset_id, entry in sources.items():
        _, _, association_at = _source_values(entry)
        if association_at > original.initiated_at:
            post_original.append(str(asset_id))
    if post_original:
        raise ValidationError(
            "发现首次清退发起后才建立的员工资产关系；这不是历史遗漏，必须先纠正："
            + "、".join(sorted(post_original))
        )
    clearance = _save_new(
        EmployeeAssetClearance(
            company=original.company,
            employee=employee,
            supplements_clearance=original,
            supplement_reason=explanation,
            initiated_at=initiated_at,
            initiated_by=actor,
            status=EmployeeAssetClearance.Status.OPEN,
            remark=normalized_remark,
            idempotency_key=key,
        ),
        "controlled_clearance_insert",
    )
    for entry in sources.values():
        _, _, association_at = _source_values(entry)
        if association_at > initiated_at:
            raise ValidationError("资产关联生效时间不得晚于补充清退发起时间。")
        _create_item(
            clearance=clearance,
            entry=entry,
            discovered_at=initiated_at,
        )
    _recount(clearance)
    _audit(
        actor=actor,
        action="employee_offboarding.supplement_created",
        instance=clearance,
        new={
            "original_clearance_id": str(original.pk),
            "reason": explanation,
            "item_count": clearance.total_assets_snapshot,
        },
        request=request,
    )
    return clearance


def _current_sources_must_be_covered(clearance, *, actor, request=None):
    """Final verification: add historical misses, block post-init corruption."""

    from apps.offboarding.models import EmployeeAssetClearanceItem

    sources = _collect_sources(clearance.employee)
    existing = {
        item.asset_id: item
        for item in _locked_self(EmployeeAssetClearanceItem.objects.all())
        .select_related(
            "company", "clearance", "source_loan__return_movement", "disposal"
        )
        .filter(clearance=clearance)
    }
    post_initiation = []
    discovered_at = max(
        timezone.now(), clearance.initiated_at + timedelta(microseconds=1)
    )
    for asset_id, entry in sources.items():
        _, _, association_at = _source_values(entry)
        if association_at > clearance.initiated_at:
            post_initiation.append(str(asset_id))
            continue
        if asset_id not in existing:
            _create_item(
                clearance=clearance,
                entry=entry,
                discovered_at=discovered_at,
                added_during_clearance=True,
                addition_reason="完成清退前系统最终核对发现历史关联",
            )
    if post_initiation:
        raise ValidationError(
            "存在清退发起后新增给离职员工的资产关系，必须先纠正后才能完成："
            + "、".join(post_initiation)
        )


@transaction.atomic
def complete_clearance(
    *, actor, clearance, termination_date=None, request=None,
):
    from apps.assets.models import Asset
    from apps.offboarding.models import (
        EmployeeAssetClearance,
        EmployeeAssetClearanceItem,
    )

    clearance = _lock_clearance(
        clearance, serialize_initial_employee_write=True
    )
    require_complete_clearance(actor, clearance)
    if clearance.status not in ACTIVE_CLEARANCE_STATUSES:
        if clearance.status == EmployeeAssetClearance.Status.COMPLETED:
            employee = clearance.employee
            if clearance.supplements_clearance_id is None:
                if (
                    termination_date is not None
                    and termination_date != employee.termination_date
                ):
                    raise ValidationError(
                        {"termination_date": "该清退单已按另一实际离职日期完成。"}
                    )
            elif termination_date not in (None, employee.termination_date):
                raise ValidationError("补充清退不得改写原实际离职日期。")
            return clearance
        raise ValidationError("只有处理中清退单可以完成。")
    item_qs = _locked_self(EmployeeAssetClearanceItem.objects.all())
    item_qs = item_qs.select_related(
        "company", "clearance", "source_loan__return_movement", "disposal"
    ).filter(clearance=clearance).order_by("pk")
    # Lock related assets in deterministic order before the final state check.
    list(
        Asset.objects.select_for_update().filter(
            pk__in=item_qs.values_list("asset_id", flat=True)
        ).order_by("pk")
    )
    for item in list(item_qs):
        _sync_locked_item(item, actor=actor, request=request)
    _current_sources_must_be_covered(clearance, actor=actor, request=request)
    _recount(clearance)
    if clearance.unresolved_assets:
        raise ValidationError(
            {"clearance": f"仍有 {clearance.unresolved_assets} 项资产未解决，不能完成清退。"}
        )

    employee = clearance.employee
    employee.refresh_from_db()
    now = timezone.now()
    if clearance.supplements_clearance_id is None:
        if employee.employment_status != "leaving":
            raise ValidationError("首次清退只能完成 leaving 状态员工。")
        termination = validate_termination_date(
            employee=employee, termination_date=termination_date
        )
        _set_employee_offboarding(
            employee, status="resigned", termination_date=termination
        )
    else:
        if employee.employment_status != "resigned" or employee.termination_date is None:
            raise ValidationError("补充清退完成时员工必须保持已离职状态。")
        if termination_date not in (None, employee.termination_date):
            raise ValidationError("补充清退不得改写原实际离职日期。")

    _base_update(
        EmployeeAssetClearance,
        clearance.pk,
        {
            "status": EmployeeAssetClearance.Status.COMPLETED,
            "unresolved_assets": 0,
            "completed_at": now,
            "completed_by_id": actor.pk,
        },
        "controlled_clearance_mutation",
    )
    clearance.refresh_from_db()
    _audit(
        actor=actor,
        action="employee_offboarding.completed",
        instance=clearance,
        old={"status": "open_or_blocked"},
        new={
            "status": "completed",
            "termination_date": employee.termination_date,
            "supplemental": clearance.supplements_clearance_id is not None,
        },
        request=request,
    )
    return clearance


def _actionable_item(item):
    from apps.offboarding.models import EmployeeAssetClearanceItem

    item_id = getattr(item, "pk", item)
    raw = EmployeeAssetClearanceItem.objects.values(
        "company_id", "clearance_id"
    ).get(pk=item_id)
    selected = current_company()
    if selected is None or selected.pk != raw["company_id"]:
        raise PermissionDenied("目标清退项目不属于当前公司。")
    # Serialize against every lifecycle mutation in the same company, then
    # lock and re-read this Item. This prevents two handlers from both acting
    # on a stale `pending` value and creating two authoritative Movements.
    _selected_company(raw["company_id"])
    item_qs = _locked_self(EmployeeAssetClearanceItem.objects.all())
    raw = item_qs.select_related(
        "company", "clearance", "asset", "source_loan"
    ).get(pk=item_id, company_id=raw["company_id"])
    if raw.clearance.status not in ACTIVE_CLEARANCE_STATUSES:
        raise ValidationError("清退单已结束，不能继续处理项目。")
    if raw.resolution not in UNRESOLVED_ITEM_RESOLUTIONS:
        raise ValidationError("该清退项目已处理，请刷新后重试。")
    return raw


@transaction.atomic
def return_clearance_item(
    *, actor, item, returned_at, received_by_employee, return_department,
    return_responsible_employee, return_location, return_asset_status,
    idempotency_key, remark="", request=None,
):
    from apps.assets.lifecycle_services import return_asset_assignment, return_loan
    from apps.assets.models import AssetLoan

    item = _actionable_item(item)
    loan = AssetLoan.objects.filter(
        company=item.company,
        asset=item.asset,
        status=AssetLoan.Status.ACTIVE,
    ).first()
    if loan is not None:
        loan = return_loan(
            actor=actor,
            loan=loan,
            returned_at=returned_at,
            received_by_employee=received_by_employee,
            return_department=return_department,
            return_responsible_employee=return_responsible_employee,
            return_location=return_location,
            return_asset_status=return_asset_status,
            idempotency_key=idempotency_key,
            remark=remark,
            request=request,
        )
        movement = loan.return_movement
    else:
        movement = return_asset_assignment(
            actor=actor,
            asset=item.asset,
            to_department=return_department,
            to_responsible_employee=return_responsible_employee,
            to_location=return_location,
            effective_at=returned_at,
            reason="离职资产清退归还",
            idempotency_key=idempotency_key,
            to_status=return_asset_status,
            remark=remark,
            request=request,
        )
    return sync_clearance_item(
        actor=actor, item=item, movement=movement, request=request
    )


@transaction.atomic
def transfer_clearance_item(
    *, actor, item, to_department, to_responsible_employee, to_location,
    effective_at, reason, idempotency_key, remark="", request=None,
):
    from apps.assets.lifecycle_services import transfer_asset

    item = _actionable_item(item)
    movement = transfer_asset(
        actor=actor,
        asset=item.asset,
        to_department=to_department,
        to_responsible_employee=to_responsible_employee,
        to_location=to_location,
        effective_at=effective_at,
        reason=reason,
        idempotency_key=idempotency_key,
        remark=remark,
        request=request,
    )
    return sync_clearance_item(
        actor=actor, item=item, movement=movement, request=request
    )


@transaction.atomic
def mark_clearance_item_disposal_started(
    *, actor, item, disposal, request=None,
):
    item = _actionable_item(item)
    if disposal.asset_id != item.asset_id or disposal.status not in {
        "draft", "finance_locked", "confirmed"
    }:
        raise ValidationError({"disposal": "处置记录与清退项目不匹配或状态无效。"})
    return sync_clearance_item(
        actor=actor, item=item, disposal=disposal, request=request
    )


@transaction.atomic
def upload_clearance_attachment(
    *, actor, target, uploaded_file, security_class="A0", request=None,
):
    from apps.assets.models import AttachmentLink
    from apps.assets.services import (
        MIME_BY_EXTENSION,
        _detect_mime,
        _read_upload,
        _validate_filename,
    )
    from apps.masterdata.models import Attachment
    from apps.masterdata.services import get_system_setting
    from apps.offboarding.models import (
        EmployeeAssetClearance,
        EmployeeAssetClearanceItem,
    )

    model_name = getattr(getattr(target, "_meta", None), "model_name", "")
    if model_name == "employeeassetclearance":
        clearance = _lock_clearance(target)
        target = clearance
        target_values = {"clearance": clearance, "clearance_item": None}
    elif model_name == "employeeassetclearanceitem":
        raw = EmployeeAssetClearanceItem.objects.get(pk=target.pk)
        clearance = _lock_clearance(raw.clearance_id)
        target = EmployeeAssetClearanceItem.objects.select_for_update().get(
            pk=raw.pk, clearance=clearance
        )
        target_values = {"clearance": None, "clearance_item": target}
    else:
        raise ValidationError("目标不是清退单或清退项目。")
    if security_class not in {"A0", "A1"}:
        raise ValidationError({"security_class": "附件安全分类无效。"})
    if not can_manage_clearance_attachment(
        actor, target, security_class=security_class
    ):
        raise PermissionDenied("您没有上传此清退附件的权限。")
    original_name, extension = _validate_filename(uploaded_file.name)
    allowed = set(
        get_system_setting(
            company=clearance.company, key="attachment_allowed_extensions"
        )
    )
    if extension not in allowed or extension not in MIME_BY_EXTENSION:
        raise ValidationError("当前公司未允许该附件扩展名。")
    limit = get_system_setting(
        company=clearance.company, key="attachment_max_size_bytes"
    )
    data = _read_upload(uploaded_file, limit)
    detected_mime = _detect_mime(extension, data)
    client_mime = str(getattr(uploaded_file, "content_type", "") or "").lower()
    if client_mime and client_mime != detected_mime:
        raise ValidationError("客户端 MIME 与文件实际类型不一致。")
    storage_key = (
        f"private/assets/{clearance.company_id}/clearance/"
        f"{uuid.uuid4().hex}.{extension}"
    )
    saved_key = default_storage.save(storage_key, ContentFile(data))
    linked = False
    try:
        attachment = Attachment(
            company=clearance.company,
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
        attachment.full_clean()
        attachment.save(force_insert=True)
        link = AttachmentLink(
            company=clearance.company,
            attachment=attachment,
            role=AttachmentLink.Role.CLEARANCE,
            security_class=security_class,
            created_by=actor,
            **target_values,
        )
        link.full_clean()
        link.save(force_insert=True)
        QuerySet.update(
            Attachment._base_manager.filter(pk=attachment.pk), is_available=True
        )
        attachment.refresh_from_db(fields=("is_available",))
        _audit(
            actor=actor,
            action="employee_offboarding.attachment_uploaded",
            instance=link,
            new={
                "clearance_id": str(clearance.pk),
                "clearance_item_id": str(getattr(target_values["clearance_item"], "pk", "")),
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


def authorize_clearance_attachment_download(*, actor, link):
    """Return the validated link; the HTTP layer may then open storage_key."""

    from apps.assets.models import AttachmentLink

    link = AttachmentLink._base_manager.select_related(
        "attachment", "clearance", "clearance_item__clearance"
    ).get(pk=getattr(link, "pk", link), role=AttachmentLink.Role.CLEARANCE)
    require_view_clearance_attachment(actor, link)
    if (
        not link.attachment.is_available
        or link.attachment.malware_scan_status not in {"policy_limited", "clean"}
    ):
        raise PermissionDenied("附件当前不可下载。")
    return link


@transaction.atomic
def void_clearance_attachment(*, actor, link, reason, request=None):
    from apps.assets.models import AttachmentLink

    raw = AttachmentLink._base_manager.select_related(
        "clearance", "clearance_item__clearance"
    ).get(pk=getattr(link, "pk", link), role=AttachmentLink.Role.CLEARANCE)
    clearance = raw.clearance or raw.clearance_item.clearance
    clearance = _lock_clearance(clearance)
    link_qs = _locked_self(AttachmentLink._base_manager.all())
    link = link_qs.select_related(
        "clearance", "clearance_item__clearance"
    ).get(pk=raw.pk, company=clearance.company)
    target = link.clearance or link.clearance_item
    if not can_manage_clearance_attachment(
        actor, target, security_class=link.security_class
    ):
        raise PermissionDenied("您没有作废此清退附件的权限。")
    if link.status == AttachmentLink.Status.VOIDED:
        return link
    explanation = _required(reason, "reason", "作废附件必须填写原因。")
    now = timezone.now()
    _base_update(
        AttachmentLink,
        link.pk,
        {
            "status": AttachmentLink.Status.VOIDED,
            "void_reason": explanation,
            "voided_by_id": actor.pk,
            "voided_at": now,
        },
        "eam_lite.controlled_asset_mutation",
    )
    link.refresh_from_db()
    _audit(
        actor=actor,
        action="employee_offboarding.attachment_voided",
        instance=link,
        old={"status": "active"},
        new={"status": "voided", "reason": explanation},
        request=request,
    )
    return link


def require_disposal_not_used_by_clearance(disposal):
    """Block V1 reversal once a disposal is resolution evidence anywhere."""

    from apps.offboarding.models import EmployeeAssetClearanceItem

    referenced = EmployeeAssetClearanceItem.objects.select_for_update().filter(
        company_id=disposal.company_id,
        disposal_id=disposal.pk,
        resolution=EmployeeAssetClearanceItem.Resolution.DISPOSED,
    )
    if referenced.exists():
        raise ValidationError(
            "该处置已被离职资产清退作为已处置解决证据引用，V1 不允许冲销。"
        )


__all__ = [
    "authorize_clearance_attachment_download",
    "complete_clearance",
    "create_supplemental_clearance",
    "initiate_clearance",
    "mark_clearance_item_disposal_started",
    "refresh_clearance",
    "require_disposal_not_used_by_clearance",
    "return_clearance_item",
    "sync_clearance_item",
    "sync_clearance_items_for_asset",
    "transfer_clearance_item",
    "upload_clearance_attachment",
    "void_clearance_attachment",
]
