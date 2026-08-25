import re

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_NO_DELETE_MODELS = (
    "audit.AuditLog",
    "masterdata.IssuedCode",
    "assets.AssetCodeHistory",
    "assets.AssetMovement",
    "assets.AssetDisposal",
    "assets.AssetDisposalReversal",
    "finance.DepreciationEntry",
    "inventory.InventoryTaskAsset",
    "inventory.InventoryScan",
    "inventory.InventoryResolution",
    "maintenance.MaintenanceRecord",
    "maintenance.MaintenanceProblem",
    "offboarding.EmployeeAssetClearance",
    "offboarding.EmployeeAssetClearanceItem",
    "reports.ExportLog",
    "reports.ExportLogTotal",
    "operations.BackupSet",
    "operations.BackupDownloadGrant",
)


def _quote_identifier(value):
    return connection.ops.quote_name(value)


class Command(BaseCommand):
    help = "由迁移身份向最小权限 runtime 角色授予当前 schema 权限。"

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("生产权限授予只支持 PostgreSQL。")
        role = settings.DATABASE_RUNTIME_ROLE
        if not _ROLE_RE.fullmatch(role or ""):
            raise CommandError("DATABASE_RUNTIME_ROLE 格式非法或未配置。")
        quoted_role = _quote_identifier(role)
        protected_tables = [
            apps.get_model(label)._meta.db_table for label in _NO_DELETE_MODELS
        ]
        with connection.cursor() as cursor:
            cursor.execute(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {quoted_role}"
            )
            cursor.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {quoted_role}"
            )
            cursor.execute(f"REVOKE CREATE ON SCHEMA public FROM {quoted_role}")
            for table in protected_tables:
                cursor.execute(
                    f"REVOKE DELETE, TRUNCATE ON TABLE {_quote_identifier(table)} FROM {quoted_role}"
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"已刷新 runtime 角色 {role} 的最小数据库权限。"
            )
        )
