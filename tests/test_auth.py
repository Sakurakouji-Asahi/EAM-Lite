import json
import logging
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from apps.audit.models import AuditLog


pytestmark = pytest.mark.django_db


def create_user(username="zhangsan", password="Correct-Password-2026!"):
    return get_user_model().objects.create_user(
        username=username,
        password=password,
        display_name="张三",
    )


def test_user_can_log_in_with_correct_password(client):
    user = create_user()

    response = client.post(
        reverse("login"),
        {"username": user.username, "password": "Correct-Password-2026!"},
    )

    assert response.status_code == 302
    assert response.url == reverse("home")
    assert AuditLog.objects.filter(
        action="auth.login_succeeded", object_id=str(user.pk)
    ).exists()


def test_wrong_password_is_rejected_and_never_logged(client, caplog):
    create_user()
    submitted_password = "Never-Write-This-Password!"

    with caplog.at_level(logging.WARNING):
        response = client.post(
            reverse("login"),
            {"username": "zhangsan", "password": submitted_password},
        )

    assert response.status_code == 200
    assert "用户名或密码错误，请重新输入。" in response.content.decode()
    assert "_auth_user_id" not in client.session
    failure = AuditLog.objects.get(action="auth.login_failed")
    serialized = json.dumps(
        {"old": failure.old_data_json, "new": failure.new_data_json},
        ensure_ascii=False,
    )
    assert submitted_password not in serialized
    assert submitted_password not in caplog.text


def test_unknown_and_existing_user_receive_same_error(client):
    create_user()

    existing = client.post(
        reverse("login"),
        {"username": "zhangsan", "password": "wrong"},
    )
    unknown = client.post(
        reverse("login"),
        {"username": "not-a-user", "password": "wrong"},
    )

    message = "用户名或密码错误，请重新输入。"
    assert message in existing.content.decode()
    assert message in unknown.content.decode()


def test_inactive_user_cannot_log_in(client):
    user = create_user()
    user.is_active = False
    user.save(update_fields=["is_active"])

    response = client.post(
        reverse("login"),
        {"username": user.username, "password": "Correct-Password-2026!"},
    )

    assert response.status_code == 200
    assert "用户名或密码错误，请重新输入。" in response.content.decode()
    assert "_auth_user_id" not in client.session
    assert AuditLog.objects.filter(action="auth.login_failed").exists()


@override_settings(
    LOGIN_FAILURE_WINDOW_SECONDS=300,
    LOGIN_FAILURE_PAIR_LIMIT=2,
)
def test_pair_limit_blocks_n_plus_one_without_authentication_or_audit(
    client, caplog
):
    create_user(username="limited-user")
    login_url = reverse("login")
    credentials = {"username": "limited-user", "password": "wrong"}

    assert client.post(login_url, credentials).status_code == 200
    assert client.post(login_url, credentials).status_code == 200
    assert AuditLog.objects.filter(action="auth.login_failed").count() == 2

    with patch("django.contrib.auth.forms.authenticate") as authenticate, caplog.at_level(
        logging.WARNING
    ):
        response = client.post(login_url, credentials)

    assert response.status_code == 200
    assert "用户名或密码错误，请重新输入。" in response.content.decode()
    authenticate.assert_not_called()
    assert AuditLog.objects.filter(action="auth.login_failed").count() == 2
    assert "limited-user" not in caplog.text
    assert "wrong" not in caplog.text


@override_settings(
    LOGIN_FAILURE_WINDOW_SECONDS=300,
    LOGIN_FAILURE_PAIR_LIMIT=2,
)
def test_success_resets_only_the_pair_continuous_failure_count(client):
    create_user(username="reset-user")
    login_url = reverse("login")
    wrong = {"username": "reset-user", "password": "wrong"}

    client.post(login_url, wrong)
    response = client.post(
        login_url,
        {"username": "reset-user", "password": "Correct-Password-2026!"},
    )
    assert response.status_code == 302
    client.logout()

    assert client.post(login_url, wrong).status_code == 200
    assert client.post(login_url, wrong).status_code == 200
    with patch("django.contrib.auth.forms.authenticate") as authenticate:
        response = client.post(login_url, wrong)

    assert "用户名或密码错误，请重新输入。" in response.content.decode()
    authenticate.assert_not_called()
    assert AuditLog.objects.filter(action="auth.login_failed").count() == 3


def test_anonymous_user_is_redirected_from_home(client):
    response = client.get(reverse("home"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


def test_login_page_uses_accessible_responsive_product_shell(client):
    response = client.get(reverse("login"))
    content = response.content.decode()

    assert response.status_code == 200
    assert 'class="login-layout"' in content
    assert 'class="skip-link"' in content
    assert 'autocomplete="username"' in content
    assert 'autocomplete="current-password"' in content
    assert "资产有据可查，责任清晰可见" in content


def test_authenticated_user_can_access_home(client):
    user = create_user()
    client.force_login(user)

    response = client.get(reverse("home"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "EAM-Lite 企业资产管理系统" in content
    assert "系统初始化尚未完成" in content
    assert 'class="app-layout"' in content
    assert 'id="mobile-navigation"' in content
    assert 'aria-current="page"' in content


def test_logout_requires_post_and_invalidates_session(client):
    user = create_user()
    client.force_login(user)

    assert client.get(reverse("logout")).status_code == 405
    response = client.post(reverse("logout"))

    assert response.status_code == 302
    assert response.url == reverse("login")
    assert "_auth_user_id" not in client.session
    assert client.get(reverse("home")).status_code == 302


def test_logout_post_requires_csrf_token():
    user = create_user()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    csrf_client.get(reverse("home"))

    assert csrf_client.post(reverse("logout")).status_code == 403

    csrf_token = csrf_client.cookies["csrftoken"].value
    response = csrf_client.post(
        reverse("logout"),
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert response.status_code == 302
    assert "_auth_user_id" not in csrf_client.session
