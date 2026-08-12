import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command

from apps.audit.models import AuditLog
from apps.audit.signals import _current_company
from apps.audit.services import (
    write_audit_log,
    write_business_audit_log,
    write_system_audit_log,
)
from apps.masterdata.models import Company


pytestmark = pytest.mark.django_db


def test_pre_initialization_audit_may_remain_without_company():
    audit = write_system_audit_log(
        action="auth.pre_initialization", object_type="System"
    )

    assert audit.company is None


def test_business_audit_requires_real_company():
    with pytest.raises(ValueError, match="必须关联 Company"):
        write_business_audit_log(
            company=None,
            action="business.invalid",
            object_type="Example",
        )

    with pytest.raises(ValueError, match="真实的 Company"):
        write_audit_log(
            company="fake",
            action="business.invalid",
            object_type="Example",
        )


def test_generic_audit_rejects_null_company_after_any_company_exists():
    for active in (True, False):
        Company.objects.all().delete()
        Company.objects.create(
            code=f"C-{active}",
            normalized_code=f"c-{active}",
            name="已建立公司",
            short_name="公司",
            is_active=active,
        )
        with pytest.raises(ValueError, match="必须关联真实 Company"):
            write_system_audit_log(
                action="auth.login_failed",
                object_type="UserAuthentication",
            )
        with pytest.raises(ValueError, match="必须关联真实 Company"):
            write_audit_log(
                action="business.invalid",
                object_type="Employee",
            )


def test_inactive_existing_company_is_used_for_security_and_bootstrap_audit(
    monkeypatch,
):
    company = Company.objects.create(
        code="C1",
        normalized_code="c1",
        name="停用公司",
        short_name="停用",
        is_active=False,
    )
    assert _current_company() == company

    User = get_user_model()
    User.objects.create_superuser(
        username="root",
        password="Root-Password-2026!",
        display_name="恢复管理员",
    )
    Group.objects.get_or_create(name="employee")
    monkeypatch.setenv("TEST_BOOTSTRAP_PASSWORD", "Target-Password-2026!Strong")
    monkeypatch.setenv("TEST_ACTOR_PASSWORD", "Root-Password-2026!")
    call_command(
        "bootstrap_user",
        actor="root",
        username="target",
        display_name="目标用户",
        reason="停用公司下恢复账号",
        roles=["employee"],
        password_env="TEST_BOOTSTRAP_PASSWORD",
        actor_password_env="TEST_ACTOR_PASSWORD",
        verbosity=0,
    )

    assert AuditLog.objects.get(action="account.bootstrap_created").company == company
