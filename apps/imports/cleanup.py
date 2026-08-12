"""Audited, idempotent cleanup for Sprint 1 import staging evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.audit.services import write_business_audit_log
from apps.masterdata.permissions import has_role, is_login_capable
from apps.imports.tempfiles import (
    ACTIVE_MARKER_DIRECTORY,
    marker_is_active,
    marker_path_for,
)


AUTOMATIC_BATCH_STATUSES = frozenset({"uploaded", "invalid", "failed"})


@dataclass
class CleanupReport:
    dry_run: bool
    batch_candidates: int = 0
    batches_deleted: list[int] = field(default_factory=list)
    batches_skipped: dict[int, str] = field(default_factory=dict)
    attachments_deleted: list[int] = field(default_factory=list)
    attachments_skipped: dict[int, str] = field(default_factory=dict)
    legacy_files_deleted: list[str] = field(default_factory=list)
    legacy_files_skipped: dict[str, str] = field(default_factory=dict)


def _positive_days(value, label, *, minimum=1):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} 必须是整数天数。") from exc
    if value < minimum:
        raise ValidationError(f"{label} 不得小于 {minimum} 天。")
    return value


def _positive_hours(value, label):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} 必须是整数小时数。") from exc
    if value < 1:
        raise ValidationError(f"{label} 必须大于 0 小时。")
    return value


def _require_system_admin(actor):
    if (
        actor is None
        or not is_login_capable(actor)
        or not has_role(actor, "system_admin")
    ):
        raise PermissionDenied("只有可登录的 system_admin 可执行导入清理。")


def _cleanup_audit_company():
    from apps.masterdata.permissions import current_company

    company = current_company(include_inactive=True)
    if company is None:
        raise ValidationError("尚未建立公司，不能执行需要审计的文件清理。")
    return company


def _is_current_company_id(company_id):
    from apps.masterdata.permissions import current_company

    current = current_company(include_inactive=True)
    return current is not None and current.pk == company_id


def _lock_import_namespace(company_id):
    # Reuse the exact lock namespace used by upload idempotency decisions.
    from apps.imports.services import _lock_import_namespace as lock

    lock(company_id)


def _has_created_mapping(batch):
    return batch.rows.filter(
        created_object_type__gt="",
        created_object_id__gt="",
    ).exists() or batch.rows.filter(validation_status="created").exists()


def _mark_attachment_orphaned(attachment, now):
    attachment.is_available = False
    attachment.orphaned_at = attachment.orphaned_at or now
    attachment.full_clean()
    attachment.save(update_fields=["is_available", "orphaned_at"])


def _delete_batch_via_collector(batch):
    """Delete only after all checks while bypassing deliberately locked API."""
    from django.db.models.deletion import Collector

    collector = Collector(using=batch._state.db)
    collector.collect([batch])
    collector.delete()


def _processing_idempotency_exists(batch):
    """Return whether a durable processing record blocks cleanup.

    Sprint 1 uses ``ImportBatch`` itself plus a company advisory lock for
    upload/confirm idempotency and does not create the optional cross-operation
    ``IdempotencyRecord``.  Keep this explicit hook so cleanup also honours
    that table if a later approved Sprint introduces it.
    """

    from django.apps import apps

    try:
        record_model = apps.get_model("masterdata", "IdempotencyRecord")
    except LookupError:
        return False
    return record_model.objects.filter(
        company_id=batch.company_id,
        idempotency_key=batch.idempotency_key,
        status="processing",
    ).exists()


def _audit_cleanup(*, company, actor, object_type, object_id, action, data):
    write_business_audit_log(
        company=company,
        user=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        old_data=data.get("old", {}),
        new_data=data.get("new", {}),
    )


def cleanup_import_batches(
    *, actor, retention_days=30, dry_run=True, task_id="manual", now=None
):
    """Delete expired uploaded/invalid/failed batches, never validated/confirmed.

    The 30-day value is the approved default and lower bound.  Every candidate
    is re-locked and re-checked.  The per-company advisory lock serializes this
    decision with upload idempotency processing.
    """

    from apps.masterdata.models import ImportBatch

    _require_system_admin(actor)
    retention_days = _positive_days(
        retention_days, "导入批次保留期", minimum=30
    )
    now = now or timezone.now()
    cutoff = now - timedelta(days=retention_days)
    candidate_ids = list(
        ImportBatch.objects.filter(
            status__in=AUTOMATIC_BATCH_STATUSES,
            uploaded_at__lte=cutoff,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    report = CleanupReport(dry_run=dry_run, batch_candidates=len(candidate_ids))

    for batch_id in candidate_ids:
        with transaction.atomic():
            company_id = (
                ImportBatch.objects.filter(pk=batch_id)
                .values_list("company_id", flat=True)
                .first()
            )
            if company_id is None:
                report.batches_skipped[batch_id] = "已由其他清理任务处理"
                continue
            if not _is_current_company_id(company_id):
                report.batches_skipped[batch_id] = "不属于当前公司"
                continue
            # Upload and confirmation acquire this company advisory lock
            # before row locks.  Keep the same order here to avoid a
            # batch-row/advisory-lock deadlock under concurrent cleanup.
            _lock_import_namespace(company_id)
            batch = (
                ImportBatch.objects.select_for_update()
                .select_related("company", "file_attachment")
                .filter(pk=batch_id)
                .first()
            )
            if batch is None:
                report.batches_skipped[batch_id] = "已由其他清理任务处理"
                continue
            list(batch.rows.select_for_update().order_by("pk"))
            if batch.status not in AUTOMATIC_BATCH_STATUSES:
                report.batches_skipped[batch_id] = "状态已变化，禁止自动清理"
                continue
            if batch.uploaded_at > cutoff:
                report.batches_skipped[batch_id] = "尚未达到保留期"
                continue
            if _has_created_mapping(batch):
                report.batches_skipped[batch_id] = "存在已创建对象映射"
                continue
            if _processing_idempotency_exists(batch):
                report.batches_skipped[batch_id] = "关联幂等请求仍在处理中"
                continue
            # Holding the same namespace lock as upload proves that no request
            # can currently reserve or commit this company's idempotency key.
            if dry_run:
                report.batches_deleted.append(batch_id)
                continue
            attachment = batch.file_attachment.__class__.objects.select_for_update().get(
                pk=batch.file_attachment_id
            )
            _audit_cleanup(
                company=batch.company,
                actor=actor,
                object_type="ImportBatch",
                object_id=batch.pk,
                action="import_cleanup",
                data={
                    "old": {
                        "status": batch.status,
                        "file_sha256": batch.file_sha256,
                        "idempotency_key_hash": hashlib.sha256(
                            batch.idempotency_key.encode()
                        ).hexdigest(),
                    },
                    "new": {
                        "deleted": True,
                        "task_id": str(task_id),
                        "retention_days": retention_days,
                    },
                },
            )
            _delete_batch_via_collector(batch)
            _mark_attachment_orphaned(attachment, now)
            report.batches_deleted.append(batch_id)
    return report


def abandon_validated_batch(*, actor, batch_id, reason, dry_run=True, task_id="manual"):
    """Explicitly abandon one validated batch; never called by automatic cleanup."""

    from apps.masterdata.models import ImportBatch

    _require_system_admin(actor)
    reason = str(reason or "").strip()
    if not reason:
        raise ValidationError("明确放弃已验证批次必须填写原因。")
    with transaction.atomic():
        company_id = (
            ImportBatch.objects.filter(pk=batch_id)
            .values_list("company_id", flat=True)
            .first()
        )
        if company_id is None:
            return False
        if not _is_current_company_id(company_id):
            raise PermissionDenied("批次不属于当前公司。")
        _lock_import_namespace(company_id)
        batch = (
            ImportBatch.objects.select_for_update()
            .select_related("company", "file_attachment")
            .filter(pk=batch_id)
            .first()
        )
        if batch is None:
            return False
        list(batch.rows.select_for_update().order_by("pk"))
        if batch.status != "validated":
            raise ValidationError("只能明确放弃尚未确认的 validated 批次。")
        if _has_created_mapping(batch):
            raise ValidationError("存在已创建对象映射，不能放弃批次。")
        if dry_run:
            return True
        attachment = batch.file_attachment.__class__.objects.select_for_update().get(
            pk=batch.file_attachment_id
        )
        _audit_cleanup(
            company=batch.company,
            actor=actor,
            object_type="ImportBatch",
            object_id=batch.pk,
            action="import_abandon",
            data={
                "old": {
                    "status": batch.status,
                    "file_sha256": batch.file_sha256,
                },
                "new": {
                    "deleted": True,
                    "reason": reason,
                    "task_id": str(task_id),
                },
            },
        )
        _delete_batch_via_collector(batch)
        _mark_attachment_orphaned(attachment, timezone.now())
        return True


def cleanup_orphan_attachments(
    *, actor, orphan_retention_days, dry_run=True, task_id="manual", now=None
):
    """Delete expired orphan metadata and objects after a final reference check."""

    from apps.masterdata.models import Attachment

    _require_system_admin(actor)
    days = _positive_days(orphan_retention_days, "附件孤儿保留期")
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    ids = list(
        Attachment.objects.filter(
            orphaned_at__isnull=False,
            orphaned_at__lte=cutoff,
            is_available=False,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    report = CleanupReport(dry_run=dry_run)
    for attachment_id in ids:
        storage_key = None
        with transaction.atomic():
            company_id = (
                Attachment.objects.filter(pk=attachment_id)
                .values_list("company_id", flat=True)
                .first()
            )
            if company_id is None:
                report.attachments_skipped[attachment_id] = "已由其他清理任务处理"
                continue
            if not _is_current_company_id(company_id):
                report.attachments_skipped[attachment_id] = "不属于当前公司"
                continue
            _lock_import_namespace(company_id)
            attachment = (
                Attachment.objects.select_for_update()
                .select_related("company")
                .filter(pk=attachment_id)
                .first()
            )
            if attachment is None:
                report.attachments_skipped[attachment_id] = "已由其他清理任务处理"
                continue
            if attachment.import_batches.exists():
                report.attachments_skipped[attachment_id] = "仍被导入批次引用"
                continue
            if attachment.is_available or not attachment.orphaned_at:
                report.attachments_skipped[attachment_id] = "不是不可用孤儿候选"
                continue
            if attachment.orphaned_at > cutoff:
                report.attachments_skipped[attachment_id] = "尚未达到孤儿保留期"
                continue
            if dry_run:
                report.attachments_deleted.append(attachment_id)
                continue
            storage_key = attachment.storage_key
            _audit_cleanup(
                company=attachment.company,
                actor=actor,
                object_type="Attachment",
                object_id=attachment.pk,
                action="attachment_cleanup",
                data={
                    "old": {
                        "storage_key_hash": hashlib.sha256(
                            storage_key.encode()
                        ).hexdigest(),
                        "sha256": attachment.sha256,
                        "orphaned_at": attachment.orphaned_at,
                    },
                    "new": {
                        "deleted": True,
                        "task_id": str(task_id),
                        "retention_days": days,
                    },
                },
            )
            attachment.delete()
            report.attachments_deleted.append(attachment_id)
        if storage_key:
            # Database evidence and audit are committed first.  A storage
            # failure leaves a harmless unreferenced object for legacy cleanup.
            default_storage.delete(storage_key)
    return report


def _safe_cleanup_path(root, value):
    root = Path(root).resolve()
    value = Path(value).resolve()
    if value == root or root not in value.parents:
        raise ValidationError("待清理文件超出受控根目录。")
    return value


def cleanup_legacy_temp_files(
    *, actor, older_than_hours, dry_run=True, task_id="manual", now=None
):
    """Remove only expired regular files strictly below IMPORT_TEMP_ROOT."""

    from apps.masterdata.models import Attachment

    _require_system_admin(actor)
    hours = _positive_hours(older_than_hours, "遗留临时文件时限")
    now = now or timezone.now()
    cutoff = now - timedelta(hours=hours)
    root = Path(settings.IMPORT_TEMP_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = CleanupReport(dry_run=dry_run)
    audit_company = None if dry_run else _cleanup_audit_company()
    referenced_keys = set(Attachment.objects.values_list("storage_key", flat=True))
    for candidate in sorted(root.rglob("*")):
        if ACTIVE_MARKER_DIRECTORY in candidate.relative_to(root).parts:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate = _safe_cleanup_path(root, candidate)
        relative = PurePosixPath(candidate.relative_to(root).as_posix()).as_posix()
        modified = timezone.datetime.fromtimestamp(
            candidate.stat().st_mtime, tz=timezone.get_current_timezone()
        )
        if modified > cutoff:
            report.legacy_files_skipped[relative] = "尚未达到临时时限"
            continue
        activity_marker = marker_path_for(candidate, root)
        if activity_marker.exists():
            if marker_is_active(activity_marker):
                report.legacy_files_skipped[relative] = "属于正在处理的上传任务"
                continue
            marker_modified = timezone.datetime.fromtimestamp(
                activity_marker.stat().st_mtime,
                tz=timezone.get_current_timezone(),
            )
            if marker_modified > cutoff:
                report.legacy_files_skipped[relative] = "活动标记尚未超过时限"
                continue
            if not dry_run:
                activity_marker.unlink(missing_ok=True)
        if relative in referenced_keys:
            report.legacy_files_skipped[relative] = "仍被附件元数据引用"
            continue
        if not dry_run:
            # Legacy objects have no company metadata.  Record a system audit
            # under the active company before touching the external file.
            # If the unlink fails, the durable audit still records the
            # attempted cleanup and a retry remains safe.
            with transaction.atomic():
                _audit_cleanup(
                    company=audit_company,
                    actor=actor,
                    object_type="ImportTempFile",
                    object_id=hashlib.sha256(relative.encode()).hexdigest(),
                    action="import_temp_cleanup",
                    data={
                        "new": {
                            "delete_requested": True,
                            "task_id": str(task_id),
                            "older_than_hours": hours,
                        }
                    },
                )
            candidate.unlink(missing_ok=True)
        report.legacy_files_deleted.append(relative)

    # Crash-recovery markers are not evidence.  Remove only unlocked markers
    # older than the same explicit threshold; active markers are never touched.
    marker_root = root / ACTIVE_MARKER_DIRECTORY
    if marker_root.exists():
        for marker in sorted(marker_root.glob("*.lock")):
            if marker_is_active(marker):
                continue
            modified = timezone.datetime.fromtimestamp(
                marker.stat().st_mtime, tz=timezone.get_current_timezone()
            )
            if modified <= cutoff and not dry_run:
                marker.unlink(missing_ok=True)
    return report


def cleanup_unreferenced_private_files(
    *, actor, older_than_days, dry_run=True, task_id="manual", now=None
):
    """Remove expired storage objects that have no Attachment metadata.

    This closes the failure window where storage succeeded but the process
    ended before the database could create the protected metadata row.
    """

    from apps.masterdata.models import Attachment

    _require_system_admin(actor)
    days = _positive_days(older_than_days, "无元数据私有文件保留期")
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    report = CleanupReport(dry_run=dry_run)
    root = Path(default_storage.path("private/imports")).resolve()
    if not root.exists():
        return report
    referenced = set(Attachment.objects.values_list("storage_key", flat=True))
    audit_company = None if dry_run else _cleanup_audit_company()
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate = _safe_cleanup_path(root, candidate)
        storage_key = PurePosixPath(
            candidate.relative_to(Path(default_storage.location).resolve()).as_posix()
        ).as_posix()
        modified = timezone.datetime.fromtimestamp(
            candidate.stat().st_mtime, tz=timezone.get_current_timezone()
        )
        if modified > cutoff:
            report.legacy_files_skipped[storage_key] = "尚未达到保留期"
            continue
        if storage_key in referenced:
            report.legacy_files_skipped[storage_key] = "仍被附件元数据引用"
            continue
        if not dry_run:
            with transaction.atomic():
                _audit_cleanup(
                    company=audit_company,
                    actor=actor,
                    object_type="PrivateImportFile",
                    object_id=hashlib.sha256(storage_key.encode()).hexdigest(),
                    action="import_private_file_cleanup",
                    data={
                        "new": {
                            "delete_requested": True,
                            "task_id": str(task_id),
                            "older_than_days": days,
                        }
                    },
                )
            default_storage.delete(storage_key)
        report.legacy_files_deleted.append(storage_key)
    return report
