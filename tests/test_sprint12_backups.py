from __future__ import annotations

from datetime import timedelta
import io
import json
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone
from django.test import Client

from apps.audit.models import AuditLog
from apps.operations.crypto import decrypt_file, encrypt_file, encryption_metadata
from apps.operations.models import BackupDownloadGrant, BackupSet
from apps.operations.services import (
    create_backup_set,
    expire_due_backups,
    issue_download_grant,
    start_download_grant,
    current_database_name,
    restore_backup_package_to_isolated,
    verify_backup_set,
)
from tests.test_sprint1_services import PASSWORD, make_user
from tests.test_sprint4_acceptance import _base_context


pytestmark = pytest.mark.django_db(transaction=True)
requires_postgresql_backup = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="正式数据库与附件一致性备份只支持 PostgreSQL。",
)


def _configure_backup_settings(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.BACKUP_ROOT = tmp_path / "backups"
    settings.BACKUP_TEMP_ROOT = tmp_path / "tmp"
    settings.BACKUP_MIRROR_ROOT = tmp_path / "mirror"
    settings.BACKUP_RETENTION_DAYS = 30
    settings.BACKUP_DOWNLOAD_GRANT_MINUTES = 10
    settings.APP_COMMIT_SHA = "test-commit"
    for path in (
        settings.MEDIA_ROOT,
        settings.BACKUP_ROOT,
        settings.BACKUP_TEMP_ROOT,
        settings.BACKUP_MIRROR_ROOT,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _mark_recently_authenticated(user):
    user.last_login = timezone.now()
    user.save(update_fields=("last_login",))


def _fake_external_tools(monkeypatch):
    def fake_dump(path):
        path.write_bytes(b"FAKE-POSTGRES-CUSTOM-DUMP")

    monkeypatch.setattr("apps.operations.services._run_pg_dump", fake_dump)
    monkeypatch.setattr("apps.operations.services._validate_dump", lambda path: None)


def test_backup_crypto_round_trip_and_wrong_passphrase_fails(tmp_path):
    source = tmp_path / "source.bin"
    encrypted = tmp_path / "backup.eambak"
    restored = tmp_path / "restored.bin"
    source.write_bytes(b"EAM-Lite\x00backup" * 1000)
    metadata = encrypt_file(source, encrypted, passphrase="correct-backup-passphrase")
    assert encryption_metadata(encrypted) == metadata
    decrypt_file(encrypted, restored, passphrase="correct-backup-passphrase")
    assert restored.read_bytes() == source.read_bytes()
    with pytest.raises(Exception):
        decrypt_file(
            encrypted,
            tmp_path / "wrong.bin",
            passphrase="incorrect-passphrase",
        )


@requires_postgresql_backup
def test_manual_backup_is_encrypted_mirrored_verified_idempotent_and_expired(
    settings, tmp_path, monkeypatch
):
    context = _base_context("S12BKP")
    _mark_recently_authenticated(context["admin"])
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    media_file = settings.MEDIA_ROOT / "private" / "asset-photo.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"image-bytes")

    backup = create_backup_set(
        actor=context["admin"],
        company=context["company"],
        kind=BackupSet.Kind.MANUAL,
        idempotency_key="S12BKP-manual",
        passphrase="manual-backup-passphrase",
    )
    assert backup.status == BackupSet.Status.COMPLETED
    assert backup.package_sha256 and backup.package_size > 0
    primary = settings.BACKUP_ROOT / backup.storage_key
    mirror = settings.BACKUP_MIRROR_ROOT / backup.storage_key
    assert primary.read_bytes() == mirror.read_bytes()
    manifest = verify_backup_set(
        backup, passphrase="manual-backup-passphrase"
    )
    assert manifest["media"]["file_count"] == 1
    assert manifest["package_format_version"] == 1
    assert manifest["application_version"]
    assert manifest["application_commit"] == "test-commit"
    assert manifest["encryption"] == encryption_metadata(primary)
    assert manifest["record_counts"]["companies"] == 1
    assert manifest["record_counts"]["assets"] == 0
    repeated = create_backup_set(
        actor=context["admin"],
        company=context["company"],
        kind=BackupSet.Kind.MANUAL,
        idempotency_key="S12BKP-manual",
        passphrase="manual-backup-passphrase",
    )
    assert repeated.pk == backup.pk
    assert AuditLog.objects.filter(
        object_type="BackupSet", object_id=str(backup.pk), action="backup.completed"
    ).exists()

    assert expire_due_backups(as_of=backup.expires_at) == []
    expired = expire_due_backups(as_of=backup.expires_at + timedelta(seconds=1))
    assert expired == [backup.pk]
    backup.refresh_from_db()
    assert backup.status == BackupSet.Status.EXPIRED
    assert not primary.exists() and not mirror.exists()


@requires_postgresql_backup
def test_backup_permissions_and_one_time_download_grant(settings, tmp_path, monkeypatch):
    context = _base_context("S12GRANT")
    _mark_recently_authenticated(context["admin"])
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    backup = create_backup_set(
        actor=context["admin"],
        company=context["company"],
        kind=BackupSet.Kind.MANUAL,
        idempotency_key="S12GRANT-backup",
        passphrase="manual-backup-passphrase",
    )
    with pytest.raises(PermissionDenied):
        issue_download_grant(
            actor=context["equipment"],
            backup_set=backup,
            idempotency_key="S12GRANT-denied",
        )
    grant = issue_download_grant(
        actor=context["admin"],
        backup_set=backup,
        idempotency_key="S12GRANT-ok",
    )
    started, package = start_download_grant(
        actor=context["admin"], grant=grant
    )
    assert started.status == BackupDownloadGrant.Status.STARTED
    assert package.is_file()
    with pytest.raises(ValidationError, match="已使用或已过期"):
        start_download_grant(actor=context["admin"], grant=grant)


@requires_postgresql_backup
def test_automatic_backup_missing_key_fails_closed_and_is_audited(
    settings, tmp_path, monkeypatch
):
    context = _base_context("S12FAIL")
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    settings.BACKUP_KEY_FILE = tmp_path / "missing-key.txt"
    with pytest.raises(ValidationError, match="密钥文件不存在"):
        create_backup_set(
            actor=None,
            company=context["company"],
            kind=BackupSet.Kind.AUTOMATIC,
            idempotency_key="S12FAIL-auto",
        )
    backup = BackupSet.objects.get(idempotency_key="S12FAIL-auto")
    assert backup.status == BackupSet.Status.FAILED
    assert not backup.storage_key and backup.error_summary
    assert AuditLog.objects.filter(
        action="backup.failed", object_id=str(backup.pk)
    ).exists()


@requires_postgresql_backup
def test_portable_console_command_exports_verified_file_without_actor_password(
    settings, tmp_path, monkeypatch
):
    _base_context("S12PORTABLE")
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    settings.EAM_ENVIRONMENT = "local"
    settings.APP_VERSION = "0.2.1-test"
    settings.BUILD_TIME = "2026-09-01T00:00:00Z"
    passphrase_file = tmp_path / "migration-passphrase.txt"
    passphrase_file.write_text("portable-migration-passphrase", encoding="utf-8")
    output_dir = tmp_path / "便携 输出"
    stdout = io.StringIO()

    call_command(
        "create_eam_backup",
        portable_output_dir=str(output_dir),
        passphrase_file=str(passphrase_file),
        stdout=stdout,
    )

    line = next(
        row for row in stdout.getvalue().splitlines() if "PORTABLE_BACKUP_JSON=" in row
    )
    summary = json.loads(line.split("PORTABLE_BACKUP_JSON=", 1)[1])
    package = Path(summary["path"])
    assert package.is_file()
    assert package.parent == output_dir.resolve()
    assert package.suffix == ".eambak"
    assert summary["version"] == "0.2.1-test"
    backup = BackupSet.objects.get(backup_set_id=summary["backup_set_id"])
    assert verify_backup_set(
        backup, passphrase="portable-migration-passphrase"
    )["record_counts"] == summary["record_counts"]


@requires_postgresql_backup
def test_current_local_restore_refuses_nonempty_database_without_overwrite(
    settings, tmp_path, monkeypatch
):
    context = _base_context("S12RESTOREEMPTY")
    _mark_recently_authenticated(context["admin"])
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    settings.EAM_ENVIRONMENT = "local"
    backup = create_backup_set(
        actor=context["admin"],
        company=context["company"],
        kind=BackupSet.Kind.MANUAL,
        idempotency_key="S12RESTOREEMPTY-backup",
        passphrase="portable-migration-passphrase",
    )

    with pytest.raises(ValidationError, match="目标数据库不是空库"):
        restore_backup_package_to_isolated(
            package_path=settings.BACKUP_ROOT / backup.storage_key,
            passphrase="portable-migration-passphrase",
            target_database=current_database_name(),
            target_media_root=settings.MEDIA_ROOT,
            expected_sha256=backup.package_sha256,
            allow_current_empty=True,
        )

    assert BackupSet.objects.filter(pk=backup.pk).exists()


@requires_postgresql_backup
def test_retention_never_removes_the_only_successful_automatic_backup(
    settings, tmp_path, monkeypatch
):
    context = _base_context("S12KEEP")
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    key_file = tmp_path / "automatic-key.txt"
    key_file.write_text("automatic-backup-passphrase", encoding="utf-8")
    settings.BACKUP_KEY_FILE = key_file
    backup = create_backup_set(
        actor=None,
        company=context["company"],
        kind=BackupSet.Kind.AUTOMATIC,
        idempotency_key="S12KEEP-auto",
    )
    output = io.StringIO()
    call_command("check_eam_backup_health", stdout=output)
    assert "备份健康" in output.getvalue()
    assert expire_due_backups(as_of=backup.expires_at + timedelta(days=365)) == []
    backup.refresh_from_db()
    assert backup.status == BackupSet.Status.COMPLETED


@requires_postgresql_backup
def test_backup_http_is_system_admin_only_reauthenticates_and_never_exposes_path(
    client, settings, tmp_path, monkeypatch
):
    context = _base_context("S12HTTP")
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    client.force_login(context["equipment"])
    assert client.get(reverse("operations:backup-list")).status_code == 403
    client.force_login(context["admin"])
    assert "数据备份" in client.get(reverse("home")).content.decode()
    page = client.get(reverse("operations:backup-create"))
    assert page.status_code == 200
    response = client.post(
        reverse("operations:backup-create"),
        {
            "current_password": PASSWORD,
            "backup_passphrase": "manual-backup-passphrase",
            "backup_passphrase_confirm": "manual-backup-passphrase",
            "idempotency_key": "S12HTTP-create",
        },
    )
    assert response.status_code == 302
    backup = BackupSet.objects.get(idempotency_key="S12HTTP-create")
    detail = client.get(reverse("operations:backup-detail", args=[backup.pk]))
    html = detail.content.decode()
    assert detail.status_code == 200
    assert str(settings.BACKUP_ROOT) not in html
    bad = client.post(
        reverse("operations:backup-authorize-download", args=[backup.pk]),
        {"current_password": "wrong", "idempotency_key": "S12HTTP-bad"},
    )
    assert bad.status_code == 400
    assert AuditLog.objects.filter(
        action="backup.reauthentication_failed", object_id=str(backup.pk)
    ).exists()
    authorized = client.post(
        reverse("operations:backup-authorize-download", args=[backup.pk]),
        {"current_password": PASSWORD, "idempotency_key": "S12HTTP-download"},
    )
    assert authorized.status_code == 200
    grant = BackupDownloadGrant.objects.get(idempotency_key="S12HTTP-download")
    download = client.post(reverse("operations:backup-download", args=[grant.pk]))
    payload = b"".join(download.streaming_content)
    assert download.status_code == 200
    assert payload and payload != b"FAKE-POSTGRES-CUSTOM-DUMP"
    assert "attachment" in download["Content-Disposition"]
    grant.refresh_from_db()
    assert grant.status == BackupDownloadGrant.Status.COMPLETED
    assert AuditLog.objects.filter(
        action="backup.download_completed", object_id=str(grant.pk)
    ).exists()
    interrupted_authorization = client.post(
        reverse("operations:backup-authorize-download", args=[backup.pk]),
        {"current_password": PASSWORD, "idempotency_key": "S12HTTP-interrupted"},
    )
    assert interrupted_authorization.status_code == 200
    interrupted_grant = BackupDownloadGrant.objects.get(
        idempotency_key="S12HTTP-interrupted"
    )
    interrupted_response = client.post(
        reverse("operations:backup-download", args=[interrupted_grant.pk])
    )
    iterator = iter(interrupted_response.streaming_content)
    assert next(iterator)
    interrupted_response.close()
    interrupted_grant.refresh_from_db()
    assert interrupted_grant.status == BackupDownloadGrant.Status.FAILED

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(context["admin"])
    assert csrf_client.post(
        reverse("operations:backup-create"),
        {
            "current_password": PASSWORD,
            "backup_passphrase": "manual-backup-passphrase",
            "backup_passphrase_confirm": "manual-backup-passphrase",
            "idempotency_key": "S12HTTP-csrf",
        },
    ).status_code == 403


def test_postgresql_guards_reject_backup_delete_and_cross_company_grant():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL backup trigger acceptance")
    context = _base_context("S12PG")
    now = timezone.now()
    backup = BackupSet.objects.create(
        company=context["company"],
        backup_set_id="BKP-S12PG",
        kind=BackupSet.Kind.MANUAL,
        status=BackupSet.Status.PENDING,
        request_hash="a" * 64,
        idempotency_key="S12PG-backup",
        requested_by=context["admin"],
        started_at=now,
    )
    with pytest.raises(IntegrityError, match="append-only"):
        with transaction.atomic():
            BackupSet._base_manager.filter(pk=backup.pk).delete()

    from apps.masterdata.models import Company

    other = Company.objects.create(
        code="S12PG-OTHER",
        normalized_code="s12pg-other",
        name="隔离公司",
        short_name="隔离",
        is_active=False,
    )
    with pytest.raises(IntegrityError, match="invalid backup download grant"):
        with transaction.atomic():
            BackupDownloadGrant._base_manager.create(
                company=other,
                backup_set=backup,
                user=context["admin"],
                idempotency_key="S12PG-cross",
                issued_at=now,
                expires_at=now + timedelta(minutes=10),
            )


@requires_postgresql_backup
def test_backup_actor_deletion_preserves_backup_and_download_history(
    settings, tmp_path, monkeypatch
):
    context = _base_context("S12ACTOR")
    actor = make_user("s12-backup-actor", "system_admin")
    _mark_recently_authenticated(actor)
    _configure_backup_settings(settings, tmp_path)
    _fake_external_tools(monkeypatch)
    backup = create_backup_set(
        actor=actor,
        company=context["company"],
        kind=BackupSet.Kind.MANUAL,
        idempotency_key="S12ACTOR-backup",
        passphrase="manual-backup-passphrase",
    )
    grant = issue_download_grant(
        actor=actor,
        backup_set=backup,
        idempotency_key="S12ACTOR-grant",
    )
    actor.delete()
    backup.refresh_from_db()
    grant.refresh_from_db()
    assert backup.requested_by_id is None
    assert grant.user_id is None
    assert backup.status == BackupSet.Status.COMPLETED
    assert grant.status == BackupDownloadGrant.Status.ISSUED


def test_pending_backup_freezes_http_writes_and_stale_recovery_unfreezes(
    client, settings, tmp_path
):
    context = _base_context("S12FREEZE")
    _configure_backup_settings(settings, tmp_path)
    client.force_login(context["admin"])
    pending = BackupSet.objects.create(
        company=context["company"],
        backup_set_id="BKP-S12FREEZE",
        kind=BackupSet.Kind.AUTOMATIC,
        status=BackupSet.Status.PENDING,
        request_hash="b" * 64,
        idempotency_key="S12FREEZE-pending",
        started_at=timezone.now() - timedelta(hours=2),
    )
    frozen = client.post(reverse("operations:backup-create"), {})
    assert frozen.status_code == 503
    assert frozen["Retry-After"] == "60"
    call_command("fail_stale_eam_backups", older_minutes=60, verbosity=0)
    pending.refresh_from_db()
    assert pending.status == BackupSet.Status.FAILED
    unfrozen = client.post(reverse("operations:backup-create"), {})
    assert unfrozen.status_code != 503
    assert AuditLog.objects.filter(
        action="backup.stale_failed", object_id=str(pending.pk)
    ).exists()


def test_restored_snapshot_closes_its_recent_pending_backup(settings):
    context = _base_context("S12RESTOREDPENDING")
    settings.EAM_ENVIRONMENT = "local"
    pending = BackupSet.objects.create(
        company=context["company"],
        backup_set_id="BKP-S12RESTOREDPENDING",
        kind=BackupSet.Kind.MANUAL,
        status=BackupSet.Status.PENDING,
        request_hash="c" * 64,
        idempotency_key="S12RESTOREDPENDING-pending",
        started_at=timezone.now(),
    )

    call_command("fail_stale_eam_backups", restored_snapshot=True, verbosity=0)

    pending.refresh_from_db()
    assert pending.status == BackupSet.Status.FAILED
    assert "已恢复备份" in pending.error_summary
    assert AuditLog.objects.filter(
        action="backup.stale_failed", object_id=str(pending.pk)
    ).exists()


def test_operations_migrations_reverse_and_forward_on_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL migration round trip")
    try:
        call_command("migrate", "operations", "zero", verbosity=0)
        assert "operations_backupset" not in connection.introspection.table_names()
        call_command("migrate", "operations", "0002", verbosity=0)
        assert "operations_backupset" in connection.introspection.table_names()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname IN (%s, %s) AND NOT tgisinternal",
                (
                    "operations_backup_set_guard",
                    "operations_backup_download_grant_guard",
                ),
            )
            assert cursor.fetchone()[0] == 2
    finally:
        call_command("migrate", "operations", "0002", verbosity=0)
