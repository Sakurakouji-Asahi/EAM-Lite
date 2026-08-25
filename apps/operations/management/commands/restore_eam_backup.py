import json

from django.core.management.base import BaseCommand, CommandError

from apps.operations.management.commands.verify_eam_backup import _read_passphrase
from apps.operations.models import BackupSet
from apps.operations.services import (
    restore_backup_package_to_isolated,
    restore_backup_to_isolated,
)


class Command(BaseCommand):
    help = "只向全新隔离数据库和空附件目录执行恢复演练。"

    def add_arguments(self, parser):
        parser.add_argument("backup_id", nargs="?")
        parser.add_argument("--package-file")
        parser.add_argument("--expected-sha256")
        parser.add_argument("--target-database", required=True)
        parser.add_argument("--target-media-root", required=True)
        parser.add_argument("--passphrase-file")
        parser.add_argument(
            "--confirm-isolated",
            action="store_true",
            help="确认目标为可丢弃的隔离恢复环境。",
        )

    def handle(self, *args, **options):
        if not options["confirm_isolated"]:
            raise CommandError("必须显式传入 --confirm-isolated。")
        try:
            if bool(options.get("backup_id")) == bool(options.get("package_file")):
                raise CommandError("必须且只能提供 backup_id 或 --package-file。")
            if options.get("package_file"):
                result = restore_backup_package_to_isolated(
                    package_path=options["package_file"],
                    passphrase=_read_passphrase(options),
                    target_database=options["target_database"],
                    target_media_root=options["target_media_root"],
                    expected_sha256=options.get("expected_sha256"),
                )
            else:
                backup = BackupSet.objects.get(pk=options["backup_id"])
                result = restore_backup_to_isolated(
                    backup_set=backup,
                    passphrase=_read_passphrase(options),
                    target_database=options["target_database"],
                    target_media_root=options["target_media_root"],
                )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        summary = {
            "target_database": result["target_database"],
            "target_media_root": result["target_media_root"],
            "migration_count": result["migration_count"],
            "asset_count": result["asset_count"],
            "audit_count": result["audit_count"],
            "media_file_count": result["media_file_count"],
            "backup_set_id": result["manifest"].get("backup_set_id"),
        }
        self.stdout.write(
            self.style.SUCCESS(json.dumps(summary, ensure_ascii=False, default=str))
        )
