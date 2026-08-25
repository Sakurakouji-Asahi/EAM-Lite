from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.masterdata.permissions import current_company
from apps.operations.crypto import sha256_file
from apps.operations.models import BackupSet
from apps.operations.services import backup_package_path, _storage_path


class Command(BaseCommand):
    help = "检查最近成功自动备份的时间、主副本摘要与镜像可读性。"

    def handle(self, *args, **options):
        company = current_company()
        if company is None:
            raise CommandError("尚未配置启用公司。")
        backup = BackupSet.objects.filter(
            company=company,
            kind=BackupSet.Kind.AUTOMATIC,
            status=BackupSet.Status.COMPLETED,
        ).order_by("-finished_at", "-pk").first()
        if backup is None:
            raise CommandError("没有成功的自动备份。")
        cutoff = timezone.now() - timedelta(hours=settings.BACKUP_MAX_AGE_HOURS)
        if backup.finished_at < cutoff:
            raise CommandError("最近成功自动备份已超过允许时限。")
        primary = backup_package_path(backup)
        if settings.BACKUP_MIRROR_ROOT:
            mirror = _storage_path(
                backup.storage_key, root=settings.BACKUP_MIRROR_ROOT
            )
            if not mirror.is_file() or sha256_file(mirror) != backup.package_sha256:
                raise CommandError("独立镜像备份不存在或摘要不一致。")
        self.stdout.write(
            self.style.SUCCESS(
                f"备份健康：{backup.backup_set_id}，{primary.stat().st_size} bytes。"
            )
        )
