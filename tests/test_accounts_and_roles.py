import importlib
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.roles import ROLE_CHOICES, ROLE_LABELS, ROLE_NAMES
from apps.audit.models import AuditLog


pytestmark = pytest.mark.django_db


def test_every_fixed_role_has_a_stable_chinese_display_label():
    assert tuple(name for name, _label in ROLE_CHOICES) == ROLE_NAMES
    assert set(ROLE_LABELS) == set(ROLE_NAMES)
    assert ROLE_LABELS["system_admin"] == "系统管理员"
    assert ROLE_LABELS["finance"] == "财务"


def test_custom_user_model_is_active_from_initial_migration():
    User = get_user_model()
    assert User._meta.label == "accounts.User"
    assert "display_name" in User.REQUIRED_FIELDS

    user = User.objects.create_user(
        username="custom-user",
        password="Custom-Password-2026!",
        display_name="自定义用户",
        email="user@example.test",
        mobile="13800000000",
    )

    assert user.check_password("Custom-Password-2026!")
    assert user.display_name == "自定义用户"
    assert user.created_at is not None
    assert user.updated_at is not None


def test_eight_fixed_roles_exist_and_seed_migration_is_idempotent():
    migration = importlib.import_module(
        "apps.accounts.migrations.0002_seed_fixed_roles"
    )
    Group.objects.filter(name=ROLE_NAMES[0]).delete()

    migration.seed_fixed_roles(django_apps, None)
    migration.seed_fixed_roles(django_apps, None)

    assert set(
        Group.objects.filter(name__in=ROLE_NAMES).values_list("name", flat=True)
    ) == set(ROLE_NAMES)
    assert Group.objects.filter(name__in=ROLE_NAMES).count() == 8


def test_bootstrap_command_creates_user_with_only_explicit_roles_and_audit(
    monkeypatch,
):
    User = get_user_model()
    User.objects.create_superuser(
        username="root",
        password="Root-Password-2026!",
        display_name="恢复管理员",
    )
    monkeypatch.setenv(
        "TEST_BOOTSTRAP_PASSWORD", "Application-Password-2026!Strong"
    )
    monkeypatch.setenv("TEST_ACTOR_PASSWORD", "Root-Password-2026!")

    call_command(
        "bootstrap_user",
        actor="root",
        username="finance-user",
        display_name="财务用户",
        reason="建立首批财务账号",
        email="finance@example.test",
        mobile="13900000000",
        roles=["system_admin", "finance"],
        password_env="TEST_BOOTSTRAP_PASSWORD",
        actor_password_env="TEST_ACTOR_PASSWORD",
        verbosity=0,
    )

    user = User.objects.get(username="finance-user")
    assert set(user.groups.values_list("name", flat=True)) == {
        "system_admin",
        "finance",
    }
    assert not user.groups.filter(name="hr").exists()
    assert not user.is_superuser
    audit = AuditLog.objects.get(action="account.bootstrap_created")
    assert audit.user.username == "root"
    assert audit.new_data_json["roles"] == ["system_admin", "finance"]
    assert audit.new_data_json["reason"] == "建立首批财务账号"
    assert "password" not in repr(audit.new_data_json).lower()
    assert "Application-Password-2026!Strong" not in repr(audit.new_data_json)
    assert "Root-Password-2026!" not in repr(audit.new_data_json)


def test_bootstrap_command_rejects_non_superuser_actor(monkeypatch):
    User = get_user_model()
    User.objects.create_user(
        username="ordinary",
        password="Ordinary-Password-2026!",
        display_name="普通用户",
    )
    monkeypatch.setenv("TEST_BOOTSTRAP_PASSWORD", "Another-Password-2026!Strong")

    with pytest.raises(CommandError):
        call_command(
            "bootstrap_user",
            actor="ordinary",
            username="target",
            display_name="目标用户",
            reason="测试非管理员拒绝",
            roles=["employee"],
            password_env="TEST_BOOTSTRAP_PASSWORD",
        )

    assert not User.objects.filter(username="target").exists()


def test_bootstrap_rolls_back_account_when_required_audit_fails(monkeypatch):
    User = get_user_model()
    User.objects.create_superuser(
        username="root",
        password="Root-Password-2026!",
        display_name="恢复管理员",
    )
    monkeypatch.setenv("TEST_BOOTSTRAP_PASSWORD", "Rollback-Password-2026!Strong")
    monkeypatch.setenv("TEST_ACTOR_PASSWORD", "Root-Password-2026!")
    with patch(
        "apps.accounts.management.commands.bootstrap_user.write_system_audit_log",
        side_effect=RuntimeError("audit unavailable"),
    ):
        with pytest.raises(RuntimeError, match="audit unavailable"):
            call_command(
                "bootstrap_user",
                actor="root",
                username="rolled-back",
                display_name="应回滚用户",
                reason="测试审计失败回滚",
                roles=["employee"],
                password_env="TEST_BOOTSTRAP_PASSWORD",
                actor_password_env="TEST_ACTOR_PASSWORD",
            )

    assert not User.objects.filter(username="rolled-back").exists()


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_bootstrap_command_requires_nonblank_reason(monkeypatch, reason):
    User = get_user_model()
    User.objects.create_superuser(
        username="root",
        password="Root-Password-2026!",
        display_name="恢复管理员",
    )
    monkeypatch.setenv("TEST_BOOTSTRAP_PASSWORD", "Target-Password-2026!Strong")
    monkeypatch.setenv("TEST_ACTOR_PASSWORD", "Root-Password-2026!")
    options = {
        "actor": "root",
        "username": "target",
        "display_name": "目标用户",
        "roles": ["employee"],
        "password_env": "TEST_BOOTSTRAP_PASSWORD",
        "actor_password_env": "TEST_ACTOR_PASSWORD",
    }
    if reason is not None:
        options["reason"] = reason

    with pytest.raises(CommandError):
        call_command("bootstrap_user", **options)

    assert not User.objects.filter(username="target").exists()


def test_bootstrap_command_rejects_wrong_actor_password_before_writes(monkeypatch):
    User = get_user_model()
    User.objects.create_superuser(
        username="root",
        password="Root-Password-2026!",
        display_name="恢复管理员",
    )
    monkeypatch.setenv("TEST_BOOTSTRAP_PASSWORD", "Target-Password-2026!Strong")
    monkeypatch.setenv("TEST_ACTOR_PASSWORD", "Wrong-Actor-Password!")

    with pytest.raises(CommandError, match="执行人身份确认失败"):
        call_command(
            "bootstrap_user",
            actor="root",
            username="target",
            display_name="目标用户",
            reason="建立普通账号",
            roles=["employee"],
            password_env="TEST_BOOTSTRAP_PASSWORD",
            actor_password_env="TEST_ACTOR_PASSWORD",
        )

    assert not User.objects.filter(username="target").exists()
    assert not AuditLog.objects.filter(action="account.bootstrap_created").exists()
