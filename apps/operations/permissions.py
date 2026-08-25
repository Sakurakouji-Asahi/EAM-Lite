from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.masterdata.permissions import role_names_for


def can_manage_backups(user) -> bool:
    return bool(user and user.is_authenticated and "system_admin" in role_names_for(user))


def require_manage_backups(user) -> None:
    if not can_manage_backups(user):
        raise PermissionDenied("只有系统管理员可以管理和下载备份。")


def require_recent_backup_authentication(user) -> None:
    require_manage_backups(user)
    cutoff = timezone.now() - timedelta(minutes=settings.BACKUP_RECENT_AUTH_MINUTES)
    if user.last_login is None or user.last_login < cutoff:
        raise PermissionDenied("该高风险操作需要近期登录；请退出后重新登录再试。")


__all__ = [
    "can_manage_backups",
    "require_manage_backups",
    "require_recent_backup_authentication",
]
