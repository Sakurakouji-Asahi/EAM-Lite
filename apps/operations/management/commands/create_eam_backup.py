import uuid

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.masterdata.permissions import current_company
from apps.operations.models import BackupSet
from apps.operations.services import create_backup_set


class Command(BaseCommand):
    help = "生成数据库与附件同批次的加密 EAM-Lite 自动备份。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--idempotency-key",
            help="可选幂等键；默认按上海业务日生成。",
        )

    def handle(self, *args, **options):
        company = current_company()
        if company is None:
            raise CommandError("尚未配置启用公司，不能生成备份。")
        key = options.get("idempotency_key")
        if not key:
            base_key = f"automatic-{timezone.localdate().isoformat()}"
            previous = BackupSet.objects.filter(
                company=company, idempotency_key=base_key
            ).first()
            if previous and previous.status == BackupSet.Status.FAILED:
                key = f"{base_key}-retry-{uuid.uuid4()}"
            else:
                key = base_key
        try:
            backup = create_backup_set(
                actor=None,
                company=company,
                kind=BackupSet.Kind.AUTOMATIC,
                idempotency_key=key,
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        if backup.status != BackupSet.Status.COMPLETED:
            raise CommandError(
                f"备份未完成，当前状态为 {backup.get_status_display()}。"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"备份完成：{backup.backup_set_id} sha256={backup.package_sha256}"
            )
        )
