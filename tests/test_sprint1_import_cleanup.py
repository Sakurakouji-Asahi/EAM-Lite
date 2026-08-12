import io
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils.functional import empty
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.audit.models import AuditLog
from apps.imports.cleanup import (
    abandon_validated_batch,
    cleanup_import_batches,
    cleanup_legacy_temp_files,
    cleanup_orphan_attachments,
    cleanup_unreferenced_private_files,
)
from apps.imports.services import build_template_workbook, upload_and_validate_import
from apps.imports.tempfiles import hold_temp_file_active
from apps.masterdata.models import Attachment, Company, Department, ImportBatch


pytestmark = pytest.mark.django_db
PASSWORD = "Valid-Password-2026!"


@pytest.fixture
def private_roots(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "tmp"
    settings.FILE_UPLOAD_TEMP_DIR = str(settings.IMPORT_TEMP_ROOT)
    settings.MEDIA_ROOT.mkdir()
    settings.IMPORT_TEMP_ROOT.mkdir()
    default_storage._wrapped = empty
    yield settings.MEDIA_ROOT, settings.IMPORT_TEMP_ROOT
    default_storage._wrapped = empty


def make_context():
    company = Company.objects.create(
        code="C1", normalized_code="c1", name="测试公司", short_name="测试"
    )
    admin = get_user_model().objects.create_user(
        username="admin", password=PASSWORD, display_name="管理员"
    )
    admin.groups.add(Group.objects.get(name="system_admin"))
    ordinary = get_user_model().objects.create_user(
        username="ordinary", password=PASSWORD, display_name="普通用户"
    )
    return company, admin, ordinary


def workbook_file(rows):
    data = build_template_workbook("department")
    book = load_workbook(io.BytesIO(data))
    sheet = book["部门导入"]
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    book.save(output)
    return SimpleUploadedFile(
        "department.xlsx",
        output.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )


def upload(company, admin, *, good=False, idem="cleanup-test"):
    rows = (
        [["D1", "部门一", "", "", "是"]]
        if good
        else [["D1", "错误部门", "MISSING", "", "是"]]
    )
    return upload_and_validate_import(
        actor=admin,
        company=company,
        import_type="department",
        uploaded_file=workbook_file(rows),
        idempotency_key=idem,
    )


def age_batch(batch, *, days):
    when = timezone.now() - timedelta(days=days)
    ImportBatch.objects.filter(pk=batch.pk).update(uploaded_at=when)
    batch.refresh_from_db()
    return batch


def test_upload_is_unavailable_until_atomic_publication_and_failure_is_private(
    private_roots,
):
    company, admin, _ordinary = make_context()
    observed = []
    real_save = Attachment.save

    def observe_save(instance, *args, **kwargs):
        if instance._state.adding:
            observed.append(
                (instance.malware_scan_status, instance.is_available, instance.storage_key)
            )
        return real_save(instance, *args, **kwargs)

    with patch.object(Attachment, "save", observe_save):
        batch = upload(company, admin)

    assert observed == [("pending", False, batch.file_attachment.storage_key)]
    batch.file_attachment.refresh_from_db()
    assert batch.file_attachment.is_available
    assert batch.file_attachment.malware_scan_status == "policy_limited"
    assert batch.file_attachment.storage_key.startswith("private/imports/")
    with pytest.raises(ValueError, match="没有公开 URL"):
        default_storage.url(batch.file_attachment.storage_key)

    with (
        patch(
            "apps.imports.services._audit_batch",
            side_effect=RuntimeError("audit failed"),
        ),
        patch("apps.imports.services.default_storage.delete", side_effect=OSError),
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            upload(company, admin, good=True, idem="failed-upload")
    assert not ImportBatch.objects.filter(idempotency_key="failed-upload").exists()
    assert not Attachment.objects.filter(company=company).exclude(
        pk=batch.file_attachment_id
    ).exists()
    # A storage-delete failure leaves only a private, unreferenced object;
    # the legacy cleanup is deliberately able to remove it later.
    failed_files = [
        item
        for item in Path(default_storage.path("private/imports")).rglob("*.xlsx")
        if item.name != Path(batch.file_attachment.storage_key).name
    ]
    assert len(failed_files) == 1
    old_time = (timezone.now() - timedelta(days=8)).timestamp()
    os.utime(failed_files[0], (old_time, old_time))
    cleanup_unreferenced_private_files(
        actor=admin, older_than_days=7, dry_run=False
    )
    assert not failed_files[0].exists()


def test_automatic_cleanup_retention_status_mapping_and_repeat_are_safe(private_roots):
    company, admin, _ordinary = make_context()
    invalid = age_batch(upload(company, admin, idem="old-invalid"), days=31)
    validated = age_batch(upload(company, admin, good=True, idem="old-valid"), days=31)

    dry = cleanup_import_batches(actor=admin, dry_run=True, retention_days=30)
    assert invalid.pk in dry.batches_deleted
    assert ImportBatch.objects.filter(pk=invalid.pk).exists()

    done = cleanup_import_batches(actor=admin, dry_run=False, retention_days=30)
    assert invalid.pk in done.batches_deleted
    assert not ImportBatch.objects.filter(pk=invalid.pk).exists()
    assert ImportBatch.objects.filter(pk=validated.pk, status="validated").exists()
    attachment = Attachment.objects.get(pk=invalid.file_attachment_id)
    assert attachment.orphaned_at is not None and not attachment.is_available
    assert default_storage.exists(attachment.storage_key)
    assert AuditLog.objects.filter(
        action="import_cleanup", object_id=str(invalid.pk), company=company
    ).exists()

    repeated = cleanup_import_batches(actor=admin, dry_run=False, retention_days=30)
    assert invalid.pk not in repeated.batches_deleted
    with pytest.raises(ValidationError, match="不得小于 30"):
        cleanup_import_batches(actor=admin, retention_days=29)


def test_confirmed_created_and_referenced_evidence_are_protected(private_roots):
    company, admin, _ordinary = make_context()
    validated = age_batch(upload(company, admin, good=True), days=31)
    from apps.imports.services import confirm_import_batch

    confirm_import_batch(actor=admin, batch=validated)
    report = cleanup_import_batches(actor=admin, dry_run=False, retention_days=30)
    assert validated.pk not in report.batches_deleted
    assert ImportBatch.objects.filter(pk=validated.pk, status="confirmed").exists()
    assert validated.rows.filter(validation_status="created").exists()
    assert Department.objects.filter(company=company).exists()
    assert default_storage.exists(validated.file_attachment.storage_key)

    attachment = validated.file_attachment
    with pytest.raises(Exception):
        attachment.delete()


def test_validated_requires_explicit_admin_abandon_reason_and_is_audited(private_roots):
    company, admin, ordinary = make_context()
    batch = upload(company, admin, good=True)
    with pytest.raises(PermissionDenied):
        abandon_validated_batch(
            actor=ordinary, batch_id=batch.pk, reason="无权", dry_run=False
        )
    admin.is_active = False
    admin.save(update_fields=["is_active"])
    with pytest.raises(PermissionDenied):
        abandon_validated_batch(
            actor=admin, batch_id=batch.pk, reason="停用账号不能清理", dry_run=False
        )
    admin.is_active = True
    admin.save(update_fields=["is_active"])
    with pytest.raises(ValidationError, match="必须填写原因"):
        abandon_validated_batch(actor=admin, batch_id=batch.pk, reason="")
    assert abandon_validated_batch(
        actor=admin, batch_id=batch.pk, reason="用户确认不再导入", dry_run=True
    )
    assert ImportBatch.objects.filter(pk=batch.pk).exists()
    assert abandon_validated_batch(
        actor=admin,
        batch_id=batch.pk,
        reason="用户确认不再导入",
        dry_run=False,
    )
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()
    assert AuditLog.objects.filter(
        company=company, action="import_abandon", object_id=str(batch.pk)
    ).exists()
    assert not abandon_validated_batch(
        actor=admin, batch_id=batch.pk, reason="重复执行", dry_run=False
    )


def test_orphan_cleanup_waits_rechecks_references_and_is_idempotent(private_roots):
    company, admin, _ordinary = make_context()
    batch = age_batch(upload(company, admin), days=31)
    cleanup_import_batches(actor=admin, dry_run=False, retention_days=30)
    attachment = Attachment.objects.get(pk=batch.file_attachment_id)
    Attachment.objects.filter(pk=attachment.pk).update(
        orphaned_at=timezone.now() - timedelta(days=8)
    )

    dry = cleanup_orphan_attachments(
        actor=admin, orphan_retention_days=7, dry_run=True
    )
    assert attachment.pk in dry.attachments_deleted
    assert Attachment.objects.filter(pk=attachment.pk).exists()

    done = cleanup_orphan_attachments(
        actor=admin, orphan_retention_days=7, dry_run=False
    )
    assert attachment.pk in done.attachments_deleted
    assert not Attachment.objects.filter(pk=attachment.pk).exists()
    assert not default_storage.exists(attachment.storage_key)
    assert AuditLog.objects.filter(
        company=company, action="attachment_cleanup", object_id=str(attachment.pk)
    ).exists()
    repeated = cleanup_orphan_attachments(
        actor=admin, orphan_retention_days=7, dry_run=False
    )
    assert attachment.pk not in repeated.attachments_deleted


def test_legacy_temp_and_unreferenced_private_cleanup_are_bounded_and_repeatable(
    private_roots,
):
    media_root, temp_root = private_roots
    _company, admin, _ordinary = make_context()
    old_temp = temp_root / "legacy" / "old.tmp"
    old_temp.parent.mkdir()
    old_temp.write_bytes(b"temporary")
    old_time = (timezone.now() - timedelta(hours=25)).timestamp()
    os.utime(old_temp, (old_time, old_time))

    with hold_temp_file_active(old_temp, temp_root):
        active = cleanup_legacy_temp_files(
            actor=admin, older_than_hours=24, dry_run=False
        )
        assert active.legacy_files_skipped["legacy/old.tmp"] == (
            "属于正在处理的上传任务"
        )
        assert old_temp.exists()

    dry = cleanup_legacy_temp_files(actor=admin, older_than_hours=24, dry_run=True)
    assert "legacy/old.tmp" in dry.legacy_files_deleted and old_temp.exists()
    cleanup_legacy_temp_files(actor=admin, older_than_hours=24, dry_run=False)
    assert not old_temp.exists()
    cleanup_legacy_temp_files(actor=admin, older_than_hours=24, dry_run=False)

    key = default_storage.save("private/imports/legacy.xlsx", ContentFile(b"legacy"))
    private_path = Path(default_storage.path(key))
    old_time = (timezone.now() - timedelta(days=8)).timestamp()
    os.utime(private_path, (old_time, old_time))
    cleanup_unreferenced_private_files(
        actor=admin, older_than_days=7, dry_run=False
    )
    assert not default_storage.exists(key)
    cleanup_unreferenced_private_files(
        actor=admin, older_than_days=7, dry_run=False
    )


def test_source_download_has_no_public_route_and_rejects_unauthorized_users(
    client, private_roots
):
    _media, _temp = private_roots
    company, admin, ordinary = make_context()
    batch = upload(company, admin)
    source_url = reverse("imports:source", args=[batch.pk])

    response = client.get(source_url)
    assert response.status_code == 302 and response.url.startswith("/login/")
    client.force_login(ordinary)
    assert client.get(source_url).status_code == 403
    assert client.get(f"/protected-media/{batch.file_attachment.storage_key}").status_code == 404

    client.force_login(admin)
    response = client.get(source_url)
    assert response.status_code == 200
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Disposition"].startswith("attachment;")
    batch.file_attachment.is_available = False
    batch.file_attachment.save(update_fields=["is_available"])
    assert client.get(source_url).status_code == 404


def test_cleanup_management_command_is_dry_run_by_default_and_executes_explicitly(
    private_roots,
):
    company, admin, ordinary = make_context()
    batch = age_batch(upload(company, admin, idem="command-cleanup"), days=31)

    output = StringIO()
    call_command(
        "cleanup_import_staging",
        actor=admin.username,
        task_id="pytest-dry-run",
        stdout=output,
    )
    assert "mode=dry-run" in output.getvalue()
    assert ImportBatch.objects.filter(pk=batch.pk).exists()

    output = StringIO()
    call_command(
        "cleanup_import_staging",
        actor=admin.username,
        task_id="pytest-execute",
        execute=True,
        stdout=output,
    )
    assert "mode=execute" in output.getvalue()
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()
    assert AuditLog.objects.filter(
        company=company,
        action="import_cleanup",
        object_id=str(batch.pk),
        new_data_json__task_id="pytest-execute",
    ).exists()

    with pytest.raises(CommandError, match="只有可登录的 system_admin"):
        call_command(
            "cleanup_import_staging",
            actor=ordinary.username,
            task_id="pytest-denied",
            stdout=StringIO(),
        )
