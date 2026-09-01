import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.masterdata.models import Company
from apps.operations.management.commands.verify_eam_backup import _read_passphrase
from apps.operations.models import BackupSet
from apps.operations.services import (
    current_database_name,
    restore_backup_package_to_isolated,
    restore_backup_to_isolated,
)


class Command(BaseCommand):
    help = "只向全新隔离数据库和空附件目录执行恢复演练。"

    def add_arguments(self, parser):
        parser.add_argument("backup_id", nargs="?")
        parser.add_argument("--package-file")
        parser.add_argument("--expected-sha256")
        parser.add_argument("--target-database")
        parser.add_argument("--target-media-root")
        parser.add_argument("--passphrase-file")
        parser.add_argument(
            "--confirm-isolated",
            action="store_true",
            help="确认目标为可丢弃的隔离恢复环境。",
        )
        parser.add_argument(
            "--target-current-empty",
            action="store_true",
            help="恢复到当前配置但必须为空的本机数据库和附件目录。",
        )
        parser.add_argument(
            "--confirm-empty-local",
            action="store_true",
            help="确认当前本机实例为全新空实例。",
        )

    def handle(self, *args, **options):
        try:
            if bool(options.get("backup_id")) == bool(options.get("package_file")):
                raise CommandError("必须且只能提供 backup_id 或 --package-file。")
            if options["target_current_empty"]:
                if not options["confirm_empty_local"]:
                    raise CommandError("必须显式传入 --confirm-empty-local。")
                if options.get("backup_id"):
                    raise CommandError("本机空实例恢复必须使用 --package-file。")
                if settings.EAM_ENVIRONMENT not in {"local", "development"}:
                    raise CommandError("本机空实例恢复只允许 local/development 环境。")
                result = restore_backup_package_to_isolated(
                    package_path=options["package_file"],
                    passphrase=_read_passphrase(options),
                    target_database=current_database_name(),
                    target_media_root=settings.MEDIA_ROOT,
                    expected_sha256=options.get("expected_sha256"),
                    allow_current_empty=True,
                )
            else:
                if not options["confirm_isolated"]:
                    raise CommandError("必须显式传入 --confirm-isolated。")
                if not options.get("target_database") or not options.get(
                    "target_media_root"
                ):
                    raise CommandError(
                        "隔离恢复必须提供 --target-database 和 --target-media-root。"
                    )
            if options.get("package_file") and not options["target_current_empty"]:
                result = restore_backup_package_to_isolated(
                    package_path=options["package_file"],
                    passphrase=_read_passphrase(options),
                    target_database=options["target_database"],
                    target_media_root=options["target_media_root"],
                    expected_sha256=options.get("expected_sha256"),
                )
            elif not options["target_current_empty"]:
                backup = BackupSet.objects.get(pk=options["backup_id"])
                result = restore_backup_to_isolated(
                    backup_set=backup,
                    passphrase=_read_passphrase(options),
                    target_database=options["target_database"],
                    target_media_root=options["target_media_root"],
                )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        company_code = result["manifest"].get("company_code")
        if not company_code:
            company_code = Company.objects.filter(is_active=True).values_list(
                "code", flat=True
            ).first()
        if not company_code:
            raise CommandError("恢复后未找到活动公司，无法执行后续一致性核对。")
        summary = {
            "target_database": result["target_database"],
            "target_media_root": result["target_media_root"],
            "migration_count": result["migration_count"],
            "asset_count": result["asset_count"],
            "audit_count": result["audit_count"],
            "media_file_count": result["media_file_count"],
            "record_counts": result["record_counts"],
            "backup_set_id": result["manifest"].get("backup_set_id"),
            "company_code": company_code,
        }
        self.stdout.write(
            self.style.SUCCESS(json.dumps(summary, ensure_ascii=False, default=str))
        )
