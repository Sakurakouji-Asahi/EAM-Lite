"""Controlled publication, download and external-reference services."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.normalization import clean_display_identifier, normalize_identifier
from apps.masterdata.permissions import current_company
from apps.reports.excel import XLSX_MIME, write_report_workbook, write_tplus_workbook
from apps.reports.permissions import (
    require_download_export,
    require_export_report,
    require_manage_external_reference,
    require_tplus_export,
)
from apps.reports.queries import build_report_dataset, build_tplus_dataset
from apps.reports.schemas import validate_totals, visible_report_definition


def _serializable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serializable(item) for item in value]
    return value


def _request_hash(payload):
    encoded = json.dumps(
        _serializable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stored_export_filters(*, actor, company, report_key, filters):
    from apps.assets.permissions import can_view_financial_fields
    from apps.masterdata.permissions import resolve_department_ids, role_names_for
    from apps.reports.schemas import get_report_definition
    from apps.supplies.permissions import can_view_supply_cost

    result = dict(filters or {})
    roles = role_names_for(actor)
    definition = get_report_definition(report_key)
    result["_report_schema_version"] = definition.schema_version
    if definition.supply:
        visible = visible_report_definition(
            report_key,
            include_supply_cost=can_view_supply_cost(actor),
            include_asset_finance=can_view_financial_fields(actor),
        )
        cost_columns = [
            column.key for column in visible.columns if column.access
        ]
        result["_includes_cost_fields"] = bool(cost_columns)
        result["_cost_columns"] = cost_columns
    global_roles = (
        {"finance", "equipment", "management", "hr"}
        if definition.hr_clearance
        else {"finance", "equipment", "warehouse", "management", "system_admin"}
    )
    if "department_manager" in roles and not roles.intersection(global_roles):
        result["_authorized_department_ids"] = sorted(resolve_department_ids(actor, company))
    if definition.hr_clearance and "warehouse" in roles and not roles.intersection(global_roles):
        from apps.offboarding.domain import UNRESOLVED_ITEM_RESOLUTIONS
        from apps.offboarding.permissions import scoped_clearance_items

        result["_authorized_clearance_item_ids"] = sorted(
            str(item_id)
            for item_id in scoped_clearance_items(actor, company).filter(
                resolution__in=UNRESOLVED_ITEM_RESOLUTIONS
            ).values_list("pk", flat=True)
        )
    return result


def _require_company(company):
    selected = current_company(include_inactive=True)
    if company is None or selected is None or company.pk != selected.pk:
        raise PermissionDenied("请求公司不属于当前公司边界。")


def _required_text(value, field, *, max_length=None):
    result = str(value or "").strip()
    if not result:
        raise ValidationError({field: "不得为空。"})
    if max_length and len(result) > max_length:
        raise ValidationError({field: f"不得超过 {max_length} 个字符。"})
    return result


def _enable_capability(name):
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)", [f"eam_lite.{name}", "on"]
            )


def _base_update(model, pk, values, capability):
    _enable_capability(capability)
    if QuerySet.update(model._base_manager.filter(pk=pk), **values) != 1:
        raise ValidationError("受控更新未命中唯一记录。")


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


def _create_export_request(
    *, actor, company, export_type, filters, idempotency_key, request_hash, request
):
    from apps.masterdata.models import Company
    from apps.reports.models import ExportLog

    key = _required_text(idempotency_key, "idempotency_key", max_length=128)
    filters_json = _serializable(dict(filters or {}))
    with transaction.atomic():
        Company.objects.select_for_update().get(pk=company.pk)
        existing = (
            ExportLog.objects.select_for_update()
            .filter(company=company, idempotency_key=key)
            .first()
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValidationError("同一幂等键已用于不同导出请求。")
            return existing, False
        export_log = ExportLog(
            company=company,
            export_type=export_type,
            filters_json=filters_json,
            request_hash=request_hash,
            idempotency_key=key,
            requested_by=actor,
        )
        export_log.full_clean()
        try:
            export_log.save(force_insert=True)
        except IntegrityError as exc:
            raise ValidationError("导出幂等键发生并发冲突，请重试。") from exc
        _audit(
            actor=actor,
            action="report_export_requested",
            instance=export_log,
            new={"export_type": export_type, "filters": filters_json},
            request=request,
        )
    return export_log, True


def _mark_export_failed(*, export_log, actor, exc, request):
    from apps.reports.models import ExportLog

    summary = (
        "；".join(exc.messages)[:1000]
        if isinstance(exc, ValidationError)
        else "导出生成失败，请联系管理员。"
    )
    with transaction.atomic():
        locked = ExportLog.objects.select_for_update().get(pk=export_log.pk)
        if locked.status != ExportLog.Status.PENDING:
            return locked
        _base_update(
            ExportLog,
            locked.pk,
            {"status": ExportLog.Status.FAILED, "error_summary": summary},
            "controlled_export_log_mutation",
        )
        _audit(
            actor=actor,
            action="report_export_failed",
            instance=locked,
            old={"status": ExportLog.Status.PENDING},
            new={"status": ExportLog.Status.FAILED, "error_type": exc.__class__.__name__},
            request=request,
        )
    return ExportLog.objects.get(pk=export_log.pk)


def _publish_export(
    *, export_log, actor, dataset, saved_key, digest, file_size, filename,
    completed_at, request
):
    from apps.masterdata.models import Attachment
    from apps.reports.models import ExportLog, ExportLogTotal

    validate_totals(
        export_log.export_type, dataset.definition.schema_version, dataset.totals
    )
    with transaction.atomic():
        locked = ExportLog.objects.select_for_update().get(pk=export_log.pk)
        if locked.status != ExportLog.Status.PENDING:
            return locked, False
        attachment = Attachment(
            company=locked.company,
            storage_key=saved_key,
            original_filename=filename,
            safe_filename=filename,
            file_size=file_size,
            mime_type=XLSX_MIME,
            sha256=digest,
            uploaded_by=actor,
            malware_scan_status=Attachment.MalwareScanStatus.PENDING,
            is_available=False,
        )
        attachment.full_clean()
        attachment.save(force_insert=True)
        totals = [
            ExportLogTotal(
                company=locked.company,
                export_log=locked,
                metric_key=key,
                amount=value,
                currency="CNY",
            )
            for key, value in dataset.totals.items()
        ]
        for total in totals:
            total.full_clean()
            total.save(force_insert=True)
        _base_update(
            Attachment,
            attachment.pk,
            {
                "malware_scan_status": Attachment.MalwareScanStatus.POLICY_LIMITED,
                "is_available": True,
            },
            "controlled_asset_mutation",
        )
        attachment.malware_scan_status = Attachment.MalwareScanStatus.POLICY_LIMITED
        attachment.is_available = True
        values = {
            "data_snapshot_at": dataset.data_snapshot_at,
            "row_count": dataset.row_count,
            "output_attachment_id": attachment.pk,
            "output_sha256": digest,
            "totals_schema_version": dataset.definition.schema_version,
            "completed_at": completed_at,
            "status": ExportLog.Status.COMPLETED,
            "error_summary": "",
        }
        _base_update(
            ExportLog, locked.pk, values, "controlled_export_log_mutation"
        )
        for field, value in values.items():
            setattr(locked, field, value)
        locked.output_attachment = attachment
        _audit(
            actor=actor,
            action="report_export_completed",
            instance=locked,
            old={"status": ExportLog.Status.PENDING},
            new={
                "status": ExportLog.Status.COMPLETED,
                "data_snapshot_at": dataset.data_snapshot_at,
                "completed_at": completed_at,
                "row_count": dataset.row_count,
                "output_sha256": digest,
                "totals_schema_version": dataset.definition.schema_version,
            },
            request=request,
        )
    return locked, True


def _write_and_publish(
    *, export_log, actor, company, dataset, request, tplus=False
):
    completed_at = timezone.now()
    date_part = completed_at.astimezone(timezone.get_current_timezone()).strftime("%Y%m%d-%H%M%S")
    filename = f"{export_log.export_type}-{date_part}-{str(export_log.pk)[:8]}.xlsx"
    temp_path = None
    saved_key = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".xlsx",
            dir=getattr(settings, "IMPORT_TEMP_ROOT", None),
            delete=False,
        ) as temp:
            temp_path = temp.name
        if tplus:
            write_tplus_workbook(
                dataset,
                temp_path,
                export_id=export_log.pk,
                company_name=str(company),
                requested_by=getattr(actor, "get_username", lambda: str(actor))(),
                generated_at=completed_at,
            )
        else:
            write_report_workbook(dataset, temp_path, generated_at=completed_at)
        hasher = hashlib.sha256()
        with open(temp_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        file_size = os.path.getsize(temp_path)
        storage_key = f"private/assets/{company.pk}/exports/{uuid.uuid4().hex}.xlsx"
        with open(temp_path, "rb") as source:
            saved_key = default_storage.save(storage_key, File(source))
        result, published = _publish_export(
            export_log=export_log,
            actor=actor,
            dataset=dataset,
            saved_key=saved_key,
            digest=digest,
            file_size=file_size,
            filename=filename,
            completed_at=completed_at,
            request=request,
        )
        if not published:
            default_storage.delete(saved_key)
            saved_key = None
        return result
    except Exception as exc:
        if saved_key:
            try:
                default_storage.delete(saved_key)
            except Exception:
                pass
        _mark_export_failed(export_log=export_log, actor=actor, exc=exc, request=request)
        raise
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def generate_report_export(
    *, actor, company, report_key, filters=None, idempotency_key, request=None
):
    """Materialize a generic report, write a private XLSX and publish atomically."""
    _require_company(company)
    require_export_report(actor, report_key)
    payload = {
        "export_type": report_key,
        "filters": _stored_export_filters(
            actor=actor, company=company, report_key=report_key, filters=filters
        ),
    }
    digest = _request_hash(payload)
    export_log, created = _create_export_request(
        actor=actor,
        company=company,
        export_type=report_key,
        filters=payload["filters"],
        idempotency_key=idempotency_key,
        request_hash=digest,
        request=request,
    )
    if not created:
        return export_log
    try:
        dataset = build_report_dataset(
            actor=actor, company=company, report_key=report_key,
            filters=payload["filters"],
        )
    except Exception as exc:
        _mark_export_failed(export_log=export_log, actor=actor, exc=exc, request=request)
        raise
    return _write_and_publish(
        export_log=export_log, actor=actor, company=company,
        dataset=dataset, request=request,
    )


def generate_tplus_export(
    *, actor, company, period_start, period_end, filters=None,
    idempotency_key, request=None
):
    """Generate the read-only five-sheet T+ reconciliation workbook."""
    _require_company(company)
    require_tplus_export(actor)
    export_type = "tplus_reconciliation"
    stored_filters = {
        **dict(filters or {}),
        "period": period_start.strftime("%Y-%m"),
        "period_start": period_start,
        "period_end": period_end,
    }
    digest = _request_hash({"export_type": export_type, "filters": stored_filters})
    export_log, created = _create_export_request(
        actor=actor,
        company=company,
        export_type=export_type,
        filters=stored_filters,
        idempotency_key=idempotency_key,
        request_hash=digest,
        request=request,
    )
    if not created:
        return export_log
    try:
        dataset = build_tplus_dataset(
            actor=actor,
            company=company,
            period_start=period_start,
            period_end=period_end,
            filters=filters,
        )
        if "period" not in dataset.filters:
            from dataclasses import replace
            from types import MappingProxyType

            dataset = replace(
                dataset,
                filters=MappingProxyType(
                    {**dict(dataset.filters), "period": stored_filters["period"]}
                ),
            )
    except Exception as exc:
        _mark_export_failed(export_log=export_log, actor=actor, exc=exc, request=request)
        raise
    return _write_and_publish(
        export_log=export_log, actor=actor, company=company,
        dataset=dataset, request=request, tplus=True,
    )


def get_export_for_download(*, actor, company, export_id, request=None):
    """Re-authorize a completed export and return its private Attachment metadata."""
    from apps.reports.models import ExportLog

    _require_company(company)
    with transaction.atomic():
        export_log = ExportLog.objects.select_related("output_attachment").get(
            company=company, pk=export_id
        )
        require_download_export(actor, export_log)
        attachment = export_log.output_attachment
        if (
            attachment is None
            or attachment.company_id != company.pk
            or not attachment.is_available
            or attachment.sha256 != export_log.output_sha256
        ):
            raise ValidationError("导出附件不可用或完整性校验失败。")
        _audit(
            actor=actor,
            action="report_export_downloaded",
            instance=export_log,
            new={"output_sha256": export_log.output_sha256},
            request=request,
        )
    return attachment


def create_or_correct_external_reference(
    *, actor, asset, reference_value, note="", reason, request=None
):
    """Create or audit-correct one T+ asset-card reference without external I/O."""
    from apps.assets.models import Asset, AssetExternalReference

    company = asset.company
    _require_company(company)
    require_manage_external_reference(actor)
    display_value = clean_display_identifier(reference_value)
    normalized_value = normalize_identifier(display_value)
    if not normalized_value:
        raise ValidationError({"reference_value": "T+ 资产卡片编码不得为空。"})
    reason = _required_text(reason, "reason")
    note = str(note or "").strip()
    try:
        with transaction.atomic():
            locked_asset = Asset.objects.select_for_update().get(
                pk=asset.pk, company=company
            )
            if locked_asset.asset_status in {"draft", "pending_finance"}:
                raise ValidationError("只有正式资产可以维护 T+ 资产卡片编码。")
            current = (
                AssetExternalReference.objects.select_for_update()
                .filter(
                    company=company,
                    asset=locked_asset,
                    external_system=AssetExternalReference.ExternalSystem.TPLUS,
                    reference_type=AssetExternalReference.ReferenceType.ASSET_CARD_CODE,
                )
                .first()
            )
            duplicate = AssetExternalReference.objects.filter(
                company=company,
                external_system=AssetExternalReference.ExternalSystem.TPLUS,
                reference_type=AssetExternalReference.ReferenceType.ASSET_CARD_CODE,
                normalized_value=normalized_value,
            )
            if current is not None:
                duplicate = duplicate.exclude(pk=current.pk)
            if duplicate.exists():
                raise ValidationError({"reference_value": "该 T+ 资产卡片编码已被其他资产使用。"})
            if current is None:
                current = AssetExternalReference(
                    company=company,
                    asset=locked_asset,
                    external_system=AssetExternalReference.ExternalSystem.TPLUS,
                    reference_type=AssetExternalReference.ReferenceType.ASSET_CARD_CODE,
                    reference_value=display_value,
                    note=note,
                    created_by=actor,
                )
                current.full_clean()
                current.save(force_insert=True)
                _audit(
                    actor=actor,
                    action="asset_external_reference_created",
                    instance=current,
                    new={"reference_value": display_value, "note": note, "reason": reason},
                    request=request,
                )
                return current
            if current.reference_value == display_value and current.note == note:
                return current
            old = {"reference_value": current.reference_value, "note": current.note}
            updated_at = timezone.now()
            _base_update(
                AssetExternalReference,
                current.pk,
                {
                    "reference_value": display_value,
                    "normalized_value": normalized_value,
                    "note": note,
                    "updated_at": updated_at,
                },
                "controlled_external_reference_mutation",
            )
            current.reference_value = display_value
            current.normalized_value = normalized_value
            current.note = note
            current.updated_at = updated_at
            _audit(
                actor=actor,
                action="asset_external_reference_corrected",
                instance=current,
                old=old,
                new={"reference_value": display_value, "note": note, "reason": reason},
                request=request,
            )
            return current
    except IntegrityError as exc:
        raise ValidationError("T+ 资产卡片编码与现有引用冲突。") from exc


__all__ = [
    "create_or_correct_external_reference",
    "generate_report_export",
    "generate_tplus_export",
    "get_export_for_download",
]
