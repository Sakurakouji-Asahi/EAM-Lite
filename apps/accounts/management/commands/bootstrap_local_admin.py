import getpass
import sys

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.accounts.roles import ensure_fixed_roles
from apps.audit.services import write_system_audit_log


class Command(BaseCommand):
    help = "在空用户库中交互创建首个本机 system_admin 应用账号。"

    def handle(self, *args, **options):
        if settings.EAM_ENVIRONMENT not in {"local", "development"}:
            raise CommandError("该命令只允许在本机 local/development 环境使用。")

        User = get_user_model()
        if User.objects.exists():
            self.stdout.write("系统已存在用户，跳过首次管理员初始化。")
            return
        if not sys.stdin.isatty():
            raise CommandError("首次管理员初始化需要在本机交互终端中运行。")

        username = input("管理员用户名：").strip()
        display_name = input("显示名称：").strip()
        password = getpass.getpass("管理员密码：")
        confirmation = getpass.getpass("再次输入管理员密码：")
        if password != confirmation:
            raise CommandError("两次输入的密码不一致。")
        if not username or not display_name:
            raise CommandError("用户名和显示名称不能为空。")

        user = User(
            username=username,
            display_name=display_name,
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        try:
            validate_password(password, user=user)
            user.full_clean(exclude={"password"})
        except ValidationError as exc:
            raise CommandError("管理员资料或密码不符合要求：" + "；".join(exc.messages))

        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ["eam-lite-bootstrap-local-admin"],
                    )
            if User.objects.exists():
                raise CommandError("另一个初始化进程已经创建用户，请直接登录。")

            from apps.masterdata.models import Company

            ensure_fixed_roles()
            user.set_password(password)
            user.save()
            user.groups.add(user.groups.model.objects.get(name="system_admin"))
            company = Company.objects.order_by("-is_active", "created_at").first()
            write_system_audit_log(
                user=user,
                action="account.bootstrap_created",
                object_type="User",
                object_id=user.pk,
                old_data={},
                new_data={
                    "username": user.username,
                    "display_name": user.display_name,
                    "is_active": True,
                    "roles": ["system_admin"],
                    "source": "local_console_first_run",
                },
                company=company,
            )

        self.stdout.write(self.style.SUCCESS(f"已创建首个系统管理员：{username}"))
