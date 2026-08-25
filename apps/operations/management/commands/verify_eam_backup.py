import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.operations.models import BackupSet
from apps.operations.services import verify_backup_package, verify_backup_set


def _read_passphrase(options):
    if options.get("passphrase_file"):
        path = Path(options["passphrase_file"])
        if not path.is_file():
            raise CommandError("备份口令文件不存在。")
        return path.read_text(encoding="utf-8").strip()
    value = os.environ.get("EAM_BACKUP_PASSPHRASE", "")
    if not value:
        raise CommandError(
            "请通过 --passphrase-file 或 EAM_BACKUP_PASSPHRASE 提供口令。"
        )
    return value


class Command(BaseCommand):
    help = "验证 EAM-Lite 加密备份包、内部摘要、pg_dump 与附件归档。"

    def add_arguments(self, parser):
        parser.add_argument("backup_id", nargs="?")
        parser.add_argument("--package-file")
        parser.add_argument("--expected-sha256")
        parser.add_argument("--passphrase-file")

    def handle(self, *args, **options):
        try:
            if bool(options.get("backup_id")) == bool(options.get("package_file")):
                raise CommandError("必须且只能提供 backup_id 或 --package-file。")
            if options.get("package_file"):
                manifest = verify_backup_package(
                    options["package_file"],
                    passphrase=_read_passphrase(options),
                    expected_sha256=options.get("expected_sha256"),
                )
            else:
                backup = BackupSet.objects.get(pk=options["backup_id"])
                manifest = verify_backup_set(
                    backup, passphrase=_read_passphrase(options)
                )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"验证通过：{manifest['backup_set_id']}，附件 {manifest['media']['file_count']} 个。"
            )
        )
