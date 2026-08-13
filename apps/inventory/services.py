"""Transactional Sprint 8 inventory services.

All mutations are keyword-only, lock the authoritative task rows and repeat
authorization after locking.  Inventory snapshots never expose finance data.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import deque
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.inventory.permissions import (
    INVENTORY_EXECUTION_ROLES,
    can_manage_inventory_attachment,
    require_close_inventory_task,
    require_convert_inventory_surplus,
    require_create_inventory_task,
    require_publish_inventory_task,
    require_reconcile_inventory_task,
    require_reconcile_inventory_task_scope,
    require_scan_inventory_task,
    require_scan_inventory_task_scope,
    require_view_inventory_attachment,
)
from apps.masterdata.permissions import current_company, role_names_for


FORMAL_INVENTORY_STATUSES = frozenset(
    {"pending_label", "in_use", "idle", "loaned", "under_repair", "pending_disposal"}
)
OPERATION_AUDIT_PREFIX = "inventory.idempotency"


def _required(value, field_name, message=None):
    result = str(value or "").strip()
    if not result:
        raise ValidationError({field_name: message or "不能为空。"})
    return result


def _serializable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if hasattr(value, "pk"):
        return str(value.pk)
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serializable(item) for item in value]
    return value


def _request_hash(payload):
    return hashlib.sha256(
        json.dumps(
            _serializable(payload), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _enable_capability(name):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, true)", [f"eam_lite.{name}", "on"])


def _base_update(model, pk, values, capability):
    _enable_capability(capability)
    if QuerySet.update(model._base_manager.filter(pk=pk), **values) != 1:
        raise ValidationError("受控盘点更新未命中唯一记录。")


def _save_new(instance, capability=None):
    if capability:
        _enable_capability(capability)
    instance.full_clean()
    try:
        instance.save(force_insert=True)
    except IntegrityError as exc:
        raise ValidationError("保存失败：幂等键或业务唯一性冲突。") from exc
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


def _operation_marker(*, company, operation, key):
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
    *, actor, operation, result, key, digest, request=None
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
        },
        **request_audit_context(request),
    )


def _operation_key(value, *, operation, result_id):
    """Keep legacy callers idempotent while preferring an explicit UI key."""

    key = str(value or f"implicit:{operation}:{result_id}").strip()
    if len(key) > 128:
        raise ValidationError({"idempotency_key": "幂等键不得超过 128 个字符。"})
    return key


def _current_company(company=None):
    active = current_company()
    if active is None or not active.is_active:
        raise PermissionDenied("当前没有启用公司。")
    if company is not None and getattr(company, "pk", None) != active.pk:
        raise PermissionDenied("盘点对象不属于当前公司。")
    return active


def _lock_task(task):
    from apps.inventory.models import InventoryTask
    from apps.masterdata.models import Company

    company = _current_company()
    try:
        company_id = InventoryTask.objects.values_list("company_id", flat=True).get(
            pk=getattr(task, "pk", task)
        )
        if company_id != company.pk:
            raise InventoryTask.DoesNotExist
        # Formalization and lifecycle services lock the Company as their first
        # serialization point.  Sharing it gives snapshot_at a real boundary.
        Company.objects.select_for_update().get(pk=company.pk)
        return InventoryTask.objects.select_for_update(of=("self",)).select_related(
            "company", "scope_department", "scope_location", "scope_category"
        ).get(pk=getattr(task, "pk", task), company=company)
    except (InventoryTask.DoesNotExist, TypeError, ValueError) as exc:
        raise PermissionDenied("盘点任务不存在或不属于当前公司。") from exc


def _descendant_ids(model, root):
    children = {}
    for pk, parent_id in model.objects.filter(company=root.company).values_list("pk", "parent_id"):
        children.setdefault(parent_id, []).append(pk)
    result, queue = {root.pk}, deque(children.get(root.pk, ()))
    while queue:
        pk = queue.popleft()
        if pk in result:
            continue
        result.add(pk)
        queue.extend(children.get(pk, ()))
    return result


def _location_path(location):
    names, current, seen = [], location, set()
    while current is not None:
        if current.pk in seen:
            raise ValidationError("位置树存在循环，不能生成快照。")
        seen.add(current.pk)
        names.append(current.name)
        current = current.parent
    return " / ".join(reversed(names))


def _validate_assignees(company, users):
    from apps.masterdata.permissions import is_login_capable

    result, seen = [], set()
    for user in users or ():
        if not getattr(user, "pk", None) or user.pk in seen:
            if getattr(user, "pk", None) in seen:
                continue
            raise ValidationError({"assignees": "执行人必须是有效用户。"})
        seen.add(user.pk)
        if not is_login_capable(user):
            raise ValidationError({"assignees": f"执行人 {user} 未启用或不能登录。"})
        if not role_names_for(user).intersection(INVENTORY_EXECUTION_ROLES):
            raise ValidationError({"assignees": f"执行人 {user} 缺少允许的盘点执行角色。"})
        result.append(user)
    if not result:
        raise ValidationError({"assignees": "盘点任务至少需要一名执行人。"})
    return result


def _scope_definition(data):
    selected = data.get("selected_asset_ids") or data.get("selected_assets") or ()
    return {
        "scope_type": data.get("scope_type"),
        "selected_asset_ids": sorted(str(getattr(item, "pk", item)) for item in selected),
    }


def _validate_type_scope(inventory_type, scope_type, scope_department):
    if inventory_type == "full" and scope_type != "company":
        raise ValidationError({"scope_type": "财务全盘必须使用全公司范围。"})
    if inventory_type == "department" and (
        scope_type != "department" or scope_department is None
    ):
        raise ValidationError({"scope_type": "部门盘点必须指定一个部门范围。"})


def _validate_scope_objects(company, data):
    """Reject inactive or cross-company scope roots before model persistence."""

    matrix = {
        "department": ("scope_department", "部门"),
        "category": ("scope_category", "实物分类"),
        "location": ("scope_location", "位置"),
    }
    scope_type = data.get("scope_type")
    required_name = matrix.get(scope_type, (None, None))[0]
    for kind, (name, label) in matrix.items():
        value = data.get(name)
        if value is None:
            if kind == scope_type:
                raise ValidationError({name: f"{label}范围必须选择具体对象。"})
            continue
        if kind != scope_type:
            raise ValidationError({name: "当前范围类型不得提交此字段。"})
        if value.company_id != company.pk:
            raise PermissionDenied(f"范围{label}不属于当前公司。")
        if not value.is_active:
            raise ValidationError({name: f"范围{label}已停用。"})
    selected = data.get("selected_asset_ids") or data.get("selected_assets") or ()
    if scope_type == "selected_assets" and not selected:
        raise ValidationError({"selected_asset_ids": "必须至少选择一项资产。"})
    if scope_type != "selected_assets" and selected:
        raise ValidationError({"selected_asset_ids": "当前范围类型不得提交已选资产。"})
    if scope_type == "company" and required_name is None:
        return


def _task_create_matches(task, data, assignees):
    expected = {
        "task_code": str(data.get("task_code") or "").strip(),
        "name": str(data.get("name") or "").strip(),
        "inventory_type": data.get("inventory_type"),
        "scope_type": data.get("scope_type"),
        "scope_department_id": getattr(data.get("scope_department"), "pk", None),
        "scope_location_id": getattr(data.get("scope_location"), "pk", None),
        "scope_category_id": getattr(data.get("scope_category"), "pk", None),
        "scope_definition_json": _scope_definition(data),
        "planned_start": data.get("planned_start"),
        "planned_end": data.get("planned_end"),
        "remark": str(data.get("remark") or "").strip(),
    }
    if any(getattr(task, name) != value for name, value in expected.items()):
        return False
    existing_ids = set(task.assignees.values_list("user_id", flat=True))
    return existing_ids == {user.pk for user in assignees}


@transaction.atomic
def create_inventory_task_draft(
    *, actor, company, data, assignee_users, request=None
):
    from apps.inventory.models import InventoryTask, InventoryTaskAssignee
    from apps.masterdata.models import Company

    company = _current_company(company)
    Company.objects.select_for_update().get(pk=company.pk)
    inventory_type = data.get("inventory_type")
    scope_department = data.get("scope_department")
    _validate_type_scope(inventory_type, data.get("scope_type"), scope_department)
    _validate_scope_objects(company, data)
    require_create_inventory_task(
        actor, company, inventory_type, scope_department=scope_department
    )
    key = _required(data.get("idempotency_key"), "idempotency_key", "创建幂等键必填。")
    assignees = _validate_assignees(company, assignee_users)
    existing = InventoryTask.objects.filter(company=company, idempotency_key=key).first()
    if existing is not None:
        if not _task_create_matches(existing, data, assignees):
            raise ValidationError("相同创建幂等键已用于不同盘点任务。")
        return existing
    task = InventoryTask(
        company=company,
        task_code=_required(data.get("task_code"), "task_code", "任务编号必填。"),
        name=_required(data.get("name"), "name", "任务名称必填。"),
        inventory_type=inventory_type,
        scope_type=data.get("scope_type"),
        scope_department=scope_department,
        scope_location=data.get("scope_location"),
        scope_category=data.get("scope_category"),
        scope_definition_json=_scope_definition(data),
        planned_start=data.get("planned_start"),
        planned_end=data.get("planned_end"),
        remark=str(data.get("remark") or "").strip(),
        status=InventoryTask.Status.DRAFT,
        idempotency_key=key,
        created_by=actor,
    )
    _save_new(task)
    for user in assignees:
        _save_new(InventoryTaskAssignee(
            company=company, inventory_task=task, user=user, assigned_by=actor
        ))
    _audit(
        actor=actor, action="inventory.task_created", instance=task,
        new={
            "inventory_type": task.inventory_type, "scope_type": task.scope_type,
            "assignee_ids": [str(user.pk) for user in assignees],
        }, request=request,
    )
    return task


@transaction.atomic
def update_inventory_task_draft(
    *, actor, task, data, assignee_users, request=None
):
    from apps.inventory.models import InventoryTask, InventoryTaskAssignee

    task = _lock_task(task)
    if task.status != "draft":
        raise ValidationError("只有草稿盘点任务可编辑。")
    require_create_inventory_task(
        actor, task.company, data.get("inventory_type"),
        scope_department=data.get("scope_department"),
    )
    _validate_type_scope(
        data.get("inventory_type"), data.get("scope_type"), data.get("scope_department")
    )
    _validate_scope_objects(task.company, data)
    assignees = _validate_assignees(task.company, assignee_users)
    old = {"name": task.name, "inventory_type": task.inventory_type, "scope_type": task.scope_type}
    values = {
        "name": _required(data.get("name"), "name"),
        "inventory_type": data.get("inventory_type"), "scope_type": data.get("scope_type"),
        "scope_department_id": getattr(data.get("scope_department"), "pk", None),
        "scope_location_id": getattr(data.get("scope_location"), "pk", None),
        "scope_category_id": getattr(data.get("scope_category"), "pk", None),
        "scope_definition_json": _scope_definition(data),
        "planned_start": data.get("planned_start"), "planned_end": data.get("planned_end"),
        "remark": str(data.get("remark") or "").strip(),
    }
    _base_update(InventoryTask, task.pk, values, "controlled_inventory_task_mutation")
    # Draft-only relation deletion is explicitly allowed by the model.
    InventoryTaskAssignee.objects.filter(inventory_task=task).delete()
    for user in assignees:
        _save_new(InventoryTaskAssignee(
            company=task.company, inventory_task=task, user=user, assigned_by=actor
        ))
    task.refresh_from_db()
    _audit(actor=actor, action="inventory.task_updated", instance=task, old=old, new=values, request=request)
    return task


def _scope_assets(task, snapshot_at):
    from apps.assets.models import Asset
    from apps.assets.permissions import scoped_assets
    from apps.masterdata.models import AssetCategory, Location

    queryset = Asset.objects.select_related(
        "category", "department", "responsible_employee", "location", "location__parent"
    ).filter(
        company=task.company, record_status="active",
        asset_status__in=FORMAL_INVENTORY_STATUSES,
        current_issued_code__isnull=False, created_at__lte=snapshot_at,
    )
    if task.scope_type == "department":
        from apps.masterdata.models import Department

        queryset = queryset.filter(
            department_id__in=_descendant_ids(Department, task.scope_department)
        )
    elif task.scope_type == "category":
        queryset = queryset.filter(category_id__in=_descendant_ids(AssetCategory, task.scope_category))
    elif task.scope_type == "location":
        queryset = queryset.filter(location_id__in=_descendant_ids(Location, task.scope_location))
    elif task.scope_type == "selected_assets":
        queryset = queryset.filter(pk__in=task.scope_definition_json.get("selected_asset_ids", ()))
    elif task.scope_type != "company":
        raise ValidationError({"scope_type": "盘点范围类型无效。"})
    # Selected assets must be individually authorized. Other creator matrices
    # are either company-global or one explicitly authorized department.
    return queryset.order_by("pk")


@transaction.atomic
def publish_inventory_task(*, actor, task, request=None):
    from apps.inventory.models import InventoryTask, InventoryTaskAsset

    task = _lock_task(task)
    # Re-authorize every retry before returning an already-published result.
    require_create_inventory_task(
        actor, task.company, task.inventory_type,
        scope_department=task.scope_department,
    )
    if task.status != "draft":
        if task.status == "in_progress" and task.snapshot_at is not None:
            assignees = _validate_assignees(
                task.company,
                [item.user for item in task.assignees.select_related("user")],
            )
            return task
        raise ValidationError("只有草稿任务可发布。")
    require_publish_inventory_task(actor, task)
    assignees = _validate_assignees(
        task.company, [item.user for item in task.assignees.select_related("user")]
    )
    snapshot_at = timezone.now()
    assets = list(_scope_assets(task, snapshot_at))
    if task.scope_type == "selected_assets":
        from apps.assets.permissions import scoped_assets

        allowed_ids = set(
            scoped_assets(actor, task.company).filter(
                pk__in=[asset.pk for asset in assets]
            ).values_list("pk", flat=True)
        )
        if allowed_ids != {asset.pk for asset in assets}:
            raise PermissionDenied("已选资产包含当前发布人无权对象。")
        requested = set(task.scope_definition_json.get("selected_asset_ids", ()))
        found = {str(asset.pk) for asset in assets}
        if requested != found:
            raise ValidationError("已选资产包含非正式、已归档、越权或已变化对象。")
    _base_update(InventoryTask, task.pk, {
        "status": "in_progress", "snapshot_at": snapshot_at,
        "expected_asset_count": len(assets),
    }, "controlled_inventory_task_mutation")
    task.refresh_from_db()
    for asset in assets:
        if not all((asset.asset_code, asset.department_id, asset.responsible_employee_id, asset.location_id)):
            raise ValidationError(f"资产 {asset.pk} 缺少正式快照必需值。")
        _save_new(InventoryTaskAsset(
            company=task.company, inventory_task=task, asset=asset,
            expected_department=asset.department,
            expected_employee=asset.responsible_employee,
            expected_location=asset.location,
            expected_asset_status=asset.asset_status,
            expected_code_snapshot=asset.asset_code,
            expected_name_snapshot=asset.asset_name,
            expected_category_snapshot=asset.category.name,
            expected_department_snapshot=asset.department.name,
            expected_employee_snapshot=asset.responsible_employee.name,
            expected_location_path_snapshot=_location_path(asset.location),
            inventory_status="pending",
        ), "controlled_inventory_task_mutation")
    _audit(
        actor=actor, action="inventory.task_published", instance=task,
        old={"status": "draft"},
        new={"status": "in_progress", "snapshot_at": snapshot_at,
             "expected_asset_count": len(assets), "assignee_count": len(assignees)},
        request=request,
    )
    return task


def _resolve_qr_identity(task, qr_identity=None, public_token=None):
    from apps.assets.models import AssetQrIdentity

    queryset = AssetQrIdentity.objects.select_related("asset")
    if qr_identity is not None:
        queryset = queryset.filter(pk=getattr(qr_identity, "pk", qr_identity))
    elif public_token:
        queryset = queryset.filter(public_token=public_token)
    else:
        raise ValidationError({"qr": "必须重新扫描当前有效二维码。"})
    identity = queryset.filter(
        company=task.company,
        status="active",
        label_status="attached",
        asset__company=task.company,
    ).first()
    if identity is None:
        raise ValidationError({"qr": "二维码无效、已撤销或不属于当前公司。"})
    return identity


def _derive_result(task_asset, *, actual_location, actual_employee, actual_status, other_mismatch, note):
    differences = [
        actual_location.pk != task_asset.expected_location_id,
        actual_employee.pk != task_asset.expected_employee_id,
        actual_status != task_asset.expected_asset_status,
    ]
    count = sum(differences)
    if other_mismatch:
        if count:
            raise ValidationError("其他异常不得覆盖已派生的位置/责任人/状态差异。")
        _required(note, "note", "其他异常必须填写说明。")
        return "other_mismatch"
    if count == 0:
        return "normal"
    if count > 1:
        return "multiple_mismatch"
    return ("location_mismatch", "responsible_mismatch", "status_mismatch")[differences.index(True)]


def _scan_matches(scan, payload):
    return all(getattr(scan, field) == value for field, value in payload.items())


def _perform_scan(
    *, actor, task, qr_identity, public_token, actual_location, actual_employee,
    actual_status, idempotency_key, note, other_mismatch, scan_mode,
    supplement_reason, task_asset=None, request=None,
):
    from apps.assets.models import Asset
    from apps.inventory.models import (
        InventoryResolution,
        InventoryScan,
        InventoryTaskAsset,
    )

    task = _lock_task(task)
    key = _required(idempotency_key, "idempotency_key", "扫码幂等键必填。")
    existing = InventoryScan.objects.filter(
        company=task.company, idempotency_key=key
    ).first()
    if scan_mode == "normal":
        if existing is None:
            require_scan_inventory_task(actor, task)
        else:
            require_scan_inventory_task_scope(actor, task)
    else:
        require_reconcile_inventory_task(actor, task)
        if task.status != "reconciliation":
            raise ValidationError("受控补盘只允许在差异处理状态执行。")
        _required(supplement_reason, "supplement_reason", "补盘原因必填。")
    identity = _resolve_qr_identity(task, qr_identity, public_token)
    if actual_location is None or actual_employee is None:
        raise ValidationError("实际位置和实际责任人必填。")
    if actual_location.company_id != task.company_id or actual_employee.company_id != task.company_id:
        raise PermissionDenied("实际位置或责任人不属于当前公司。")
    if not actual_location.is_active or actual_location.children.exists():
        raise ValidationError({"actual_location": "实际位置必须是同公司启用叶级位置。"})
    if (
        not actual_employee.is_active
        or actual_employee.employment_status != "active"
        or not actual_employee.department.is_active
    ):
        raise ValidationError({"actual_employee": "实际责任人必须是所属部门启用的在职启用员工。"})
    if actual_status not in Asset.AssetStatus.values:
        raise ValidationError({"actual_status": "实际资产状态无效。"})
    if task_asset is None:
        task_asset = InventoryTaskAsset.objects.select_for_update(of=("self",)).filter(
            inventory_task=task, asset=identity.asset
        ).first()
    else:
        task_asset = InventoryTaskAsset.objects.select_for_update(of=("self",)).filter(
            pk=getattr(task_asset, "pk", task_asset), inventory_task=task,
            asset=identity.asset,
        ).first()
    if task_asset is None:
        raise ValidationError("非本任务资产，本次扫码未计入进度。")
    if (
        scan_mode == "supplemental"
        and InventoryResolution.objects.filter(
            inventory_task_asset=task_asset,
            status="active",
        ).exists()
    ):
        raise ValidationError("已有有效处理结论的行不得再执行受控补盘。")
    result = _derive_result(
        task_asset, actual_location=actual_location, actual_employee=actual_employee,
        actual_status=actual_status, other_mismatch=other_mismatch, note=note,
    )
    payload = {
        "inventory_task_id": task.pk, "task_asset_id": task_asset.pk,
        "asset_id": identity.asset_id, "scan_mode": scan_mode,
        "supplement_reason": str(supplement_reason or "").strip(),
        "actual_location_id": actual_location.pk,
        "actual_employee_id": actual_employee.pk, "actual_status": actual_status,
        "result": result, "note": str(note or "").strip(),
    }
    if existing is not None:
        if not _scan_matches(existing, payload):
            raise ValidationError("相同扫码幂等键已用于不同请求。")
        return existing
    old = InventoryScan.objects.select_for_update().filter(
        task_asset=task_asset, is_effective=True
    ).first()
    if scan_mode == "supplemental" and old is not None:
        # Supplemental is specifically for an unresolved missing row.
        raise ValidationError("只有无有效扫码的未盘行可执行受控补盘。")
    if old is not None:
        _base_update(InventoryScan, old.pk, {"is_effective": False}, "controlled_inventory_scan_mutation")
    scan = _save_new(InventoryScan(
        company=task.company, inventory_task=task, task_asset=task_asset,
        asset=identity.asset, scan_mode=scan_mode,
        supplement_reason=payload["supplement_reason"], scanned_by=actor,
        scanned_at=timezone.now(), actual_location=actual_location,
        actual_employee=actual_employee, actual_status=actual_status,
        result=result, note=payload["note"], is_effective=True,
        supersedes_scan=old, idempotency_key=key,
    ), "controlled_inventory_scan_mutation")
    cache = "normal" if result == "normal" else "exception"
    _base_update(InventoryTaskAsset, task_asset.pk, {"inventory_status": cache}, "controlled_inventory_task_mutation")
    _audit(
        actor=actor,
        action="inventory.scan_supplemented" if scan_mode == "supplemental" else "inventory.asset_scanned",
        instance=scan,
        old={"effective_scan_id": str(old.pk) if old else None},
        new={**payload, "effective_scan_id": str(scan.pk)}, request=request,
    )
    return scan


@transaction.atomic
def scan_inventory_asset(
    *, actor, task, actual_location, actual_employee, actual_status,
    idempotency_key, qr_identity=None, public_token=None, note="",
    other_mismatch=False, request=None,
):
    return _perform_scan(
        actor=actor, task=task, qr_identity=qr_identity, public_token=public_token,
        actual_location=actual_location, actual_employee=actual_employee,
        actual_status=actual_status, idempotency_key=idempotency_key, note=note,
        other_mismatch=other_mismatch, scan_mode="normal", supplement_reason="",
        request=request,
    )


rescan_inventory_asset = scan_inventory_asset


@transaction.atomic
def stop_inventory_scanning(*, actor, task, reason, idempotency_key=None, request=None):
    from apps.inventory.models import InventoryTask, InventoryTaskAsset

    task = _lock_task(task)
    # Authorize against the immutable task scope even on a terminal retry.
    require_reconcile_inventory_task_scope(actor, task)
    explanation = _required(reason, "reason", "停止扫码必须填写原因。")
    payload = {"task_id": task.pk, "reason": explanation}
    key = _operation_key(
        idempotency_key, operation="stop", result_id=task.pk
    )
    key, digest, existing = _check_operation_idempotency(
        company=task.company, operation="stop", key=key,
        payload=payload, model=InventoryTask,
    )
    if existing is not None:
        return existing
    if task.status == "reconciliation":
        raise ValidationError("任务已停止扫码，但本次幂等键与原请求不一致。")
    if task.status != "in_progress":
        raise ValidationError("只有进行中任务可停止扫码。")
    now = timezone.now()
    _base_update(InventoryTask, task.pk, {
        "status": "reconciliation", "scanning_stopped_by_id": actor.pk,
        "scanning_stopped_at": now,
    }, "controlled_inventory_task_mutation")
    scanned_ids = task.scans.filter(is_effective=True).values_list("task_asset_id", flat=True)
    for row in InventoryTaskAsset.objects.select_for_update().filter(inventory_task=task).exclude(pk__in=scanned_ids):
        _base_update(InventoryTaskAsset, row.pk, {"inventory_status": "missing"}, "controlled_inventory_task_mutation")
    task.refresh_from_db()
    _audit(
        actor=actor, action="inventory.scanning_stopped", instance=task,
        old={"status": "in_progress"},
        new={"status": "reconciliation", "reason": explanation,
             "idempotency_key": key}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="stop", result=task, key=key,
        digest=digest, request=request,
    )
    return task


@transaction.atomic
def supplemental_scan(
    *, actor, task_asset, actual_location, actual_employee, actual_status,
    supplement_reason, idempotency_key, qr_identity=None, public_token=None,
    note="", other_mismatch=False, request=None,
):
    return _perform_scan(
        actor=actor, task=task_asset.inventory_task,
        task_asset=task_asset, qr_identity=qr_identity, public_token=public_token,
        actual_location=actual_location, actual_employee=actual_employee,
        actual_status=actual_status, idempotency_key=idempotency_key, note=note,
        other_mismatch=other_mismatch, scan_mode="supplemental",
        supplement_reason=supplement_reason, request=request,
    )


def inventory_task_summary(task):
    from apps.inventory.models import InventoryScan, InventorySurplus, InventoryTaskAsset

    rows = InventoryTaskAsset.objects.filter(inventory_task=task)
    effective = InventoryScan.objects.filter(inventory_task=task, is_effective=True)
    expected = rows.count()
    scanned = effective.count()
    normal = effective.filter(result="normal").count()
    exception = scanned - normal
    missing = expected - scanned
    surplus = InventorySurplus.objects.filter(inventory_task=task).count()
    unresolved_rows = rows.filter(
        Q(inventory_status__in=("exception", "missing"))
        & ~Q(resolutions__status="active")
    ).distinct().count()
    unresolved_surpluses = InventorySurplus.objects.filter(
        inventory_task=task, resolution_status="pending"
    ).count()
    return {
        "expected": expected, "scanned": scanned, "normal": normal,
        "exception": exception, "missing": missing, "surplus": surplus,
        "unresolved": unresolved_rows + unresolved_surpluses,
    }


def _lock_inventory_attachment_target(target):
    """Lock Company -> Task -> concrete evidence target in one order."""

    from apps.inventory.models import (
        InventoryResolution, InventoryScan, InventorySurplus,
    )

    target_id = getattr(target, "pk", target)
    if isinstance(target, InventorySurplus):
        model, task_id = InventorySurplus, InventorySurplus._base_manager.values_list(
            "inventory_task_id", flat=True
        ).get(pk=target_id)
        queryset = model._base_manager.select_for_update(of=("self",))
        lookup = {"inventory_task": _lock_task(task_id)}
    elif isinstance(target, InventoryScan):
        model, task_id = InventoryScan, InventoryScan._base_manager.values_list(
            "inventory_task_id", flat=True
        ).get(pk=target_id)
        queryset = model._base_manager.select_for_update(of=("self",))
        lookup = {"inventory_task": _lock_task(task_id)}
    elif isinstance(target, InventoryResolution):
        model, task_id = InventoryResolution, InventoryResolution._base_manager.values_list(
            "inventory_task_asset__inventory_task_id", flat=True
        ).get(pk=target_id)
        queryset = model._base_manager.select_for_update(of=("self",))
        lookup = {"inventory_task_asset__inventory_task": _lock_task(task_id)}
    else:
        raise ValidationError("盘点附件目标类型无效。")
    task = next(iter(lookup.values()))
    try:
        locked = queryset.get(pk=target_id, company=task.company, **lookup)
    except model.DoesNotExist as exc:
        raise PermissionDenied("盘点附件目标不存在或不属于当前公司。") from exc
    return locked, task


@transaction.atomic
def upload_inventory_attachment(*, actor, target, uploaded_file, request=None):
    """Validate and privately link an A0 inventory evidence attachment."""

    from apps.assets.models import AttachmentLink
    from apps.assets.services import (
        MIME_BY_EXTENSION, _detect_mime, _read_upload, _validate_filename,
    )
    from apps.inventory.models import InventorySurplus
    from apps.masterdata.models import Attachment
    from apps.masterdata.services import get_system_setting

    target, task = _lock_inventory_attachment_target(target)
    if not can_manage_inventory_attachment(actor, target):
        raise PermissionDenied("您没有上传此盘点证据的权限。")
    original_name, extension = _validate_filename(uploaded_file.name)
    allowed = set(get_system_setting(
        company=task.company, key="attachment_allowed_extensions"
    ))
    if extension not in allowed or extension not in MIME_BY_EXTENSION:
        raise ValidationError("当前公司未允许该附件扩展名。")
    limit = get_system_setting(
        company=task.company, key="attachment_max_size_bytes"
    )
    data = _read_upload(uploaded_file, limit)
    detected_mime = _detect_mime(extension, data)
    client_mime = str(getattr(uploaded_file, "content_type", "") or "").lower()
    if client_mime and client_mime != detected_mime:
        raise ValidationError("客户端 MIME 与文件实际类型不一致。")

    if isinstance(target, InventorySurplus) and not detected_mime.startswith("image/"):
        raise ValidationError("盘盈现场证据必须上传图片。")

    storage_key = (
        f"private/inventory/{task.company_id}/"
        f"{uuid.uuid4().hex}.{extension}"
    )
    saved_key = default_storage.save(storage_key, ContentFile(data))
    linked = False
    try:
        attachment = _save_new(Attachment(
            company=task.company, storage_key=saved_key,
            original_filename=original_name[:255],
            safe_filename=(get_valid_filename(original_name) or f"attachment.{extension}")[:255],
            file_size=len(data), mime_type=detected_mime,
            sha256=hashlib.sha256(data).hexdigest(), uploaded_by=actor,
            malware_scan_status=Attachment.MalwareScanStatus.POLICY_LIMITED,
            is_available=False,
        ))
        fields = {
            "inventory_surplus": None,
            "inventory_scan": None,
            "inventory_resolution": None,
        }
        role = AttachmentLink.Role.INVENTORY_EVIDENCE
        if isinstance(target, InventorySurplus):
            fields["inventory_surplus"] = target
            role = AttachmentLink.Role.SURPLUS_EVIDENCE
        elif target._meta.model_name == "inventoryscan":
            fields["inventory_scan"] = target
        else:
            fields["inventory_resolution"] = target
        link = _save_new(AttachmentLink(
            company=task.company, attachment=attachment,
            role=role, security_class=AttachmentLink.SecurityClass.A0,
            created_by=actor, **fields,
        ))
        _base_update(
            Attachment, attachment.pk, {"is_available": True},
            "controlled_asset_mutation",
        )
        attachment.is_available = True
        _audit(
            actor=actor, action="inventory.attachment_uploaded", instance=link,
            new={
                "target_type": target._meta.object_name,
                "target_id": str(target.pk), "role": role,
                "security_class": "A0", "file_size": len(data),
                "mime_type": detected_mime, "sha256": attachment.sha256,
            }, request=request,
        )
        linked = True
        return link
    finally:
        if not linked and default_storage.exists(saved_key):
            default_storage.delete(saved_key)


@transaction.atomic
def void_inventory_attachment(*, actor, link, reason, request=None):
    """Void the link only; retain private file metadata and audit evidence."""

    from apps.assets.models import AttachmentLink

    raw = AttachmentLink._base_manager.select_related(
        "inventory_surplus", "inventory_scan", "inventory_resolution"
    ).get(pk=getattr(link, "pk", link))
    target = raw.inventory_surplus or raw.inventory_scan or raw.inventory_resolution
    if target is None:
        raise ValidationError("目标不是盘点附件。")
    target, task = _lock_inventory_attachment_target(target)
    link = AttachmentLink._base_manager.select_for_update(of=("self",)).get(
        pk=raw.pk, company=task.company
    )
    if not can_manage_inventory_attachment(actor, target):
        raise PermissionDenied("您没有作废此盘点证据的权限。")
    explanation = _required(reason, "reason", "作废盘点证据必须填写原因。")
    if link.status == AttachmentLink.Status.VOIDED:
        if link.void_reason != explanation:
            raise ValidationError("该证据已使用不同原因作废。")
        return link
    _base_update(AttachmentLink, link.pk, {
        "status": AttachmentLink.Status.VOIDED,
        "void_reason": explanation, "voided_by_id": actor.pk,
        "voided_at": timezone.now(),
    }, "controlled_asset_mutation")
    link.refresh_from_db()
    _audit(
        actor=actor, action="inventory.attachment_voided", instance=link,
        old={"status": "active"},
        new={"status": "voided", "reason": explanation}, request=request,
    )
    return link


def require_inventory_attachment_download(*, actor, link):
    """Return a current link only after target scope and availability checks."""

    from apps.assets.models import AttachmentLink
    from apps.masterdata.models import Attachment

    link = AttachmentLink._base_manager.select_related(
        "attachment", "inventory_surplus__inventory_task",
        "inventory_scan__inventory_task",
        "inventory_resolution__inventory_task_asset__inventory_task",
    ).filter(
        pk=getattr(link, "pk", link), status=AttachmentLink.Status.ACTIVE,
        attachment__is_available=True,
        attachment__malware_scan_status__in=(
            Attachment.MalwareScanStatus.POLICY_LIMITED,
            Attachment.MalwareScanStatus.CLEAN,
        ),
    ).first()
    if link is None:
        raise PermissionDenied("盘点附件不存在或当前不可下载。")
    require_view_inventory_attachment(actor, link)
    return link


def _require_surplus_evidence(surplus):
    from apps.assets.models import AttachmentLink

    if not AttachmentLink._base_manager.filter(
        inventory_surplus=surplus, role=AttachmentLink.Role.SURPLUS_EVIDENCE,
        security_class=AttachmentLink.SecurityClass.A0,
        status=AttachmentLink.Status.ACTIVE,
        attachment__is_available=True,
        attachment__mime_type__startswith="image/",
        attachment__malware_scan_status__in=("policy_limited", "clean"),
    ).exists():
        raise ValidationError("盘盈必须先上传至少一张有效照片证据。")


def _validate_resolution_target(task_asset):
    from apps.inventory.models import InventoryScan

    scan = InventoryScan.objects.filter(
        task_asset=task_asset, is_effective=True
    ).first()
    if scan is None:
        return "missing"
    return scan.result


def _apply_master_correction(
    *, actor, task_asset, conclusion, idempotency_key, to_department,
    to_responsible_employee, to_location, to_status, effective_at, request,
):
    """Call Sprint 7 public services; never copy Asset current-field updates."""

    from apps.assets.lifecycle_services import (
        activate_asset,
        set_asset_idle,
        transfer_asset,
    )

    asset = task_asset.asset
    movement = None
    target_tuple = (
        to_department or asset.department,
        to_responsible_employee or asset.responsible_employee,
        to_location or asset.location,
    )
    if target_tuple != (
        asset.department, asset.responsible_employee, asset.location
    ):
        movement = transfer_asset(
            actor=actor, asset=asset, to_department=target_tuple[0],
            to_responsible_employee=target_tuple[1], to_location=target_tuple[2],
            effective_at=effective_at, reason=conclusion,
            idempotency_key=hashlib.sha256(
                f"{idempotency_key}:inventory-transfer".encode()
            ).hexdigest(),
            expected_department_id=asset.department_id,
            expected_responsible_employee_id=asset.responsible_employee_id,
            expected_location_id=asset.location_id,
            expected_status=asset.asset_status, request=request,
        )
        asset.refresh_from_db()
    if to_status and to_status != asset.asset_status:
        if to_status == "idle" and asset.asset_status == "in_use":
            status_movement = set_asset_idle(
                actor=actor, asset=asset, effective_at=effective_at,
                reason=conclusion,
                idempotency_key=hashlib.sha256(
                    f"{idempotency_key}:inventory-idle".encode()
                ).hexdigest(), request=request,
            )
        elif to_status == "in_use" and asset.asset_status == "idle":
            status_movement = activate_asset(
                actor=actor, asset=asset, effective_at=effective_at,
                reason=conclusion,
                idempotency_key=hashlib.sha256(
                    f"{idempotency_key}:inventory-activate".encode()
                ).hexdigest(), request=request,
            )
        else:
            raise ValidationError("盘点结论只能复用在用/闲置状态 Service。")
        # A Resolution can reference one Movement. The last executed mutation
        # is the clearest evidence when both tuple and status are corrected.
        movement = status_movement
    if movement is None:
        raise ValidationError("主档纠正未产生正式 AssetMovement。")
    return movement


@transaction.atomic
def resolve_inventory_difference(
    *, actor, task_asset, resolution_type, conclusion, idempotency_key,
    to_department=None, to_responsible_employee=None, to_location=None,
    to_status=None, effective_at=None, request=None,
):
    from apps.inventory.models import (
        InventoryResolution, InventoryTaskAsset,
    )

    task = _lock_task(task_asset.inventory_task)
    require_reconcile_inventory_task(actor, task)
    if task.status != "reconciliation":
        raise ValidationError("只有差异处理中任务可新增结论。")
    task_asset = InventoryTaskAsset.objects.select_for_update(of=("self",)).select_related(
        "asset", "asset__department", "asset__responsible_employee", "asset__location"
    ).get(pk=task_asset.pk, inventory_task=task)
    evidence = _validate_resolution_target(task_asset)
    if evidence == "normal":
        raise ValidationError("正常扫码已是完成证据，不得伪造处理结论。")
    if resolution_type not in {"master_updated", "master_confirmed", "loss_confirmed", "other"}:
        raise ValidationError({"resolution_type": "处理结论类型无效。"})
    explanation = _required(conclusion, "conclusion", "差异处理结论必填。")
    key = _required(idempotency_key, "idempotency_key", "结论幂等键必填。")
    payload = {
        "task_asset_id": task_asset.pk, "resolution_type": resolution_type,
        "conclusion": explanation,
        "to_department_id": getattr(to_department, "pk", None),
        "to_responsible_employee_id": getattr(
            to_responsible_employee, "pk", None
        ),
        "to_location_id": getattr(to_location, "pk", None),
        "to_status": to_status or None, "effective_at": effective_at,
    }
    digest = _request_hash(payload)
    existing = InventoryResolution.objects.filter(
        company=task.company, idempotency_key=key
    ).first()
    if existing is not None:
        marker = _operation_marker(
            company=task.company, operation="difference_resolve", key=key
        )
        if (
            existing.inventory_task_asset_id != task_asset.pk
            or existing.resolution_type != resolution_type
            or existing.conclusion != explanation
            or marker is None
            or marker.new_data_json.get("request_hash") != digest
        ):
            raise ValidationError("相同结论幂等键已用于不同请求。")
        return existing
    if InventoryResolution.objects.filter(
        inventory_task_asset=task_asset, status="active"
    ).exists():
        raise ValidationError("此快照行已有当前有效结论。")
    movement = None
    if resolution_type == "master_updated":
        if effective_at is None:
            raise ValidationError({"effective_at": "主档纠正必须填写生效时间。"})
        movement = _apply_master_correction(
            actor=actor, task_asset=task_asset, conclusion=explanation,
            idempotency_key=key, to_department=to_department,
            to_responsible_employee=to_responsible_employee,
            to_location=to_location, to_status=to_status,
            effective_at=effective_at, request=request,
        )
    resolution = _save_new(InventoryResolution(
        company=task.company, inventory_task_asset=task_asset,
        resolution_type=resolution_type, conclusion=explanation,
        movement=movement, status="active", idempotency_key=key,
        resolved_by=actor, resolved_at=timezone.now(),
    ), "controlled_inventory_resolution_mutation")
    _base_update(InventoryTaskAsset, task_asset.pk, {
        "inventory_status": "resolved"
    }, "controlled_inventory_task_mutation")
    _audit(
        actor=actor, action="inventory.difference_resolved", instance=resolution,
        new={"evidence": evidence, "resolution_type": resolution_type,
             "movement_id": str(movement.pk) if movement else None}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="difference_resolve", result=resolution,
        key=key, digest=digest, request=request,
    )
    return resolution


@transaction.atomic
def correct_inventory_resolution(
    *, actor, resolution, resolution_type, conclusion, correction_reason,
    idempotency_key, to_department=None, to_responsible_employee=None,
    to_location=None, to_status=None, effective_at=None, request=None,
):
    from apps.inventory.models import InventoryResolution

    resolution_id = getattr(resolution, "pk", resolution)
    task_id = InventoryResolution._base_manager.values_list(
        "inventory_task_asset__inventory_task_id", flat=True
    ).get(pk=resolution_id)
    task = _lock_task(task_id)
    old = InventoryResolution._base_manager.select_for_update(
        of=("self",)
    ).select_related(
        "inventory_task_asset__inventory_task",
        "inventory_task_asset__asset__department",
        "inventory_task_asset__asset__responsible_employee",
        "inventory_task_asset__asset__location",
    ).get(pk=resolution_id, company=task.company)
    require_close_inventory_task(actor, task)
    reason = _required(correction_reason, "correction_reason", "关闭后更正必须填写原因。")
    explanation = _required(conclusion, "conclusion")
    key = _required(idempotency_key, "idempotency_key")
    payload = {
        "supersedes_resolution_id": old.pk,
        "resolution_type": resolution_type, "conclusion": explanation,
        "correction_reason": reason,
        "to_department_id": getattr(to_department, "pk", None),
        "to_responsible_employee_id": getattr(
            to_responsible_employee, "pk", None
        ),
        "to_location_id": getattr(to_location, "pk", None),
        "to_status": to_status or None, "effective_at": effective_at,
    }
    digest = _request_hash(payload)
    existing = InventoryResolution.objects.filter(company=task.company, idempotency_key=key).first()
    if existing is not None:
        marker = _operation_marker(
            company=task.company, operation="resolution_correct", key=key
        )
        if (
            existing.supersedes_resolution_id != old.pk
            or existing.resolution_type != resolution_type
            or existing.conclusion != explanation
            or existing.correction_reason != reason
            or marker is None
            or marker.new_data_json.get("request_hash") != digest
        ):
            raise ValidationError("相同更正幂等键已用于不同结论。")
        return existing
    if task.status != "closed" or old.status != "active":
        raise ValidationError("只能对已关闭任务的当前结论新增更正。")
    movement = None
    if resolution_type == "master_updated":
        if effective_at is None:
            raise ValidationError({"effective_at": "主档纠正必须填写生效时间。"})
        movement = _apply_master_correction(
            actor=actor, task_asset=old.inventory_task_asset,
            conclusion=explanation, idempotency_key=key,
            to_department=to_department,
            to_responsible_employee=to_responsible_employee,
            to_location=to_location, to_status=to_status,
            effective_at=effective_at, request=request,
        )
    _base_update(InventoryResolution, old.pk, {"status": "superseded"}, "controlled_inventory_resolution_mutation")
    corrected = _save_new(InventoryResolution(
        company=task.company, inventory_task_asset=old.inventory_task_asset,
        resolution_type=resolution_type, conclusion=explanation,
        movement=movement, status="active", supersedes_resolution=old,
        correction_reason=reason, idempotency_key=key, resolved_by=actor,
        resolved_at=timezone.now(),
    ), "controlled_inventory_resolution_mutation")
    _audit(
        actor=actor, action="inventory.resolution_corrected", instance=corrected,
        old={"resolution_id": str(old.pk), "resolution_type": old.resolution_type},
        new={"resolution_type": resolution_type, "correction_reason": reason,
             "movement_id": str(movement.pk) if movement else None}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="resolution_correct", result=corrected,
        key=key, digest=digest, request=request,
    )
    return corrected


@transaction.atomic
def create_inventory_surplus(
    *, actor, task, temporary_name, temporary_category_text,
    temporary_location_text, idempotency_key, remark="", request=None,
):
    from apps.inventory.models import InventorySurplus

    task = _lock_task(task)
    require_scan_inventory_task(actor, task)
    key = _required(idempotency_key, "idempotency_key")
    requested = {
        "inventory_task_id": task.pk,
        "temporary_name": _required(temporary_name, "temporary_name"),
        "temporary_category_text": str(temporary_category_text or "").strip(),
        "temporary_location_text": _required(
            temporary_location_text, "temporary_location_text"
        ),
        # Pending surplus rows intentionally keep remark empty.  The initial
        # note is still part of idempotency and the append-only audit evidence.
        "initial_note": str(remark or "").strip(),
    }
    existing = InventorySurplus.objects.filter(company=task.company, idempotency_key=key).first()
    if existing is not None:
        audit = _operation_marker(
            company=task.company, operation="surplus_create", key=key
        )
        expected_hash = _request_hash(requested)
        if audit is not None:
            matches = audit.new_data_json.get("request_hash") == expected_hash
        else:
            matches = (
                existing.inventory_task_id == task.pk
                and existing.temporary_name == requested["temporary_name"]
                and existing.temporary_category_text
                == requested["temporary_category_text"]
                and existing.temporary_location_text
                == requested["temporary_location_text"]
            )
        if not matches:
            raise ValidationError("相同盘盈幂等键已用于不同请求。")
        return existing
    surplus = _save_new(InventorySurplus(
        company=task.company, inventory_task=task,
        temporary_name=requested["temporary_name"],
        temporary_category_text=requested["temporary_category_text"],
        temporary_location_text=requested["temporary_location_text"],
        found_by=actor, found_at=timezone.now(), resolution_status="pending",
        remark="", idempotency_key=key,
    ), "controlled_inventory_surplus_mutation")
    _audit(
        actor=actor, action="inventory.surplus_created", instance=surplus,
        new={"temporary_name": surplus.temporary_name,
             "temporary_location_text": surplus.temporary_location_text,
             "initial_note": requested["initial_note"]}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="surplus_create", result=surplus, key=key,
        digest=_request_hash(requested), request=request,
    )
    return surplus


@transaction.atomic
def resolve_inventory_surplus(
    *, actor, surplus, resolution_status, remark, idempotency_key=None,
    request=None,
):
    from apps.inventory.models import InventorySurplus

    surplus_id = getattr(surplus, "pk", surplus)
    task_id = InventorySurplus._base_manager.values_list(
        "inventory_task_id", flat=True
    ).get(pk=surplus_id)
    task = _lock_task(task_id)
    surplus = InventorySurplus._base_manager.select_for_update(
        of=("self",)
    ).get(pk=surplus_id, company=task.company, inventory_task=task)
    require_convert_inventory_surplus(actor, surplus)
    if resolution_status not in {"not_company", "duplicate", "other"}:
        raise ValidationError({"resolution_status": "盘盈处理类型无效。"})
    explanation = _required(remark, "remark", "盘盈处理必须填写说明。")
    payload = {
        "surplus_id": surplus.pk, "resolution_status": resolution_status,
        "remark": explanation,
    }
    key = _operation_key(
        idempotency_key, operation="surplus_resolve", result_id=surplus.pk
    )
    key, digest, existing = _check_operation_idempotency(
        company=task.company, operation="surplus_resolve", key=key,
        payload=payload, model=InventorySurplus,
    )
    if existing is not None:
        return existing
    if task.status != "reconciliation":
        raise ValidationError("盘盈结论只能在差异处理阶段确认。")
    if surplus.resolution_status != "pending":
        raise ValidationError("此盘盈已有不同处理请求或缺少耐久幂等结果。")
    _require_surplus_evidence(surplus)
    _base_update(InventorySurplus, surplus.pk, {
        "resolution_status": resolution_status, "resolved_by_id": actor.pk,
        "resolved_at": timezone.now(), "remark": explanation,
    }, "controlled_inventory_surplus_mutation")
    surplus.refresh_from_db()
    _audit(
        actor=actor, action="inventory.surplus_resolved", instance=surplus,
        old={"resolution_status": "pending"},
        new={"resolution_status": resolution_status, "remark": explanation}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="surplus_resolve", result=surplus, key=key,
        digest=digest, request=request,
    )
    return surplus


@transaction.atomic
def convert_surplus_to_asset_draft(
    *, actor, surplus, asset_data, idempotency_key=None, custom_values=None,
    remark, request=None,
):
    from apps.assets.services import create_asset_draft
    from apps.inventory.models import InventorySurplus

    surplus_id = getattr(surplus, "pk", surplus)
    task_id = InventorySurplus._base_manager.values_list(
        "inventory_task_id", flat=True
    ).get(pk=surplus_id)
    task = _lock_task(task_id)
    surplus = InventorySurplus._base_manager.select_for_update(
        of=("self",)
    ).select_related("linked_asset").get(
        pk=surplus_id, company=task.company, inventory_task=task
    )
    require_convert_inventory_surplus(actor, surplus)
    explanation = _required(remark, "remark", "盘盈转草稿必须填写说明。")
    payload = {
        "surplus_id": surplus.pk, "asset_data": asset_data,
        "custom_values": custom_values or {}, "remark": explanation,
        "initialization_source": "manual",
    }
    key = _operation_key(
        idempotency_key, operation="surplus_convert", result_id=surplus.pk
    )
    from apps.assets.models import Asset

    key, digest, existing = _check_operation_idempotency(
        company=task.company, operation="surplus_convert", key=key,
        payload=payload, model=Asset,
    )
    if existing is not None:
        return existing
    if task.status != "reconciliation":
        raise ValidationError("只有差异处理中的盘盈可转资产草稿。")
    if surplus.resolution_status != "pending":
        raise ValidationError("此盘盈已有其他处理结论或缺少耐久幂等结果。")
    _require_surplus_evidence(surplus)
    # This service deliberately leaves asset_code/current_issued_code empty.
    asset = create_asset_draft(
        actor=actor, company=task.company, data=asset_data,
        custom_values=custom_values, initialization_source="manual", request=request,
    )
    _base_update(InventorySurplus, surplus.pk, {
        "resolution_status": "converted_to_draft", "linked_asset_id": asset.pk,
        "resolved_by_id": actor.pk, "resolved_at": timezone.now(),
        "remark": explanation,
    }, "controlled_inventory_surplus_mutation")
    surplus.refresh_from_db()
    _audit(
        actor=actor, action="inventory.surplus_converted", instance=surplus,
        old={"resolution_status": "pending", "linked_asset_id": None},
        new={"resolution_status": "converted_to_draft",
             "linked_asset_id": str(asset.pk), "asset_code": None,
             "idempotency_key": key}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="surplus_convert", result=asset, key=key,
        digest=digest, request=request,
    )
    return asset


@transaction.atomic
def cancel_inventory_task(*, actor, task, reason, idempotency_key=None, request=None):
    from apps.inventory.models import InventoryTask

    task = _lock_task(task)
    require_close_inventory_task(actor, task)
    explanation = _required(reason, "reason", "取消盘点任务必须填写原因。")
    payload = {"task_id": task.pk, "reason": explanation}
    key = _operation_key(
        idempotency_key, operation="cancel", result_id=task.pk
    )
    key, digest, existing = _check_operation_idempotency(
        company=task.company, operation="cancel", key=key,
        payload=payload, model=InventoryTask,
    )
    if existing is not None:
        return existing
    if task.status == "cancelled":
        raise ValidationError("任务已取消，但本次幂等键或请求参数不同。")
    if task.status not in {"draft", "in_progress", "reconciliation"}:
        raise ValidationError("当前任务状态不能取消。")
    old_status = task.status
    _base_update(InventoryTask, task.pk, {
        "status": "cancelled", "cancelled_by_id": actor.pk,
        "cancelled_at": timezone.now(), "cancellation_reason": explanation,
    }, "controlled_inventory_task_mutation")
    task.refresh_from_db()
    _audit(
        actor=actor, action="inventory.task_cancelled", instance=task,
        old={"status": old_status}, new={"status": "cancelled", "reason": explanation,
          "idempotency_key": key}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="cancel", result=task, key=key,
        digest=digest, request=request,
    )
    return task


@transaction.atomic
def close_inventory_task(*, actor, task, idempotency_key=None, request=None):
    from apps.inventory.models import InventoryTask

    task = _lock_task(task)
    require_close_inventory_task(actor, task)
    payload = {"task_id": task.pk}
    key = _operation_key(
        idempotency_key, operation="close", result_id=task.pk
    )
    key, digest, existing = _check_operation_idempotency(
        company=task.company, operation="close", key=key,
        payload=payload, model=InventoryTask,
    )
    if existing is not None:
        return existing
    if task.status == "closed":
        raise ValidationError("任务已关闭，但本次幂等键与原请求不一致。")
    if task.status != "reconciliation":
        raise ValidationError("任务必须先停止扫码并进入差异处理。")
    summary = inventory_task_summary(task)
    if summary["unresolved"]:
        raise ValidationError(f"仍有 {summary['unresolved']} 项差异、未盘或盘盈未形成结论。")
    for surplus in task.surpluses.all():
        _require_surplus_evidence(surplus)
    _base_update(InventoryTask, task.pk, {
        "status": "closed", "closed_by_id": actor.pk, "closed_at": timezone.now(),
    }, "controlled_inventory_task_mutation")
    task.refresh_from_db()
    _audit(
        actor=actor, action="inventory.task_closed", instance=task,
        old={"status": "reconciliation"},
        new={"status": "closed", **summary,
             "idempotency_key": key}, request=request,
    )
    _write_operation_marker(
        actor=actor, operation="close", result=task, key=key,
        digest=digest, request=request,
    )
    return task
