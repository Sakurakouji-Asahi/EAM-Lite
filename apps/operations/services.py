from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path, PurePosixPath

import psycopg
from django.apps import apps
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.models import Company
from apps.operations.crypto import (
    KDF_ITERATIONS,
    SALT_SIZE,
    decrypt_file,
    encrypt_file,
    encryption_metadata,
    sha256_file,
)
from apps.operations.models import BackupDownloadGrant, BackupSet
from apps.operations.permissions import (
    require_manage_backups,
    require_recent_backup_authentication,
)


logger = logging.getLogger(__name__)


_SAFE_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_SAFE_CONTAINER = re.compile(r"^[A-Za-z0-9_.-]+$")
_PACKAGE_MEMBER_NAMES = frozenset(
    {"database.dump", "media.tar.gz", "manifest.json"}
)
_CRITICAL_COUNT_MODELS = {
    "companies": "masterdata.Company",
    "users": "accounts.User",
    "employees": "masterdata.Employee",
    "assets": "assets.Asset",
    "supply_items": "supplies.SupplyItem",
    "supply_balances": "supplies.SupplyStockBalance",
    "supply_ledgers": "supplies.SupplyStockLedger",
    "supply_custodies": "supplies.SupplyCustody",
    "audit_logs": "audit.AuditLog",
    "attachments": "masterdata.Attachment",
}


def _request_hash(payload) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_error(exc) -> str:
    value = str(exc)
    password = str(settings.DATABASES["default"].get("PASSWORD", ""))
    if password:
        value = value.replace(password, "[REDACTED]")
    return value[:1000] or type(exc).__name__


def _storage_path(storage_key: str, *, root=None) -> Path:
    root = Path(root or settings.BACKUP_ROOT).resolve()
    candidate = (root / storage_key).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValidationError("备份存储路径越界。")
    return candidate


def backup_package_path(backup_set: BackupSet) -> Path:
    if backup_set.status != BackupSet.Status.COMPLETED:
        raise ValidationError("只有已完成且未过期的备份可以下载。")
    path = _storage_path(backup_set.storage_key)
    if not path.is_file():
        raise ValidationError("备份文件不存在，请联系系统管理员检查备份存储。")
    if sha256_file(path) != backup_set.package_sha256:
        raise ValidationError("备份文件摘要校验失败，已阻止下载。")
    return path


def _run_checked(command, *, env=None, stdout=None, stdin=None, input_data=None):
    if stdin is not None and input_data is not None:
        raise ValueError("stdin 与 input_data 不能同时提供。")
    result = subprocess.run(
        command,
        check=False,
        env=env,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stdin=stdin,
        input=input_data,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"外部备份命令失败（{result.returncode}）：{stderr}")
    return result


def _pg_mode():
    mode = str(settings.BACKUP_PG_MODE).strip().lower()
    if mode not in {"native", "docker"}:
        raise ValidationError("BACKUP_PG_MODE 只能是 native 或 docker。")
    return mode


def _run_pg_dump(destination: Path):
    db = settings.DATABASES["default"]
    if _pg_mode() == "docker":
        container = str(settings.BACKUP_POSTGRES_CONTAINER)
        if not _SAFE_CONTAINER.fullmatch(container):
            raise ValidationError("PostgreSQL 容器名格式非法。")
        command = [
            str(settings.BACKUP_DOCKER_BIN),
            "exec",
            container,
            "pg_dump",
            "-U",
            str(db["USER"]),
            "-d",
            str(db["NAME"]),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
        ]
        with destination.open("wb") as output:
            _run_checked(command, stdout=output)
    else:
        command = [
            str(settings.BACKUP_PG_DUMP_BIN),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(destination),
            "--host",
            str(db["HOST"]),
            "--port",
            str(db["PORT"]),
            "--username",
            str(db["USER"]),
            str(db["NAME"]),
        ]
        env = os.environ.copy()
        env["PGPASSWORD"] = str(db["PASSWORD"])
        _run_checked(command, env=env)
    _validate_dump(destination)


def _validate_dump(path: Path):
    db = settings.DATABASES["default"]
    if _pg_mode() == "docker":
        command = [
            str(settings.BACKUP_DOCKER_BIN),
            "exec",
            "-i",
            str(settings.BACKUP_POSTGRES_CONTAINER),
            "pg_restore",
            "--list",
        ]
        with path.open("rb") as source:
            _run_checked(command, stdin=source)
    else:
        _run_checked([str(settings.BACKUP_PG_RESTORE_BIN), "--list", str(path)])


def _archive_media(destination: Path):
    root = Path(settings.MEDIA_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    files = []
    total_size = 0
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValidationError(f"附件目录包含不允许的符号链接：{path.name}")
            if not path.is_file():
                continue
            resolved = path.resolve()
            if root not in resolved.parents:
                raise ValidationError("附件路径越界。")
            relative = resolved.relative_to(root).as_posix()
            archive.add(resolved, arcname=relative, recursive=False)
            size = resolved.stat().st_size
            files.append(
                {"path": relative, "size": size, "sha256": sha256_file(resolved)}
            )
            total_size += size
    return files, total_size


def _migration_snapshot():
    with connection.cursor() as cursor:
        cursor.execute("SELECT app, name FROM django_migrations ORDER BY app, name")
        migrations = [f"{app}.{name}" for app, name in cursor.fetchall()]
        cursor.execute("SELECT version()")
        database_version = cursor.fetchone()[0]
    return migrations, database_version


def _critical_record_counts():
    return {
        key: apps.get_model(label)._base_manager.count()
        for key, label in _CRITICAL_COUNT_MODELS.items()
    }


def current_database_name():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        return str(cursor.fetchone()[0])


def _write_manifest(path: Path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _build_bundle(destination: Path, *, dump_path: Path, media_path: Path, manifest_path: Path):
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as bundle:
        bundle.add(dump_path, arcname="database.dump", recursive=False)
        bundle.add(media_path, arcname="media.tar.gz", recursive=False)
        bundle.add(manifest_path, arcname="manifest.json", recursive=False)


def _automatic_passphrase() -> str:
    key_file = Path(settings.BACKUP_KEY_FILE)
    if not key_file.is_file():
        raise ValidationError("自动备份密钥文件不存在。")
    value = key_file.read_text(encoding="utf-8").strip()
    if len(value) < 12:
        raise ValidationError("自动备份密钥文件内容至少需要 12 个字符。")
    return value


def _publish_package(temp_package: Path, storage_key: str):
    final_path = _storage_path(storage_key)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    staging = final_path.with_suffix(final_path.suffix + ".tmp")
    mirror_staging = None
    try:
        shutil.copyfile(temp_package, staging)
        os.replace(staging, final_path)
        try:
            os.chmod(final_path.parent, 0o700)
            os.chmod(final_path, 0o600)
        except OSError:
            pass
        mirror_root = settings.BACKUP_MIRROR_ROOT
        if mirror_root:
            mirror_path = _storage_path(storage_key, root=mirror_root)
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            mirror_staging = mirror_path.with_suffix(mirror_path.suffix + ".tmp")
            shutil.copyfile(final_path, mirror_staging)
            if sha256_file(mirror_staging) != sha256_file(final_path):
                raise ValidationError("备份镜像副本摘要校验失败。")
            os.replace(mirror_staging, mirror_path)
            try:
                os.chmod(mirror_path.parent, 0o700)
                os.chmod(mirror_path, 0o600)
            except OSError:
                pass
        return final_path
    except Exception:
        _discard_published_package(storage_key)
        raise
    finally:
        staging.unlink(missing_ok=True)
        if mirror_staging is not None:
            mirror_staging.unlink(missing_ok=True)


def _published_package_paths(storage_key: str):
    paths = [_storage_path(storage_key)]
    if settings.BACKUP_MIRROR_ROOT:
        paths.append(_storage_path(storage_key, root=settings.BACKUP_MIRROR_ROOT))
    return tuple(paths)


def _discard_published_package(storage_key: str):
    """Best-effort removal for files that have no committed BackupSet owner."""

    for path in _published_package_paths(storage_key):
        for candidate in (path, path.with_suffix(path.suffix + ".tmp")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "无法清理未发布备份文件 storage_key_hash=%s",
                    hashlib.sha256(storage_key.encode("utf-8")).hexdigest(),
                )


def _quarantine_package_files(storage_key: str):
    """Atomically move existing copies aside, restoring all on partial failure."""

    token = uuid.uuid4().hex
    moved = []
    try:
        for original in _published_package_paths(storage_key):
            if not original.exists():
                continue
            quarantined = original.with_name(f"{original.name}.expiring-{token}")
            os.replace(original, quarantined)
            moved.append((original, quarantined))
    except Exception:
        _restore_quarantined_package_files(moved)
        raise
    return moved


def _restore_quarantined_package_files(moved):
    for original, quarantined in reversed(tuple(moved)):
        if quarantined.exists():
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(quarantined, original)


def _delete_quarantined_package_files(moved):
    for _original, quarantined in moved:
        try:
            quarantined.unlink(missing_ok=True)
        except OSError:
            logger.exception("已过期备份隔离文件删除失败：%s", quarantined.name)


def _recover_backup_after_exception(*, backup, actor, exc, context, finished_at):
    """Resolve durable state before deciding whether published files are orphaned.

    A database commit can succeed even when the client receives an exception.
    Therefore an unknown state is deliberately different from ``pending``:
    only a PENDING -> FAILED transition committed by this function authorizes
    deletion of the primary and mirror files.
    """

    try:
        with transaction.atomic():
            locked = BackupSet._base_manager.select_for_update().get(pk=backup.pk)
            if locked.status == BackupSet.Status.COMPLETED:
                return locked, False
            if locked.status != BackupSet.Status.PENDING:
                return locked, False
            summary = _safe_error(exc)
            BackupSet._base_manager.filter(pk=locked.pk).update(
                status=BackupSet.Status.FAILED,
                finished_at=finished_at,
                error_summary=summary,
            )
            write_business_audit_log(
                company=locked.company,
                user=actor,
                action="backup.failed",
                object_type="BackupSet",
                object_id=locked.pk,
                old_data={"status": BackupSet.Status.PENDING},
                new_data={"status": BackupSet.Status.FAILED, "error": summary},
                **context,
            )
            locked.status = BackupSet.Status.FAILED
            locked.finished_at = finished_at
            locked.error_summary = summary
        # Reaching this line proves both the transition and its audit committed.
        return locked, True
    except Exception:
        logger.exception(
            "备份异常后的数据库状态无法确认 backup_id=%s；保留所有已发布文件",
            backup.pk,
        )
        return None, False


def create_backup_set(
    *,
    actor,
    company,
    kind,
    idempotency_key,
    passphrase=None,
    request=None,
    local_console=False,
):
    if connection.vendor != "postgresql":
        raise ValidationError("正式备份只支持 PostgreSQL。")
    if kind not in BackupSet.Kind.values:
        raise ValidationError("未知备份类型。")
    if kind == BackupSet.Kind.MANUAL:
        if local_console:
            if actor is not None or settings.EAM_ENVIRONMENT not in {
                "local",
                "development",
            }:
                raise PermissionDenied(
                    "本机控制台备份只能在 local/development 环境执行。"
                )
        else:
            require_recent_backup_authentication(actor)
    elif actor is not None:
        require_manage_backups(actor)
    if not str(idempotency_key or "").strip():
        raise ValidationError("幂等键不能为空。")
    payload_hash = _request_hash({"kind": kind})
    context = request_audit_context(request)
    now = timezone.now()

    with transaction.atomic():
        locked_company = Company.objects.select_for_update().get(pk=company.pk)
        existing = BackupSet.objects.filter(
            company=locked_company, idempotency_key=idempotency_key
        ).first()
        if existing:
            if existing.request_hash != payload_hash or existing.kind != kind:
                raise ValidationError("相同幂等键对应不同备份请求。")
            return existing
        if BackupSet.objects.filter(
            company=locked_company, status=BackupSet.Status.PENDING
        ).exists():
            raise ValidationError("当前已有备份正在生成，请稍后重试。")
        backup_id = str(uuid.uuid4())
        backup = BackupSet(
            id=uuid.UUID(backup_id),
            company=locked_company,
            backup_set_id=f"BKP-{now.astimezone().strftime('%Y%m%d%H%M%S')}-{backup_id[:8].upper()}",
            kind=kind,
            status=BackupSet.Status.PENDING,
            request_hash=payload_hash,
            idempotency_key=str(idempotency_key).strip(),
            requested_by=actor,
            started_at=now,
        )
        backup.full_clean()
        backup.save()
        write_business_audit_log(
            company=locked_company,
            user=actor,
            action="backup.requested",
            object_type="BackupSet",
            object_id=backup.pk,
            old_data={},
            new_data={"kind": kind, "status": backup.status},
            **context,
        )

    work_dir = None
    try:
        passphrase = (
            passphrase
            if kind == BackupSet.Kind.MANUAL
            else _automatic_passphrase()
        )
        if not isinstance(passphrase, str) or len(passphrase) < 12:
            raise ValidationError("备份加密口令至少需要 12 个字符。")
        temp_root = Path(settings.BACKUP_TEMP_ROOT)
        temp_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="eam-backup-", dir=temp_root))
        dump_path = work_dir / "database.dump"
        media_path = work_dir / "media.tar.gz"
        manifest_path = work_dir / "manifest.json"
        bundle_path = work_dir / "bundle.tar"
        encrypted_path = work_dir / "package.eambak"
        snapshot_at = timezone.now()
        _run_pg_dump(dump_path)
        media_files, media_size = _archive_media(media_path)
        migrations, database_version = _migration_snapshot()
        record_counts = _critical_record_counts()
        encryption_salt = os.urandom(SALT_SIZE)
        encryption = {
            "format": "EAMLITEBK1",
            "cipher": "AES-256-GCM",
            "kdf": "PBKDF2-HMAC-SHA256",
            "iterations": KDF_ITERATIONS,
            "salt": base64.b64encode(encryption_salt).decode("ascii"),
            "salt_bytes": SALT_SIZE,
        }
        manifest = {
            "format": "eam-lite-backup-v1",
            "package_format_version": 1,
            "backup_set_id": backup.backup_set_id,
            "company_id": str(company.pk),
            "created_at": snapshot_at.isoformat(),
            "business_timezone": "Asia/Shanghai",
            "application_version": str(settings.APP_VERSION),
            "application_commit": str(settings.APP_COMMIT_SHA),
            "build_time": str(settings.BUILD_TIME),
            "database_vendor": connection.vendor,
            "database_version": database_version,
            "migrations": migrations,
            "record_counts": record_counts,
            "encryption": encryption,
            "database": {
                "filename": "database.dump",
                "size": dump_path.stat().st_size,
                "sha256": sha256_file(dump_path),
            },
            "media": {
                "filename": "media.tar.gz",
                "file_count": len(media_files),
                "source_bytes": media_size,
                "archive_size": media_path.stat().st_size,
                "sha256": sha256_file(media_path),
                "files_manifest_sha256": hashlib.sha256(
                    json.dumps(
                        media_files,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "files": media_files,
            },
        }
        _write_manifest(manifest_path, manifest)
        _build_bundle(
            bundle_path,
            dump_path=dump_path,
            media_path=media_path,
            manifest_path=manifest_path,
        )
        encrypt_file(
            bundle_path,
            encrypted_path,
            passphrase=passphrase,
            salt=encryption_salt,
            iterations=KDF_ITERATIONS,
        )
        storage_key = f"backups/{backup.pk}/{backup.pk}.eambak"
        final_path = _publish_package(encrypted_path, storage_key)
        package_sha = sha256_file(final_path)
        manifest_summary = json.loads(json.dumps(manifest))
        manifest_summary["media"].pop("files", None)
        finished_at = timezone.now()
        expires_at = finished_at + timedelta(days=settings.BACKUP_RETENTION_DAYS)
        with transaction.atomic():
            locked = BackupSet._base_manager.select_for_update().get(pk=backup.pk)
            if locked.status != BackupSet.Status.PENDING:
                raise ValidationError("备份状态已被并发修改。")
            BackupSet._base_manager.filter(pk=locked.pk).update(
                status=BackupSet.Status.COMPLETED,
                storage_key=storage_key,
                package_sha256=package_sha,
                package_size=final_path.stat().st_size,
                manifest_json=manifest_summary,
                data_snapshot_at=snapshot_at,
                finished_at=finished_at,
                expires_at=expires_at,
                error_summary="",
            )
            write_business_audit_log(
                company=company,
                user=actor,
                action="backup.completed",
                object_type="BackupSet",
                object_id=backup.pk,
                old_data={"status": BackupSet.Status.PENDING},
                new_data={
                    "status": BackupSet.Status.COMPLETED,
                    "package_sha256": package_sha,
                    "package_size": final_path.stat().st_size,
                    "data_snapshot_at": snapshot_at,
                },
                **context,
            )
        return BackupSet.objects.get(pk=backup.pk)
    except Exception as exc:
        final_key = f"backups/{backup.pk}/{backup.pk}.eambak"
        finished_at = timezone.now()
        recovered, failed_committed = _recover_backup_after_exception(
            backup=backup,
            actor=actor,
            exc=exc,
            context=context,
            finished_at=finished_at,
        )
        if failed_committed:
            _discard_published_package(final_key)
        elif recovered is not None and recovered.status == BackupSet.Status.COMPLETED:
            # The publication committed; only the post-commit response/query
            # failed. Return the durable result without deleting its files.
            return recovered
        if isinstance(exc, (ValidationError, PermissionDenied)):
            raise
        raise ValidationError(f"备份生成失败：{_safe_error(exc)}") from exc
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


def _safe_tar_members(archive: tarfile.TarFile, allowed=None):
    members = archive.getmembers()
    if allowed is not None and {member.name for member in members} != set(allowed):
        raise ValidationError("备份包成员不完整或包含未知文件。")
    for member in members:
        path = PurePosixPath(member.name)
        if member.issym() or member.islnk() or path.is_absolute() or ".." in path.parts:
            raise ValidationError("备份包包含不安全路径或链接。")
    return members


def verify_backup_package(
    package_path, *, passphrase: str, expected_sha256=None, expected_backup_set_id=None
):
    package = Path(package_path).resolve()
    if not package.is_file():
        raise ValidationError("备份包文件不存在。")
    if expected_sha256 and sha256_file(package) != expected_sha256:
        raise ValidationError("备份包外层 SHA-256 校验失败。")
    temp_root = Path(settings.BACKUP_TEMP_ROOT)
    temp_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="eam-verify-", dir=temp_root))
    try:
        bundle = work_dir / "bundle.tar"
        decrypt_file(package, bundle, passphrase=passphrase)
        with tarfile.open(bundle, "r") as archive:
            members = _safe_tar_members(archive, _PACKAGE_MEMBER_NAMES)
            archive.extractall(work_dir, members=members, filter="data")
        manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest_encryption = manifest.get("encryption")
        if manifest_encryption and manifest_encryption != encryption_metadata(package):
            raise ValidationError("备份清单中的加密 KDF 参数与包头不一致。")
        if (
            expected_backup_set_id
            and manifest.get("backup_set_id") != expected_backup_set_id
        ):
            raise ValidationError("备份清单编号与数据库记录不一致。")
        for section, filename in (("database", "database.dump"), ("media", "media.tar.gz")):
            child = work_dir / filename
            expected = manifest.get(section, {}).get("sha256")
            if not expected or sha256_file(child) != expected:
                raise ValidationError(f"备份清单中的 {filename} 摘要校验失败。")
        _validate_dump(work_dir / "database.dump")
        with tarfile.open(work_dir / "media.tar.gz", "r:gz") as media_archive:
            media_members = _safe_tar_members(media_archive)
            actual_names = {
                member.name for member in media_members if member.isfile()
            }
            actual_count = len(actual_names)
        if actual_count != manifest.get("media", {}).get("file_count"):
            raise ValidationError("附件归档文件数量与清单不一致。")
        expected_names = {
            item.get("path") for item in manifest.get("media", {}).get("files", [])
        }
        if None in expected_names or actual_names != expected_names:
            raise ValidationError("附件归档成员与清单不一致。")
        files_digest = hashlib.sha256(
            json.dumps(
                manifest.get("media", {}).get("files", []),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if files_digest != manifest.get("media", {}).get("files_manifest_sha256"):
            raise ValidationError("附件逐文件清单摘要校验失败。")
        return manifest
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def verify_backup_set(backup_set: BackupSet, *, passphrase: str):
    package = backup_package_path(backup_set)
    return verify_backup_package(
        package,
        passphrase=passphrase,
        expected_sha256=backup_set.package_sha256,
        expected_backup_set_id=backup_set.backup_set_id,
    )


def issue_download_grant(*, actor, backup_set, idempotency_key, request=None):
    require_recent_backup_authentication(actor)
    context = request_audit_context(request)
    now = timezone.now()
    with transaction.atomic():
        locked = BackupSet._base_manager.select_for_update().get(pk=backup_set.pk)
        backup_package_path(locked)
        existing = BackupDownloadGrant.objects.filter(
            company=locked.company, idempotency_key=idempotency_key
        ).first()
        if existing:
            if existing.backup_set_id != locked.pk or existing.user_id != actor.pk:
                raise ValidationError("相同幂等键对应不同下载授权。")
            return existing
        grant = BackupDownloadGrant(
            company=locked.company,
            backup_set=locked,
            user=actor,
            idempotency_key=idempotency_key,
            issued_at=now,
            expires_at=now + timedelta(minutes=settings.BACKUP_DOWNLOAD_GRANT_MINUTES),
        )
        grant.full_clean()
        grant.save()
        write_business_audit_log(
            company=locked.company,
            user=actor,
            action="backup.download_authorized",
            object_type="BackupDownloadGrant",
            object_id=grant.pk,
            old_data={},
            new_data={"backup_set_id": locked.backup_set_id, "expires_at": grant.expires_at},
            **context,
        )
        return grant


def start_download_grant(*, actor, grant, request=None):
    require_manage_backups(actor)
    context = request_audit_context(request)
    now = timezone.now()
    with transaction.atomic():
        # All operations involving both records lock BackupSet before Grant.
        # The FK is immutable, so the caller's already-loaded id is safe for
        # selecting the first lock; the locked Grant is revalidated below.
        locked_backup = BackupSet._base_manager.select_for_update().get(
            pk=grant.backup_set_id
        )
        locked = (
            BackupDownloadGrant._base_manager.select_for_update(of=("self",))
            .select_related("backup_set")
            .get(pk=grant.pk)
        )
        if (
            locked.backup_set_id != locked_backup.pk
            or locked.user_id != actor.pk
            or locked.company_id != locked_backup.company_id
        ):
            raise PermissionDenied("该下载授权不属于当前用户。")
        if locked.status != BackupDownloadGrant.Status.ISSUED or locked.expires_at <= now:
            raise ValidationError("下载授权已使用或已过期，请重新授权。")
        package = backup_package_path(locked_backup)
        BackupDownloadGrant._base_manager.filter(pk=locked.pk).update(
            status=BackupDownloadGrant.Status.STARTED,
            started_at=now,
        )
        write_business_audit_log(
            company=locked.company,
            user=actor,
            action="backup.download_started",
            object_type="BackupDownloadGrant",
            object_id=locked.pk,
            old_data={"status": BackupDownloadGrant.Status.ISSUED},
            new_data={"status": BackupDownloadGrant.Status.STARTED, "backup_set_id": locked_backup.backup_set_id},
            **context,
        )
        locked.refresh_from_db()
        return locked, package


def finish_download_grant(*, grant_id, succeeded, reason=""):
    with transaction.atomic():
        grant = (
            BackupDownloadGrant._base_manager.select_for_update(of=("self",))
            .select_related("backup_set", "user")
            .get(pk=grant_id)
        )
        if grant.status != BackupDownloadGrant.Status.STARTED:
            return grant
        now = timezone.now()
        status = (
            BackupDownloadGrant.Status.COMPLETED
            if succeeded
            else BackupDownloadGrant.Status.FAILED
        )
        safe_reason = "" if succeeded else str(reason or "下载连接中断")[:500]
        BackupDownloadGrant._base_manager.filter(pk=grant.pk).update(
            status=status,
            finished_at=now,
            failure_reason=safe_reason,
        )
        write_business_audit_log(
            company=grant.company,
            user=grant.user,
            action=("backup.download_completed" if succeeded else "backup.download_failed"),
            object_type="BackupDownloadGrant",
            object_id=grant.pk,
            old_data={"status": BackupDownloadGrant.Status.STARTED},
            new_data={"status": status, "backup_set_id": grant.backup_set.backup_set_id},
        )
        grant.refresh_from_db()
        return grant


def expire_due_backups(*, as_of=None):
    as_of = as_of or timezone.now()
    expired = []
    protected_automatic_ids = set()
    protected_dates = set()
    for row in BackupSet.objects.filter(
        status=BackupSet.Status.COMPLETED,
        kind=BackupSet.Kind.AUTOMATIC,
    ).order_by("-data_snapshot_at", "-pk"):
        business_date = timezone.localtime(
            row.data_snapshot_at or row.finished_at
        ).date()
        if business_date in protected_dates:
            continue
        if len(protected_dates) >= settings.BACKUP_RETENTION_DAYS:
            break
        protected_dates.add(business_date)
        protected_automatic_ids.add(row.pk)
    queryset = BackupSet.objects.filter(
        status=BackupSet.Status.COMPLETED, expires_at__lt=as_of
    ).exclude(pk__in=protected_automatic_ids).order_by("expires_at", "pk")
    for backup_id in queryset.values_list("pk", flat=True):
        moved = ()
        try:
            with transaction.atomic():
                backup = BackupSet._base_manager.select_for_update().get(pk=backup_id)
                if (
                    backup.status != BackupSet.Status.COMPLETED
                    or backup.expires_at >= as_of
                ):
                    continue
                started_grants = list(
                    BackupDownloadGrant._base_manager.select_for_update(of=("self",))
                    .filter(
                        backup_set=backup,
                        status=BackupDownloadGrant.Status.STARTED,
                    )
                    .select_related("user")
                    .order_by("pk")
                )
                lease_grace = timedelta(
                    minutes=settings.BACKUP_DOWNLOAD_GRANT_MINUTES
                )
                active_leases = [
                    grant
                    for grant in started_grants
                    if grant.expires_at + lease_grace > as_of
                ]
                if active_leases:
                    # A response may not have opened the path yet. Keeping the
                    # durable STARTED lease prevents expiry from winning that
                    # gap after start_download_grant releases its transaction.
                    continue
                for stale_grant in started_grants:
                    reason = (
                        "下载租约已超过授权 expires_at 及同长度宽限期，"
                        "由备份到期任务标记失败。"
                    )
                    BackupDownloadGrant._base_manager.filter(
                        pk=stale_grant.pk
                    ).update(
                        status=BackupDownloadGrant.Status.FAILED,
                        finished_at=as_of,
                        failure_reason=reason,
                    )
                    write_business_audit_log(
                        company=backup.company,
                        user=stale_grant.user,
                        action="backup.download_failed",
                        object_type="BackupDownloadGrant",
                        object_id=stale_grant.pk,
                        old_data={"status": BackupDownloadGrant.Status.STARTED},
                        new_data={
                            "status": BackupDownloadGrant.Status.FAILED,
                            "backup_set_id": backup.backup_set_id,
                            "reason": "stale_started_lease",
                        },
                    )
                moved = _quarantine_package_files(backup.storage_key)
                BackupSet._base_manager.filter(pk=backup.pk).update(
                    status=BackupSet.Status.EXPIRED,
                    storage_key="",
                    expired_at=as_of,
                )
                write_business_audit_log(
                    company=backup.company,
                    user=None,
                    action="backup.expired",
                    object_type="BackupSet",
                    object_id=backup.pk,
                    old_data={"status": BackupSet.Status.COMPLETED},
                    new_data={
                        "status": BackupSet.Status.EXPIRED,
                        "expired_at": as_of,
                    },
                )
                expired.append(backup.pk)
        except Exception:
            _restore_quarantined_package_files(moved)
            raise
        else:
            _delete_quarantined_package_files(moved)
    return expired


def _target_record_counts(database_name):
    from psycopg import sql

    db = settings.DATABASES["default"]
    restored = psycopg.connect(
        dbname=database_name,
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"],
    )
    try:
        counts = {}
        with restored.cursor() as cursor:
            for key, label in _CRITICAL_COUNT_MODELS.items():
                table = apps.get_model(label)._meta.db_table
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                )
                counts[key] = cursor.fetchone()[0]
        return counts
    finally:
        restored.close()


def _target_migrations(database_name):
    db = settings.DATABASES["default"]
    restored = psycopg.connect(
        dbname=database_name,
        user=db["USER"],
        password=db["PASSWORD"],
        host=db["HOST"],
        port=db["PORT"],
    )
    try:
        with restored.cursor() as cursor:
            cursor.execute("SELECT app, name FROM django_migrations ORDER BY app, name")
            return [f"{app}.{name}" for app, name in cursor.fetchall()]
    finally:
        restored.close()


def restore_backup_package_to_isolated(
    *,
    package_path,
    passphrase,
    target_database,
    target_media_root,
    expected_sha256=None,
    expected_backup_set_id=None,
    allow_current_empty=False,
):
    if not _SAFE_DB_NAME.fullmatch(target_database or ""):
        raise ValidationError("隔离恢复数据库名称格式非法。")
    current_db = current_database_name()
    if allow_current_empty:
        if target_database != current_db:
            raise ValidationError("本机空实例恢复必须使用当前配置的目标数据库。")
    elif target_database == current_db or not any(
        marker in target_database.lower() for marker in ("restore", "uat", "test")
    ):
        raise ValidationError("恢复目标必须是名称明确包含 restore/uat/test 的独立数据库。")
    media_target = Path(target_media_root).resolve()
    source_media = Path(settings.MEDIA_ROOT).resolve()
    if allow_current_empty:
        if media_target != source_media:
            raise ValidationError("本机空实例恢复必须使用当前配置的附件目录。")
    elif (
        media_target == source_media
        or media_target in source_media.parents
        or source_media in media_target.parents
    ):
        raise ValidationError("隔离恢复附件目录必须与当前附件目录完全分离。")
    if media_target.exists() and any(media_target.iterdir()):
        raise ValidationError("恢复附件目录必须为空，拒绝覆盖已有附件。")
    if allow_current_empty:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname='public' LIMIT 1"
            )
            if cursor.fetchone():
                raise ValidationError("目标数据库不是空库，拒绝覆盖已有数据。")
        connection.close()
    package = Path(package_path).resolve()
    manifest = verify_backup_package(
        package,
        passphrase=passphrase,
        expected_sha256=expected_sha256,
        expected_backup_set_id=expected_backup_set_id,
    )
    work_dir = Path(tempfile.mkdtemp(prefix="eam-restore-", dir=settings.BACKUP_TEMP_ROOT))
    try:
        bundle = work_dir / "bundle.tar"
        decrypt_file(package, bundle, passphrase=passphrase)
        with tarfile.open(bundle, "r") as archive:
            members = _safe_tar_members(archive, _PACKAGE_MEMBER_NAMES)
            archive.extractall(work_dir, members=members, filter="data")
        db = settings.DATABASES["default"]
        if _pg_mode() == "docker":
            container = str(settings.BACKUP_POSTGRES_CONTAINER)
            if not allow_current_empty:
                list_result = _run_checked(
                    [
                        str(settings.BACKUP_DOCKER_BIN),
                        "exec",
                        container,
                        "psql",
                        "-U",
                        str(db["USER"]),
                        "-d",
                        "postgres",
                        "--set",
                        f"target_db={target_database}",
                        "-tA",
                    ],
                    input_data=b"SELECT 1 FROM pg_database WHERE datname = :'target_db';\n",
                )
                database_exists = list_result.stdout.decode().strip() == "1"
                if database_exists:
                    raise ValidationError("隔离恢复目标数据库已存在，拒绝覆盖。")
                _run_checked(
                    [
                        str(settings.BACKUP_DOCKER_BIN),
                        "exec",
                        container,
                        "createdb",
                        "-U",
                        str(db["USER"]),
                        target_database,
                    ]
                )
            with (work_dir / "database.dump").open("rb") as source:
                _run_checked(
                    [
                        str(settings.BACKUP_DOCKER_BIN),
                        "exec",
                        "-i",
                        container,
                        "pg_restore",
                        "-U",
                        str(db["USER"]),
                        "-d",
                        target_database,
                        "--no-owner",
                        "--no-privileges",
                        "--exit-on-error",
                    ],
                    stdin=source,
                )
        else:
            env = os.environ.copy()
            env["PGPASSWORD"] = str(db["PASSWORD"])
            if not allow_current_empty:
                maintenance = psycopg.connect(
                    dbname="postgres",
                    user=db["USER"],
                    password=db["PASSWORD"],
                    host=db["HOST"],
                    port=db["PORT"],
                    autocommit=True,
                )
                try:
                    with maintenance.cursor() as cursor:
                        cursor.execute(
                            "SELECT 1 FROM pg_database WHERE datname=%s",
                            (target_database,),
                        )
                        database_exists = cursor.fetchone() is not None
                        if database_exists:
                            raise ValidationError("隔离恢复目标数据库已存在，拒绝覆盖。")
                        cursor.execute(f'CREATE DATABASE "{target_database}"')
                finally:
                    maintenance.close()
            _run_checked(
                [
                    str(settings.BACKUP_PG_RESTORE_BIN),
                    "--host",
                    str(db["HOST"]),
                    "--port",
                    str(db["PORT"]),
                    "--username",
                    str(db["USER"]),
                    "--dbname",
                    target_database,
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    str(work_dir / "database.dump"),
                ],
                env=env,
            )
        media_target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(work_dir / "media.tar.gz", "r:gz") as media_archive:
            members = _safe_tar_members(media_archive)
            media_archive.extractall(media_target, members=members, filter="data")
        restored_files = sum(1 for path in media_target.rglob("*") if path.is_file())
        if restored_files != manifest["media"]["file_count"]:
            raise ValidationError("恢复后的附件文件数量与清单不一致。")
        seen_paths = set()
        for item in manifest["media"].get("files", []):
            relative = PurePosixPath(item.get("path", ""))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValidationError("附件清单包含不安全路径。")
            if relative.as_posix() in seen_paths:
                raise ValidationError("附件清单包含重复路径。")
            seen_paths.add(relative.as_posix())
            restored_path = (media_target / Path(*relative.parts)).resolve()
            if media_target not in restored_path.parents or not restored_path.is_file():
                raise ValidationError("恢复附件缺失或路径越界。")
            if restored_path.stat().st_size != item.get("size"):
                raise ValidationError("恢复附件大小与清单不一致。")
            if sha256_file(restored_path) != item.get("sha256"):
                raise ValidationError("恢复附件摘要与清单不一致。")
        record_counts = _target_record_counts(target_database)
        expected_counts = manifest.get("record_counts")
        if expected_counts and record_counts != expected_counts:
            raise ValidationError("恢复后的关键记录数量与备份清单不一致。")
        restored_migrations = _target_migrations(target_database)
        if restored_migrations != manifest.get("migrations", []):
            raise ValidationError("恢复后的 migration 列表与备份清单不一致。")
        migration_count = len(restored_migrations)
        asset_count = record_counts["assets"]
        audit_count = record_counts["audit_logs"]
        return {
            "target_database": target_database,
            "target_media_root": str(media_target),
            "migration_count": migration_count,
            "asset_count": asset_count,
            "audit_count": audit_count,
            "media_file_count": restored_files,
            "record_counts": record_counts,
            "manifest": manifest,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def restore_backup_to_isolated(
    *, backup_set, passphrase, target_database, target_media_root
):
    package = backup_package_path(backup_set)
    return restore_backup_package_to_isolated(
        package_path=package,
        passphrase=passphrase,
        target_database=target_database,
        target_media_root=target_media_root,
        expected_sha256=backup_set.package_sha256,
        expected_backup_set_id=backup_set.backup_set_id,
    )


__all__ = [
    "backup_package_path",
    "current_database_name",
    "create_backup_set",
    "expire_due_backups",
    "finish_download_grant",
    "issue_download_grant",
    "restore_backup_package_to_isolated",
    "restore_backup_to_isolated",
    "start_download_grant",
    "verify_backup_set",
    "verify_backup_package",
]
