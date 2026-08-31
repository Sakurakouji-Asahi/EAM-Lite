import hashlib
import io
import shutil
import threading
import uuid
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections, connection
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.permissions import AUDIT_OBJECT_TYPE_REGISTRY
from apps.imports.services import _validate_xlsx_container, build_template_workbook
from apps.masterdata.models import (
    Attachment,
    Company,
    Department,
    Employee,
    ImportBatch,
    ImportRow,
)
from apps.reports.permissions import can_view_export


pytestmark = pytest.mark.django_db


def _import_batch(
    *,
    settings,
    tmp_path,
    row_count=51,
    import_type=ImportBatch.ImportType.DEPARTMENT,
    template_version="department-v1",
):
    settings.MEDIA_ROOT = tmp_path / "media"
    company = Company.objects.create(
        code="PLATFORM",
        normalized_code="platform",
        name="生产加固测试公司",
        short_name="加固",
    )
    user = get_user_model().objects.create_user(
        username="platform-admin",
        password="Valid-Password-2026!",
        display_name="平台管理员",
    )
    role, _created = Group.objects.get_or_create(name="system_admin")
    user.groups.add(role)
    content = b"protected import evidence"
    digest = hashlib.sha256(content).hexdigest()
    storage_key = default_storage.save(
        f"private/imports/{company.pk}/source.xlsx",
        ContentFile(content),
    )
    attachment = Attachment.objects.create(
        company=company,
        storage_key=storage_key,
        original_filename="source.xlsx",
        safe_filename="source.xlsx",
        file_size=len(content),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256=digest,
        uploaded_by=user,
        malware_scan_status=Attachment.MalwareScanStatus.POLICY_LIMITED,
        is_available=True,
    )
    batch = ImportBatch.objects.create(
        company=company,
        import_type=import_type,
        template_version=template_version,
        file_attachment=attachment,
        file_sha256=digest,
        status=ImportBatch.Status.VALIDATED,
        total_rows=row_count,
        valid_rows=row_count,
        error_rows=0,
        warning_rows=0,
        request_hash="a" * 64,
        idempotency_key="platform-pagination",
        uploaded_by=user,
        validated_at=timezone.now(),
    )
    ImportRow.objects.bulk_create(
        [
            ImportRow(
                batch=batch,
                row_number=index + 2,
                raw_data_json={"部门编码": f"D{index:03d}"},
                normalized_data_json={"code": f"D{index:03d}"},
                validation_status=ImportRow.ValidationStatus.VALID,
            )
            for index in range(row_count)
        ]
    )
    return user, batch


def test_import_preview_is_paginated_and_sensitive_responses_are_not_cached(
    client, settings, tmp_path
):
    user, batch = _import_batch(settings=settings, tmp_path=tmp_path)
    client.force_login(user)

    first = client.get(reverse("imports:batch_detail", args=[batch.pk]))
    assert first.status_code == 200
    assert len(first.context["rows"]) == 50
    assert first.context["page_obj"].paginator.num_pages == 2
    assert "no-store" in first["Cache-Control"]

    second = client.get(
        reverse("imports:batch_detail", args=[batch.pk]), {"page": "2"}
    )
    assert second.status_code == 200
    assert [row.row_number for row in second.context["rows"]] == [52]
    assert "no-store" in second["Cache-Control"]

    source = client.get(reverse("imports:source", args=[batch.pk]))
    assert source.status_code == 200
    assert "no-store" in source["Cache-Control"]
    assert AuditLog.objects.filter(
        company=batch.company,
        user=user,
        action="import_source_download",
        object_type="ImportBatch",
        object_id=str(batch.pk),
        new_data_json__file_sha256=batch.file_sha256,
    ).exists()


def test_import_preview_rejects_unbounded_or_unknown_pagination_inputs(
    client, settings, tmp_path
):
    user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    client.force_login(user)
    url = reverse("imports:batch_detail", args=[batch.pk])

    assert client.get(url, {"page": "999999"}).status_code == 400
    assert client.get(url, {"page_size": "10000"}).status_code == 400


def test_equipment_cannot_read_another_users_item_master_import_evidence(
    client, settings, tmp_path
):
    uploader, batch = _import_batch(
        settings=settings,
        tmp_path=tmp_path,
        row_count=1,
        import_type=ImportBatch.ImportType.ITEM_MASTER,
        template_version="item-master-v1",
    )
    equipment = get_user_model().objects.create_user(
        username="platform-equipment",
        password="Valid-Password-2026!",
        display_name="设备人员",
    )
    role, _created = Group.objects.get_or_create(name="equipment")
    equipment.groups.add(role)
    client.force_login(equipment)

    detail = client.get(reverse("imports:batch_detail", args=[batch.pk]))
    source = client.get(reverse("imports:source", args=[batch.pk]))
    assert detail.status_code == 403
    assert source.status_code == 403
    assert batch.uploaded_by_id == uploader.pk


def test_supply_audit_object_types_are_filterable_in_the_read_only_log(
    client, settings, tmp_path
):
    user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    AuditLog.objects.create(
        company=batch.company,
        user=user,
        action="supply_document_post",
        object_type="SupplyDocument",
        object_id="supply-document-1",
        old_data_json={"status": "draft"},
        new_data_json={"status": "posted"},
    )
    client.force_login(user)

    response = client.get(
        reverse("audit:log-list"), {"object_type": "SupplyDocument"}
    )
    assert response.status_code == 200
    assert "SupplyDocument" in AUDIT_OBJECT_TYPE_REGISTRY
    assert "supply-document-1" in response.content.decode()


def test_employee_supply_export_is_reauthorized_after_employee_link_revoked(
    settings, tmp_path
):
    user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    user.groups.clear()
    role, _created = Group.objects.get_or_create(name="employee")
    user.groups.add(role)
    department = Department.objects.create(
        company=batch.company,
        code="D-EMP",
        normalized_code="d-emp",
        name="员工部门",
    )
    employee = Employee.objects.create(
        company=batch.company,
        employee_no="E-PLATFORM",
        normalized_employee_no="e-platform",
        name="平台员工",
        department=department,
        user=user,
        employment_status="active",
        is_active=True,
    )
    export_log = SimpleNamespace(
        company_id=batch.company_id,
        export_type="supply_custody_balance",
        filters_json={
            "_cost_columns": [],
            "_authorized_employee_ids": [str(employee.pk)],
        },
        requested_by_id=user.pk,
        status="completed",
    )

    assert can_view_export(user, export_log)
    employee.user = None
    employee.save(update_fields=["user"])
    assert not can_view_export(user, export_log)


def _completed_backup(*, company, user, settings, tmp_path):
    from apps.operations.models import BackupSet

    settings.BACKUP_ROOT = tmp_path / "backup-stage"
    settings.BACKUP_MIRROR_ROOT = tmp_path / "backup-mirror"
    settings.BACKUP_TEMP_ROOT = tmp_path / "backup-temp"
    for root in (
        settings.BACKUP_ROOT,
        settings.BACKUP_MIRROR_ROOT,
        settings.BACKUP_TEMP_ROOT,
    ):
        root.mkdir(parents=True, exist_ok=True)
    backup_id = uuid.uuid4()
    storage_key = f"backups/{backup_id}/{backup_id}.eambak"
    content = b"encrypted backup package"
    digest = hashlib.sha256(content).hexdigest()
    now = timezone.now()
    backup = BackupSet(
        id=backup_id,
        company=company,
        backup_set_id=f"BKP-PLATFORM-{str(backup_id)[:8]}",
        kind=BackupSet.Kind.MANUAL,
        status=BackupSet.Status.PENDING,
        request_hash="b" * 64,
        idempotency_key=f"platform-expiry-{backup_id}",
        requested_by=user,
        started_at=now - timedelta(days=31),
    )
    backup.full_clean()
    backup.save(force_insert=True)
    BackupSet._base_manager.filter(pk=backup.pk).update(
        status=BackupSet.Status.COMPLETED,
        storage_key=storage_key,
        package_sha256=digest,
        package_size=len(content),
        manifest_json={"format": "eam-lite-backup-v1"},
        data_snapshot_at=now - timedelta(days=31),
        finished_at=now - timedelta(days=31),
        expires_at=now - timedelta(seconds=1),
    )
    backup.refresh_from_db()
    paths = []
    for root in (settings.BACKUP_ROOT, settings.BACKUP_MIRROR_ROOT):
        path = root / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        paths.append(path)
    return backup, tuple(paths)


def _configure_fake_backup_runtime(*, settings, tmp_path, monkeypatch, services):
    settings.BACKUP_ROOT = tmp_path / "backup-stage"
    settings.BACKUP_MIRROR_ROOT = tmp_path / "backup-mirror"
    settings.BACKUP_TEMP_ROOT = tmp_path / "backup-temp"
    settings.BACKUP_KEY_FILE = tmp_path / "backup-key.txt"
    settings.APP_COMMIT_SHA = "platform-test"
    for root in (
        settings.BACKUP_ROOT,
        settings.BACKUP_MIRROR_ROOT,
        settings.BACKUP_TEMP_ROOT,
    ):
        root.mkdir(parents=True, exist_ok=True)
    settings.BACKUP_KEY_FILE.write_text(
        "automatic-backup-test-passphrase", encoding="utf-8"
    )
    monkeypatch.setattr(services.connection, "vendor", "postgresql")

    def fake_dump(destination):
        destination.write_bytes(b"database dump")

    def fake_media(destination):
        destination.write_bytes(b"media archive")
        return [], 0

    def fake_encrypt(source, destination, *, passphrase):
        assert passphrase == "automatic-backup-test-passphrase"
        shutil.copyfile(source, destination)

    monkeypatch.setattr(services, "_run_pg_dump", fake_dump)
    monkeypatch.setattr(services, "_archive_media", fake_media)
    monkeypatch.setattr(
        services,
        "_migration_snapshot",
        lambda: (["operations.0002_postgresql_backup_guards"], "18.6"),
    )
    monkeypatch.setattr(services, "encrypt_file", fake_encrypt)


def test_backup_expiry_restores_both_copies_when_audit_transaction_fails(
    settings, tmp_path, monkeypatch
):
    from apps.operations import services as backup_services
    from apps.operations.models import BackupSet

    user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    backup, paths = _completed_backup(
        company=batch.company,
        user=user,
        settings=settings,
        tmp_path=tmp_path,
    )

    def fail_audit(**kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(backup_services, "write_business_audit_log", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        backup_services.expire_due_backups(as_of=timezone.now())

    backup.refresh_from_db()
    assert backup.status == BackupSet.Status.COMPLETED
    assert backup.storage_key
    assert all(path.read_bytes() == b"encrypted backup package" for path in paths)
    assert not any(tmp_path.rglob("*.expiring-*"))


def test_backup_publish_failure_discards_primary_mirror_and_staging_files(
    settings, tmp_path, monkeypatch
):
    from apps.operations import services as backup_services
    from apps.operations.models import BackupSet

    user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    _configure_fake_backup_runtime(
        settings=settings,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        services=backup_services,
    )
    original_audit = backup_services.write_business_audit_log

    def fail_completion_audit(**kwargs):
        if kwargs.get("action") == "backup.completed":
            raise RuntimeError("injected publication audit failure")
        return original_audit(**kwargs)

    monkeypatch.setattr(
        backup_services,
        "write_business_audit_log",
        fail_completion_audit,
    )
    with pytest.raises(ValidationError, match="备份生成失败"):
        backup_services.create_backup_set(
            actor=None,
            company=batch.company,
            kind=BackupSet.Kind.AUTOMATIC,
            idempotency_key="platform-publication-failure",
        )

    backup = BackupSet.objects.get(idempotency_key="platform-publication-failure")
    assert backup.status == BackupSet.Status.FAILED
    assert not list(settings.BACKUP_ROOT.rglob("*.eambak"))
    assert not list(settings.BACKUP_MIRROR_ROOT.rglob("*.eambak"))
    assert not list(settings.BACKUP_ROOT.rglob("*.tmp"))
    assert not list(settings.BACKUP_MIRROR_ROOT.rglob("*.tmp"))


def test_post_commit_result_query_failure_preserves_and_returns_completed_backup(
    settings, tmp_path, monkeypatch
):
    from apps.operations import services as backup_services
    from apps.operations.models import BackupSet

    _user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    _configure_fake_backup_runtime(
        settings=settings,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        services=backup_services,
    )

    def fail_result_query(*args, **kwargs):
        raise RuntimeError("injected post-commit result query failure")

    monkeypatch.setattr(BackupSet.objects, "get", fail_result_query)
    backup = backup_services.create_backup_set(
        actor=None,
        company=batch.company,
        kind=BackupSet.Kind.AUTOMATIC,
        idempotency_key="platform-post-commit-query",
    )

    assert backup.status == BackupSet.Status.COMPLETED
    assert (settings.BACKUP_ROOT / backup.storage_key).is_file()
    assert (settings.BACKUP_MIRROR_ROOT / backup.storage_key).is_file()


def test_uncertain_commit_state_preserves_all_published_backup_copies(
    settings, tmp_path, monkeypatch
):
    from apps.operations import services as backup_services
    from apps.operations.models import BackupSet

    _user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    _configure_fake_backup_runtime(
        settings=settings,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        services=backup_services,
    )

    def fail_result_query(*args, **kwargs):
        raise RuntimeError("injected ambiguous commit response")

    monkeypatch.setattr(BackupSet.objects, "get", fail_result_query)
    monkeypatch.setattr(
        backup_services,
        "_recover_backup_after_exception",
        lambda **kwargs: (None, False),
    )
    with pytest.raises(ValidationError, match="备份生成失败"):
        backup_services.create_backup_set(
            actor=None,
            company=batch.company,
            kind=BackupSet.Kind.AUTOMATIC,
            idempotency_key="platform-ambiguous-commit",
        )

    backup = BackupSet._base_manager.get(
        idempotency_key="platform-ambiguous-commit"
    )
    assert backup.status == BackupSet.Status.COMPLETED
    assert (settings.BACKUP_ROOT / backup.storage_key).is_file()
    assert (settings.BACKUP_MIRROR_ROOT / backup.storage_key).is_file()


@pytest.mark.django_db(transaction=True)
def test_backup_expiry_waits_for_started_download_lease_under_lock(
    settings, tmp_path, monkeypatch
):
    if connection.vendor != "postgresql":
        pytest.skip("BackupSet-to-Grant lock ordering requires PostgreSQL")
    from apps.operations import services as backup_services
    from apps.operations.models import BackupDownloadGrant, BackupSet

    user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    backup, paths = _completed_backup(
        company=batch.company,
        user=user,
        settings=settings,
        tmp_path=tmp_path,
    )
    grant = backup_services.issue_download_grant(
        actor=user,
        backup_set=backup,
        idempotency_key="platform-active-download-lease",
    )
    original_package_path = backup_services.backup_package_path
    start_holds_locks = threading.Event()
    release_start = threading.Event()
    expiry_attempted = threading.Event()
    outcomes = {}

    def paused_package_path(backup_set):
        if threading.current_thread().name == "backup-download-start":
            start_holds_locks.set()
            if not release_start.wait(timeout=10):
                raise RuntimeError("test did not release download start")
        return original_package_path(backup_set)

    monkeypatch.setattr(
        backup_services,
        "backup_package_path",
        paused_package_path,
    )

    def start_worker():
        close_old_connections()
        try:
            actor = get_user_model().objects.get(pk=user.pk)
            worker_grant = BackupDownloadGrant._base_manager.get(pk=grant.pk)
            outcomes["started"] = backup_services.start_download_grant(
                actor=actor,
                grant=worker_grant,
            )[0].pk
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes["start_error"] = exc
        finally:
            close_old_connections()

    def expiry_worker():
        close_old_connections()
        try:
            expiry_attempted.set()
            outcomes["expired"] = backup_services.expire_due_backups(
                as_of=timezone.now()
            )
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes["expiry_error"] = exc
        finally:
            close_old_connections()

    start_thread = threading.Thread(
        target=start_worker,
        name="backup-download-start",
    )
    start_thread.start()
    assert start_holds_locks.wait(timeout=10)
    expiry_thread = threading.Thread(target=expiry_worker, name="backup-expiry")
    expiry_thread.start()
    assert expiry_attempted.wait(timeout=10)
    release_start.set()
    start_thread.join(timeout=15)
    expiry_thread.join(timeout=15)

    assert not start_thread.is_alive() and not expiry_thread.is_alive()
    assert "start_error" not in outcomes and "expiry_error" not in outcomes
    assert outcomes["expired"] == []
    backup.refresh_from_db()
    grant.refresh_from_db()
    assert backup.status == BackupSet.Status.COMPLETED
    assert grant.status == BackupDownloadGrant.Status.STARTED
    assert all(path.is_file() for path in paths)
    backup_services.finish_download_grant(grant_id=grant.pk, succeeded=True)


@pytest.mark.django_db(transaction=True)
def test_expiry_marks_stale_started_lease_failed_before_removing_backup(
    settings, tmp_path
):
    if connection.vendor != "postgresql":
        pytest.skip("Stale download lease transition requires PostgreSQL")
    from apps.operations import services as backup_services
    from apps.operations.models import BackupDownloadGrant, BackupSet

    user, batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    backup, paths = _completed_backup(
        company=batch.company,
        user=user,
        settings=settings,
        tmp_path=tmp_path,
    )
    grant = backup_services.issue_download_grant(
        actor=user,
        backup_set=backup,
        idempotency_key="platform-stale-download-lease",
    )
    grant, _package = backup_services.start_download_grant(
        actor=user,
        grant=grant,
    )
    stale_boundary = grant.expires_at + timedelta(
        minutes=settings.BACKUP_DOWNLOAD_GRANT_MINUTES,
        seconds=1,
    )

    assert backup_services.expire_due_backups(as_of=stale_boundary) == [backup.pk]
    backup.refresh_from_db()
    grant.refresh_from_db()
    assert backup.status == BackupSet.Status.EXPIRED
    assert grant.status == BackupDownloadGrant.Status.FAILED
    assert grant.finished_at == stale_boundary
    assert all(not path.exists() for path in paths)
    assert AuditLog.objects.filter(
        action="backup.download_failed",
        object_type="BackupDownloadGrant",
        object_id=str(grant.pk),
        new_data_json__reason="stale_started_lease",
    ).exists()


def test_login_audit_failure_rolls_back_last_login_and_authenticated_session(
    client, settings, tmp_path, monkeypatch
):
    from apps.audit import signals as audit_signals

    user, _batch = _import_batch(settings=settings, tmp_path=tmp_path, row_count=1)
    assert user.last_login is None

    def fail_login_audit(**kwargs):
        raise RuntimeError("injected login audit failure")

    monkeypatch.setattr(
        audit_signals,
        "write_system_audit_log",
        fail_login_audit,
    )
    client.raise_request_exception = False
    response = client.post(
        reverse("login"),
        {"username": user.username, "password": "Valid-Password-2026!"},
    )
    assert response.status_code == 500

    user.refresh_from_db()
    assert user.last_login is None
    assert "_auth_user_id" not in client.session
    assert client.get(reverse("settings-center")).status_code == 302


@pytest.mark.parametrize("unsafe_member", ["../escape.xml", "/absolute.xml"])
def test_xlsx_container_rejects_unsafe_internal_paths(unsafe_member):
    source = io.BytesIO(build_template_workbook("department"))
    output = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(
        output, "w", zipfile.ZIP_DEFLATED
    ) as modified:
        for item in original.infolist():
            modified.writestr(item, original.read(item))
        modified.writestr(unsafe_member, b"<unsafe/>")

    with pytest.raises(ValidationError, match="不安全的内部路径"):
        _validate_xlsx_container(output.getvalue())


def test_xlsx_container_rejects_duplicate_internal_names():
    source = io.BytesIO(build_template_workbook("department"))
    output = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(
        output, "w", zipfile.ZIP_DEFLATED
    ) as modified:
        for item in original.infolist():
            modified.writestr(item, original.read(item))
        with pytest.warns(UserWarning, match="Duplicate name"):
            modified.writestr(
                "[Content_Types].xml",
                original.read("[Content_Types].xml"),
            )

    with pytest.raises(ValidationError, match="重复的内部文件名"):
        _validate_xlsx_container(output.getvalue())
