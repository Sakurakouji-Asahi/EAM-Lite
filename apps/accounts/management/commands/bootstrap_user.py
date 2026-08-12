import getpass
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.roles import ROLE_NAMES, ensure_fixed_roles
from apps.audit.services import write_audit_log


class Command(BaseCommand):
    help = "由现有 Django superuser 创建首批应用用户并显式分配固定角色"

    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True, help="执行引导的 superuser 用户名")
        parser.add_argument("--username", required=True, help="新应用用户的用户名")
        parser.add_argument("--display-name", required=True, help="新应用用户的显示名称")
        parser.add_argument("--reason", required=True, help="创建账号及授予角色的原因")
        parser.add_argument("--email", default="", help="电子邮箱")
        parser.add_argument("--mobile", default="", help="手机号码")
        parser.add_argument(
            "--roles",
            nargs="+",
            required=True,
            help="一个或多个固定角色名称",
        )
        parser.add_argument(
            "--password-env",
            help="从指定环境变量读取密码；省略时通过终端安全输入",
        )
        parser.add_argument(
            "--actor-password-env",
            help="从指定环境变量读取执行人当前密码；省略时通过终端安全输入",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            actor = User.objects.get(username=options["actor"])
        except User.DoesNotExist as exc:
            raise CommandError("指定的执行人不存在") from exc
        if not actor.is_active or not actor.is_superuser:
            raise CommandError("只有启用的 Django superuser 可以执行账号引导")

        reason = options["reason"].strip()
        if not reason:
            raise CommandError("创建账号及授予角色的原因不得为空")

        actor_password_env = options.get("actor_password_env")
        if actor_password_env:
            actor_password = os.environ.get(actor_password_env)
            if not actor_password:
                raise CommandError(f"环境变量 {actor_password_env} 未设置或为空")
        else:
            actor_password = getpass.getpass("执行人当前密码：")
        if not actor.check_password(actor_password):
            raise CommandError("执行人身份确认失败")

        roles = tuple(dict.fromkeys(options["roles"]))
        invalid_roles = sorted(set(roles) - set(ROLE_NAMES))
        if invalid_roles:
            raise CommandError(f"不允许的角色：{', '.join(invalid_roles)}")

        username = options["username"].strip()
        display_name = options["display_name"].strip()
        if not username or not display_name:
            raise CommandError("用户名和显示名称不得为空")
        if User.objects.filter(username=username).exists():
            raise CommandError("目标用户名已存在，账号引导不会覆盖现有用户")

        password_env = options.get("password_env")
        if password_env:
            password = os.environ.get(password_env)
            if not password:
                raise CommandError(f"环境变量 {password_env} 未设置或为空")
        else:
            password = getpass.getpass("新用户密码：")
            confirmation = getpass.getpass("再次输入新用户密码：")
            if password != confirmation:
                raise CommandError("两次输入的密码不一致")

        user = User(
            username=username,
            display_name=display_name,
            email=options["email"].strip(),
            mobile=options["mobile"].strip(),
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        try:
            validate_password(password, user=user)
            user.full_clean(exclude={"password"})
        except ValidationError as exc:
            raise CommandError("新用户资料或密码不符合要求：" + "；".join(exc.messages))

        with transaction.atomic():
            ensure_fixed_roles()
            user.set_password(password)
            user.save()
            user.groups.set(user.groups.model.objects.filter(name__in=roles))
            write_audit_log(
                user=actor,
                action="account.bootstrap_created",
                object_type="User",
                object_id=user.pk,
                old_data={},
                new_data={
                    "username": user.username,
                    "display_name": user.display_name,
                    "email": user.email,
                    "mobile": user.mobile,
                    "is_active": user.is_active,
                    "roles": list(roles),
                    "reason": reason,
                },
            )

        self.stdout.write(self.style.SUCCESS(f"已创建应用用户：{username}"))
