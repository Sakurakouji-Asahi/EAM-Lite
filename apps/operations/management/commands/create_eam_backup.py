import json
import os
import re
import shutil
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.masterdata.permissions import current_company
from apps.operations.crypto import sha256_file
from apps.operations.models import BackupSet
from apps.operations.services import (
    backup_package_path,
    create_backup_set,
    verify_backup_set,
)


_SAFE_FILENAME = re.compile(r"^[^\\/:*?\"<>|]+\.eambak$")


class Command(BaseCommand):
    help = "生成数据库与附件同批次的加密 EAM-Lite 自动备份。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--idempotency-key",
            help="可选幂等键；默认按上海业务日生成。",
        )
        parser.add_argument(
            "--portable-output-dir",
            help="生成迁移口令保护的便携包并复制到该目录。",
        )
        parser.add_argument(
            "--passphrase-file",
            help="迁移口令文件；仅与 --portable-output-dir 同时使用。",
        )
        parser.add_argument(
            "--filename",
            help="可选便携包文件名，必须以 .eambak 结尾。",
        )

    def handle(self, *args, **options):
        company = current_company()
        if company is None:
            raise CommandError("尚未配置启用公司，不能生成备份。")
        portable = bool(options.get("portable_output_dir"))
        if portable != bool(options.get("passphrase_file")):
            raise CommandError(
                "--portable-output-dir 与 --passphrase-file 必须同时提供。"
            )
        if options.get("filename") and not portable:
            raise CommandError("--filename 只能用于便携备份。")
        key = options.get("idempotency_key")
        if not key:
            if portable:
                key = f"portable-{uuid.uuid4()}"
            else:
                base_key = f"automatic-{timezone.localdate().isoformat()}"
                previous = BackupSet.objects.filter(
                    company=company, idempotency_key=base_key
                ).first()
                if previous and previous.status == BackupSet.Status.FAILED:
                    key = f"{base_key}-retry-{uuid.uuid4()}"
                else:
                    key = base_key
        staging = None
        try:
            passphrase = None
            if portable:
                passphrase_path = Path(options["passphrase_file"])
                if not passphrase_path.is_file():
                    raise CommandError("迁移口令文件不存在。")
                passphrase = passphrase_path.read_text(encoding="utf-8").rstrip(
                    "\r\n"
                )
            backup = create_backup_set(
                actor=None,
                company=company,
                kind=(
                    BackupSet.Kind.MANUAL
                    if portable
                    else BackupSet.Kind.AUTOMATIC
                ),
                idempotency_key=key,
                passphrase=passphrase,
                local_console=portable,
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        if backup.status != BackupSet.Status.COMPLETED:
            raise CommandError(
                f"备份未完成，当前状态为 {backup.get_status_display()}。"
            )
        if not portable:
            self.stdout.write(
                self.style.SUCCESS(
                    f"备份完成：{backup.backup_set_id} sha256={backup.package_sha256}"
                )
            )
            return

        try:
            manifest = verify_backup_set(backup, passphrase=passphrase)
            output_dir = Path(options["portable_output_dir"]).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            version = re.sub(r"[^0-9A-Za-z.-]", "-", settings.APP_VERSION)
            filename = options.get("filename") or (
                "EAM-Lite-数据-"
                + timezone.localtime().strftime("%Y%m%d-%H%M%S")
                + f"-v{version}.eambak"
            )
            if (
                not _SAFE_FILENAME.fullmatch(filename)
                or Path(filename).name != filename
            ):
                raise CommandError("便携包文件名包含不允许的字符。")
            destination = output_dir / filename
            if destination.exists():
                raise CommandError("目标便携包已存在，拒绝覆盖。")
            staging = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(backup_package_path(backup), staging)
            if sha256_file(staging) != backup.package_sha256:
                raise CommandError("便携包复制后的 SHA-256 校验失败。")
            os.replace(staging, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        summary = {
            "path": str(destination),
            "size": destination.stat().st_size,
            "sha256": backup.package_sha256,
            "version": manifest.get("application_version"),
            "commit": manifest.get("application_commit"),
            "backup_set_id": backup.backup_set_id,
            "record_counts": manifest.get("record_counts", {}),
        }
        self.stdout.write(
            self.style.SUCCESS(
                "PORTABLE_BACKUP_JSON="
                + json.dumps(summary, ensure_ascii=False, sort_keys=True)
            )
        )
