"""Transactional services for preventive maintenance."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection, transaction
from django.db.models import Case, When
from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.maintenance.domain import add_calendar_cycle, business_date, due_status
from apps.maintenance.permissions import (
    can_manage_maintenance_attachment,
    require_close_maintenance_problem,
    require_complete_maintenance,
    require_manage_maintenance_plan,
    require_view_maintenance_attachment,
    require_void_maintenance_record,
)
from apps.masterdata.permissions import current_company


OPERATION_AUDIT_PREFIX = "maintenance.idempotency"
PLAN_ASSET_STATUSES = frozenset(
    {"in_use", "idle", "loaned", "under_repair"}
)


def _required(value, field_name, message=None):
    result = str(value or "").strip()
    if not result:
        raise ValidationError({field_name: message or "不能为空。"})
    return result


def _serializable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
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
            _serializable(payload),
            ensure_ascii=False,
            sort_keys=True,
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
        raise ValidationError("受控保养更新未命中唯一记录。")


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
    result = model._base_manager.filter(
        pk=marker.new_data_json.get("result_id"), company=company
    ).first()
    if result is None:
        raise ValidationError("幂等结果记录不完整，请停止并复核。")
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
            "payload": _serializable(payload),
        },
        **request_audit_context(request),
    )


def _current_company_for(company_id):
    from apps.masterdata.models import Company

    selected = current_company()
    if selected is None or selected.pk != company_id:
        raise PermissionDenied("对象不属于当前公司。")
    return Company.objects.select_for_update().get(pk=company_id)


def _company_id_for_plan(plan):
    from apps.maintenance.models import MaintenancePlan

    try:
        return MaintenancePlan._base_manager.values_list(
            "company_id", flat=True
        ).get(pk=getattr(plan, "pk", plan))
    except (MaintenancePlan.DoesNotExist, TypeError, ValueError) as exc:
        raise PermissionDenied("保养计划不存在。") from exc


def _lock_plan(plan):
    from apps.maintenance.models import MaintenancePlan

    company = _current_company_for(_company_id_for_plan(plan))
    queryset = MaintenancePlan._base_manager.select_for_update()
    if connection.vendor == "postgresql":
        queryset = queryset.select_for_update(of=("self",))
    return queryset.select_related(
        "company", "asset__department", "responsible_employee__user"
    ).get(pk=getattr(plan, "pk", plan), company=company)


def _completion_employee(actor, company):
    from apps.masterdata.models import Employee

    employee = Employee.objects.filter(company=company, user=actor).first()
    if employee is None:
        raise ValidationError("当前用户未绑定本公司的员工档案。")
    if employee.employment_status != "active" or not employee.is_active:
        raise ValidationError("当前用户绑定的员工档案不是在职启用状态。")
    return employee


def _validate_plan_inputs(*, company, asset, responsible_employee):
    if asset.company_id != company.pk or responsible_employee.company_id != company.pk:
        raise ValidationError("资产、责任人与计划必须属于同一公司。")
    if not asset.is_maintenance_required:
        raise ValidationError({"asset": "该资产未标记为需要保养。"})
    if asset.record_status != "active" or asset.asset_status not in PLAN_ASSET_STATUSES:
        raise ValidationError({"asset": "当前资产状态不能建立或启用保养计划。"})
    if (
        responsible_employee.employment_status != "active"
        or not responsible_employee.is_active
        or not responsible_employee.department.is_active
    ):
        raise ValidationError({"responsible_employee": "责任人必须为启用部门的在职启用员工。"})


def _latest_valid_record(plan):
    return plan.records.filter(status="confirmed").order_by(
        "-completed_date", "-created_at", "-pk"
    ).first()


def _recalculate_plan_dates(plan):
    latest = _latest_valid_record(plan)
    last_date = latest.completed_date if latest else None
    next_date = (
        add_calendar_cycle(last_date, plan.cycle_value, plan.cycle_unit)
        if last_date
        else plan.first_due_date
    )
    _base_update(
        type(plan),
        plan.pk,
        {"last_maintenance_date": last_date, "next_maintenance_date": next_date},
        "controlled_maintenance_plan_mutation",
    )
    plan.last_maintenance_date, plan.next_maintenance_date = last_date, next_date
    return plan


def due_maintenance_plans(user, company, as_of=None, queryset=None):
    from apps.maintenance.permissions import scoped_maintenance_plans

    plans = scoped_maintenance_plans(user, company, queryset).filter(status="active")
    today = business_date(as_of)
    return [(plan, due_status(plan, today)) for plan in plans]


@transaction.atomic
def create_maintenance_plan(
    *, actor, company, asset, name, cycle_value, cycle_unit,
    responsible_employee, advance_notice_days, standard_content,
    first_due_date, request=None,
):
    from apps.assets.models import Asset
    from apps.maintenance.models import MaintenancePlan

    company = _current_company_for(company.pk)
    asset = Asset.objects.select_for_update(of=("self",)).select_related("department").get(
        pk=asset.pk, company=company
    )
    require_manage_maintenance_plan(actor, asset)
    responsible_employee.refresh_from_db()
    _validate_plan_inputs(
        company=company, asset=asset, responsible_employee=responsible_employee
    )
    plan = MaintenancePlan(
        company=company,
        asset=asset,
        name=_required(name, "name"),
        cycle_value=cycle_value,
        cycle_unit=cycle_unit,
        responsible_employee=responsible_employee,
        advance_notice_days=advance_notice_days,
        standard_content=_required(standard_content, "standard_content"),
        first_due_date=business_date(first_due_date),
        next_maintenance_date=business_date(first_due_date),
        status="active",
    )
    _save_new(plan, "controlled_maintenance_plan_insert")
    _audit(
        actor=actor,
        action="maintenance.plan_created",
        instance=plan,
        new={
            "asset_id": str(asset.pk),
            "cycle_value": cycle_value,
            "cycle_unit": cycle_unit,
            "responsible_employee_id": str(responsible_employee.pk),
            "first_due_date": plan.first_due_date.isoformat(),
        },
        request=request,
    )
    return plan


@transaction.atomic
def update_maintenance_plan(
    *, actor, plan, name, cycle_value, cycle_unit, responsible_employee,
    advance_notice_days, standard_content, first_due_date, request=None,
    asset=None,
):
    plan = _lock_plan(plan)
    require_manage_maintenance_plan(actor, plan)
    if asset is not None and asset.pk != plan.asset_id:
        raise ValidationError({"asset": "已建立计划不得更换资产。"})
    if plan.status == "ended":
        raise ValidationError("已终止计划不能普通修改。")
    responsible_employee.refresh_from_db()
    _validate_plan_inputs(
        company=plan.company, asset=plan.asset,
        responsible_employee=responsible_employee,
    )
    old = {
        "name": plan.name,
        "cycle_value": plan.cycle_value,
        "cycle_unit": plan.cycle_unit,
        "responsible_employee_id": str(plan.responsible_employee_id),
        "advance_notice_days": plan.advance_notice_days,
        "standard_content": plan.standard_content,
        "first_due_date": plan.first_due_date.isoformat(),
    }
    values = {
        "name": _required(name, "name"),
        "cycle_value": cycle_value,
        "cycle_unit": cycle_unit,
        "responsible_employee_id": responsible_employee.pk,
        "advance_notice_days": advance_notice_days,
        "standard_content": _required(standard_content, "standard_content"),
        "first_due_date": business_date(first_due_date),
    }
    latest = _latest_valid_record(plan)
    values["last_maintenance_date"] = latest.completed_date if latest else None
    values["next_maintenance_date"] = (
        add_calendar_cycle(latest.completed_date, cycle_value, cycle_unit)
        if latest else values["first_due_date"]
    )
    _base_update(
        type(plan), plan.pk, values, "controlled_maintenance_plan_mutation"
    )
    plan.refresh_from_db()
    _audit(
        actor=actor, action="maintenance.plan_updated", instance=plan,
        old=old, new=_serializable(values), request=request,
    )
    return plan


@transaction.atomic
def set_maintenance_plan_status(
    *, actor, plan, status, reason="", request=None
):
    plan = _lock_plan(plan)
    require_manage_maintenance_plan(actor, plan)
    if status not in {"active", "suspended", "ended"}:
        raise ValidationError({"status": "状态只允许启用、暂停或终止。"})
    if plan.status == "ended":
        raise ValidationError("已终止计划不能通过普通状态操作恢复。")
    if status == plan.status:
        return plan
    if status == "active":
        _validate_plan_inputs(
            company=plan.company,
            asset=plan.asset,
            responsible_employee=plan.responsible_employee,
        )
    values = {
        "status": status,
        "ended_reason": None,
        "ended_by_disposal_id": None,
        "status_before_disposal": None,
        "ended_at": None,
    }
    if status == "ended":
        values.update(
            ended_reason="manual",
            ended_at=timezone.now(),
        )
        _required(reason, "reason", "终止计划必须填写原因。")
    old_status = plan.status
    _base_update(
        type(plan), plan.pk, values, "controlled_maintenance_plan_mutation"
    )
    plan.refresh_from_db()
    _audit(
        actor=actor,
        action="maintenance.plan_status_changed",
        instance=plan,
        old={"status": old_status},
        new={"status": status, "reason": str(reason).strip()},
        request=request,
    )
    return plan


@transaction.atomic
def complete_maintenance(
    *, actor, plan, scheduled_date, completed_date, actual_content, result,
    problem_description="", remark="", idempotency_key, request=None,
):
    from apps.maintenance.models import MaintenancePlan, MaintenanceProblem, MaintenanceRecord

    plan = _lock_plan(plan)
    require_complete_maintenance(actor, plan)
    scheduled = business_date(scheduled_date)
    completed = business_date(completed_date)
    content = _required(actual_content, "actual_content")
    problem_text = str(problem_description or "").strip()
    if result not in {"normal", "problem_found"}:
        raise ValidationError({"result": "结果只允许正常或发现问题。"})
    if result == "problem_found" and not problem_text:
        raise ValidationError({"problem_description": "发现问题时必须填写问题说明。"})
    if result == "normal" and problem_text:
        raise ValidationError({"problem_description": "正常结果不得提交问题说明。"})
    payload = {
        "plan_id": plan.pk,
        "scheduled_date": scheduled,
        "completed_date": completed,
        "actual_content": content,
        "result": result,
        "problem_description": problem_text,
        "remark": str(remark or "").strip(),
    }
    key, digest, existing = _check_operation_idempotency(
        company=plan.company,
        operation="complete",
        key=idempotency_key,
        payload=payload,
        model=MaintenanceRecord,
    )
    if existing is not None:
        return existing
    if MaintenanceRecord._base_manager.select_for_update().filter(
        maintenance_plan=plan, scheduled_date=scheduled, status="confirmed"
    ).exists():
        raise ValidationError("该计划到期实例已有确认完成记录。")
    today = business_date()
    is_current_instance = scheduled == plan.next_maintenance_date
    is_voided_rebuild = MaintenanceRecord._base_manager.select_for_update().filter(
        maintenance_plan=plan, scheduled_date=scheduled, status="voided"
    ).exists()
    if not is_current_instance and not is_voided_rebuild:
        raise ValidationError({"scheduled_date": "计划日期不是当前到期实例，也没有可重建的作废历史。"})
    if is_voided_rebuild:
        historical_records = (
            MaintenanceRecord._base_manager.select_for_update()
            .filter(maintenance_plan=plan)
            .annotate(
                boundary_priority=Case(
                    When(status="confirmed", then=0), default=1
                )
            )
        )
        previous = historical_records.filter(scheduled_date__lt=scheduled).order_by(
            "-scheduled_date", "boundary_priority", "-created_at", "-pk"
        ).first()
        following = historical_records.filter(scheduled_date__gt=scheduled).order_by(
            "scheduled_date", "boundary_priority", "-created_at", "-pk"
        ).first()
        if previous is not None and completed <= previous.completed_date:
            raise ValidationError(
                {"completed_date": "重建记录的实际完成日期必须晚于前一保养实例的历史完成日期。"}
            )
        if following is not None and completed >= following.completed_date:
            raise ValidationError(
                {"completed_date": "重建记录的实际完成日期必须早于后一保养实例的历史完成日期。"}
            )
    latest = _latest_valid_record(plan)
    if (
        is_current_instance
        and latest is not None
        and completed <= latest.completed_date
    ):
        raise ValidationError({"completed_date": "实际完成日期必须晚于上次有效保养。"})
    if add_calendar_cycle(completed, plan.cycle_value, plan.cycle_unit) <= scheduled:
        raise ValidationError({"completed_date": "实际完成日期必须使下次保养日晚于当前计划实例。"})
    if completed > today:
        raise ValidationError({"completed_date": "实际完成日期不得晚于当前上海业务日。"})
    if plan.status != "active":
        raise ValidationError("只有启用计划可以完成保养。")
    if plan.asset.asset_status in {"pending_disposal", "disposed", "sold", "other_disposed"}:
        raise ValidationError("处置中或已处置资产不能完成保养。")
    _validate_plan_inputs(
        company=plan.company, asset=plan.asset,
        responsible_employee=plan.responsible_employee,
    )
    record = MaintenanceRecord(
        company=plan.company,
        maintenance_plan=plan,
        asset=plan.asset,
        scheduled_date=scheduled,
        completed_date=completed,
        completed_by=_completion_employee(actor, plan.company),
        content_snapshot=content,
        result=result,
        status="confirmed",
        remark=str(remark or "").strip(),
        idempotency_key=key,
    )
    _save_new(record, "controlled_maintenance_record_insert")
    problem = None
    if result == "problem_found":
        problem = MaintenanceProblem(
            company=plan.company,
            maintenance_record=record,
            asset=plan.asset,
            description=problem_text,
            status="open",
        )
        _save_new(problem, "controlled_maintenance_problem_insert")
    _recalculate_plan_dates(plan)
    _audit(
        actor=actor,
        action="maintenance.completed",
        instance=record,
        new={**_serializable(payload), "problem_id": str(problem.pk) if problem else None},
        request=request,
    )
    _write_operation_marker(
        actor=actor,
        operation="complete",
        result=record,
        key=key,
        digest=digest,
        payload=payload,
        request=request,
    )
    return record


@transaction.atomic
def void_maintenance_record(
    *, actor, record, reason, idempotency_key, request=None
):
    from apps.maintenance.models import MaintenanceRecord

    plan = _lock_plan(record.maintenance_plan_id)
    require_void_maintenance_record(actor, record)
    record = MaintenanceRecord._base_manager.select_for_update().get(
        pk=record.pk, company=plan.company, maintenance_plan=plan
    )
    explanation = _required(reason, "reason", "作废保养记录必须填写原因。")
    payload = {"record_id": record.pk, "reason": explanation}
    key, digest, existing = _check_operation_idempotency(
        company=plan.company,
        operation="void_record",
        key=idempotency_key,
        payload=payload,
        model=MaintenanceRecord,
    )
    if existing is not None:
        return existing
    if record.status != "confirmed":
        raise ValidationError("只有已确认记录可以作废。")
    _base_update(
        MaintenanceRecord,
        record.pk,
        {
            "status": "voided",
            "void_reason": explanation,
            "voided_by_id": actor.pk,
            "voided_at": timezone.now(),
        },
        "controlled_maintenance_record_mutation",
    )
    _recalculate_plan_dates(plan)
    record.refresh_from_db()
    _audit(
        actor=actor,
        action="maintenance.record_voided",
        instance=record,
        old={"status": "confirmed"},
        new={"status": "voided", "reason": explanation},
        request=request,
    )
    _write_operation_marker(
        actor=actor, operation="void_record", result=record,
        key=key, digest=digest, payload=payload, request=request,
    )
    return record


@transaction.atomic
def close_maintenance_problem(
    *, actor, problem, closure_note, idempotency_key, request=None
):
    from apps.maintenance.models import MaintenanceProblem

    plan = _lock_plan(problem.maintenance_record.maintenance_plan_id)
    problem = MaintenanceProblem._base_manager.select_for_update(of=("self",)).select_related(
        "maintenance_record", "asset__department"
    ).get(pk=problem.pk, company=plan.company)
    require_close_maintenance_problem(actor, problem)
    note = _required(closure_note, "closure_note", "关闭问题必须填写处理说明。")
    payload = {"problem_id": problem.pk, "closure_note": note}
    key, digest, existing = _check_operation_idempotency(
        company=plan.company,
        operation="close_problem",
        key=idempotency_key,
        payload=payload,
        model=MaintenanceProblem,
    )
    if existing is not None:
        return existing
    if problem.status != "open" or problem.maintenance_record.status != "confirmed":
        raise ValidationError("问题已关闭或来源保养记录已作废。")
    _base_update(
        MaintenanceProblem,
        problem.pk,
        {
            "status": "closed",
            "closed_by_id": actor.pk,
            "closed_at": timezone.now(),
            "closure_note": note,
        },
        "controlled_maintenance_problem_mutation",
    )
    problem.refresh_from_db()
    _audit(
        actor=actor,
        action="maintenance.problem_closed",
        instance=problem,
        old={"status": "open"},
        new={"status": "closed", "closure_note": note},
        request=request,
    )
    _write_operation_marker(
        actor=actor, operation="close_problem", result=problem,
        key=key, digest=digest, payload=payload, request=request,
    )
    return problem


def _lock_attachment_target(target):
    from apps.maintenance.models import MaintenanceProblem, MaintenanceRecord

    if target._meta.model_name == "maintenancerecord":
        record_id = target.pk
    elif target._meta.model_name == "maintenanceproblem":
        record_id = target.maintenance_record_id
    else:
        raise ValidationError("保养附件目标类型无效。")
    plan_id = MaintenanceRecord._base_manager.values_list(
        "maintenance_plan_id", flat=True
    ).get(pk=record_id)
    plan = _lock_plan(plan_id)
    if target._meta.model_name == "maintenancerecord":
        return MaintenanceRecord._base_manager.select_for_update(of=("self",)).select_related(
            "maintenance_plan"
        ).get(pk=record_id, company=plan.company), plan
    return MaintenanceProblem._base_manager.select_for_update(of=("self",)).select_related(
        "maintenance_record__maintenance_plan", "asset__department"
    ).get(pk=target.pk, company=plan.company), plan


@transaction.atomic
def upload_maintenance_attachment(
    *, actor, target, uploaded_file, security_class="A0", request=None
):
    from apps.assets.models import AttachmentLink
    from apps.assets.services import (
        MIME_BY_EXTENSION, _detect_mime, _read_upload, _validate_filename,
    )
    from apps.masterdata.models import Attachment
    from apps.masterdata.services import get_system_setting

    target, plan = _lock_attachment_target(target)
    if not can_manage_maintenance_attachment(
        actor, target, security_class=security_class
    ):
        raise PermissionDenied("您没有上传此保养附件的权限。")
    original_name, extension = _validate_filename(uploaded_file.name)
    allowed = set(get_system_setting(
        company=plan.company, key="attachment_allowed_extensions"
    ))
    if extension not in allowed or extension not in MIME_BY_EXTENSION:
        raise ValidationError("当前公司未允许该附件扩展名。")
    limit = get_system_setting(
        company=plan.company, key="attachment_max_size_bytes"
    )
    data = _read_upload(uploaded_file, limit)
    detected_mime = _detect_mime(extension, data)
    client_mime = str(getattr(uploaded_file, "content_type", "") or "").lower()
    if client_mime and client_mime != detected_mime:
        raise ValidationError("客户端 MIME 与文件实际类型不一致。")
    storage_key = (
        f"private/assets/{plan.company_id}/maintenance/"
        f"{uuid.uuid4().hex}.{extension}"
    )
    saved_key = default_storage.save(storage_key, ContentFile(data))
    linked = False
    try:
        attachment = _save_new(
            Attachment(
                company=plan.company,
                storage_key=saved_key,
                original_filename=original_name[:255],
                safe_filename=(get_valid_filename(original_name) or f"attachment.{extension}")[:255],
                file_size=len(data),
                mime_type=detected_mime,
                sha256=hashlib.sha256(data).hexdigest(),
                uploaded_by=actor,
                malware_scan_status=Attachment.MalwareScanStatus.POLICY_LIMITED,
                is_available=False,
            )
        )
        fields = {
            "maintenance_record": target if target._meta.model_name == "maintenancerecord" else None,
            "maintenance_problem": target if target._meta.model_name == "maintenanceproblem" else None,
        }
        link = _save_new(
            AttachmentLink(
                company=plan.company,
                attachment=attachment,
                role=AttachmentLink.Role.MAINTENANCE,
                security_class=security_class,
                created_by=actor,
                **fields,
            )
        )
        _base_update(
            Attachment, attachment.pk, {"is_available": True},
            "controlled_asset_mutation",
        )
        attachment.is_available = True
        _audit(
            actor=actor,
            action="maintenance.attachment_uploaded",
            instance=link,
            new={
                "target_type": target._meta.object_name,
                "target_id": str(target.pk),
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
def void_maintenance_attachment(*, actor, link, reason, request=None):
    from apps.assets.models import AttachmentLink

    raw = AttachmentLink._base_manager.select_related(
        "maintenance_record", "maintenance_problem__maintenance_record"
    ).get(pk=getattr(link, "pk", link))
    target = raw.maintenance_record or raw.maintenance_problem
    if target is None:
        raise ValidationError("目标不是保养附件。")
    target, plan = _lock_attachment_target(target)
    link = AttachmentLink._base_manager.select_for_update().get(
        pk=raw.pk, company=plan.company
    )
    if not can_manage_maintenance_attachment(
        actor, target, security_class=link.security_class
    ):
        raise PermissionDenied("您没有作废此保养附件的权限。")
    explanation = _required(reason, "reason", "作废保养附件必须填写原因。")
    if link.status == AttachmentLink.Status.VOIDED:
        if link.void_reason != explanation:
            raise ValidationError("该附件已使用不同原因作废。")
        return link
    _base_update(
        AttachmentLink,
        link.pk,
        {
            "status": AttachmentLink.Status.VOIDED,
            "void_reason": explanation,
            "voided_by_id": actor.pk,
            "voided_at": timezone.now(),
        },
        "controlled_asset_mutation",
    )
    link.refresh_from_db()
    _audit(
        actor=actor,
        action="maintenance.attachment_voided",
        instance=link,
        old={"status": "active"},
        new={"status": "voided", "reason": explanation},
        request=request,
    )
    return link


def require_maintenance_attachment_download(*, actor, link):
    from apps.assets.models import AttachmentLink
    from apps.masterdata.models import Attachment

    link = AttachmentLink._base_manager.select_related(
        "attachment",
        "maintenance_record__maintenance_plan__asset__department",
        "maintenance_problem__maintenance_record__maintenance_plan__asset__department",
    ).filter(
        pk=getattr(link, "pk", link),
        status=AttachmentLink.Status.ACTIVE,
        attachment__is_available=True,
        attachment__malware_scan_status__in=(
            Attachment.MalwareScanStatus.POLICY_LIMITED,
            Attachment.MalwareScanStatus.CLEAN,
        ),
    ).first()
    if link is None:
        raise PermissionDenied("保养附件不存在或当前不可下载。")
    require_view_maintenance_attachment(actor, link)
    return link


__all__ = [
    "close_maintenance_problem",
    "complete_maintenance",
    "create_maintenance_plan",
    "due_maintenance_plans",
    "require_maintenance_attachment_download",
    "set_maintenance_plan_status",
    "update_maintenance_plan",
    "upload_maintenance_attachment",
    "void_maintenance_attachment",
    "void_maintenance_record",
]
