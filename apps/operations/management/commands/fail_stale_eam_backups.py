from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import write_business_audit_log
from apps.operations.models import BackupSet


class Command(BaseCommand):
    help = "把进程崩溃遗留的超时 pending 备份标为 failed，解除写冻结。"

    def add_arguments(self, parser):
        parser.add_argument("--older-minutes", type=int, default=60)

    def handle(self, *args, **options):
        minutes = options["older_minutes"]
        if minutes < 15:
            raise CommandError("older-minutes 不得小于 15。")
        cutoff = timezone.now() - timedelta(minutes=minutes)
        changed = 0
        for backup_id in BackupSet.objects.filter(
            status=BackupSet.Status.PENDING, started_at__lt=cutoff
        ).values_list("pk", flat=True):
            with transaction.atomic():
                backup = BackupSet._base_manager.select_for_update().get(pk=backup_id)
                if backup.status != BackupSet.Status.PENDING or backup.started_at >= cutoff:
                    continue
                finished = timezone.now()
                reason = "备份进程中断或超时，已由恢复命令关闭 pending 状态。"
                BackupSet._base_manager.filter(pk=backup.pk).update(
                    status=BackupSet.Status.FAILED,
                    finished_at=finished,
                    error_summary=reason,
                )
                write_business_audit_log(
                    company=backup.company,
                    user=None,
                    action="backup.stale_failed",
                    object_type="BackupSet",
                    object_id=backup.pk,
                    old_data={"status": BackupSet.Status.PENDING},
                    new_data={"status": BackupSet.Status.FAILED, "reason": reason},
                )
                changed += 1
        self.stdout.write(self.style.SUCCESS(f"已关闭 {changed} 个超时备份。"))
