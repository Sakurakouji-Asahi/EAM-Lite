from django.core.management.base import BaseCommand

from apps.operations.services import expire_due_backups


class Command(BaseCommand):
    help = "按配置保留期过期备份文件，永久保留备份元数据和审计。"

    def handle(self, *args, **options):
        expired = expire_due_backups()
        self.stdout.write(self.style.SUCCESS(f"已过期 {len(expired)} 个备份集。"))
