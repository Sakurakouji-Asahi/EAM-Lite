import os
import json
import subprocess
from pathlib import Path

import pytest
from django.test import override_settings
from django.http import JsonResponse
from django.test import RequestFactory
from django.urls import reverse


pytestmark = pytest.mark.django_db


def test_health_check_is_minimal_no_store_and_has_correlation_id(client):
    response = client.get(reverse("healthz"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "no-store" in response["Cache-Control"]
    assert response["X-Correlation-ID"]
    assert "version" not in response.content.decode().lower()


def test_version_endpoint_reports_build_identity_without_secrets(client, settings):
    settings.APP_VERSION = "0.2.1-test"
    settings.APP_COMMIT_SHA = "a" * 40
    settings.BUILD_TIME = "2026-09-01T00:00:00Z"
    settings.EAM_ENVIRONMENT = "test"

    response = client.get(reverse("version-info"))

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.2.1-test",
        "commit": "a" * 40,
        "environment": "test",
        "database_vendor": settings.DATABASE_ENGINE,
        "build_time": "2026-09-01T00:00:00Z",
    }
    assert "no-store" in response["Cache-Control"]
    serialized = response.content.decode().lower()
    assert "password" not in serialized and "secret" not in serialized


def test_unapproved_host_is_rejected(client):
    response = client.get("/healthz/", HTTP_HOST="evil.example.invalid")
    assert response.status_code == 400


@override_settings(DEBUG=False)
def test_production_error_pages_hide_exception_and_show_correlation_id(client):
    response = client.get("/definitely-missing-sprint12/")
    text = response.content.decode()
    assert response.status_code == 404
    assert "页面不存在" in text
    assert "关联标识" in text
    assert "Traceback" not in text and "settings.py" not in text


def test_no_runtime_template_uses_public_cdn():
    roots = [Path("templates"), Path("apps")]
    offenders = []
    for root in roots:
        for path in root.rglob("*.html"):
            text = path.read_text(encoding="utf-8").lower()
            if "https://" in text or "http://" in text or "//cdn" in text:
                offenders.append(str(path))
    assert offenders == []


def test_private_and_backup_roots_are_not_nested(settings):
    roots = [
        Path(settings.STATIC_ROOT).resolve(),
        Path(settings.MEDIA_ROOT).resolve(),
        Path(settings.IMPORT_TEMP_ROOT).resolve(),
        Path(settings.BACKUP_ROOT).resolve(),
        Path(settings.BACKUP_TEMP_ROOT).resolve(),
    ]
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            assert first != second
            assert first not in second.parents
            assert second not in first.parents


def _production_settings_env(tmp_path):
    key = tmp_path / "backup-key.txt"
    key.write_text("backup-key-for-settings-test", encoding="utf-8")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "SECRET_KEY": "production-settings-test-" + "x" * 64,
            "EAM_ENVIRONMENT": "production",
            "DEBUG": "false",
            "ALLOWED_HOSTS": "eam.company.lan",
            "CSRF_TRUSTED_ORIGINS": "https://eam.company.lan",
            "QR_BASE_URL": "https://eam.company.lan",
            "DB_ENGINE": "postgresql",
            "DB_NAME": "eam_lite",
            "DB_USER": "eam_lite_runtime",
            "DB_PASSWORD": "test-only-database-password",
            "DB_HOST": "db",
            "DB_PORT": "5432",
            "SECURE_SSL_REDIRECT": "true",
            "SESSION_COOKIE_SECURE": "true",
            "CSRF_COOKIE_SECURE": "true",
            "TRUST_PROXY_SSL_HEADER": "true",
            "TRUST_PROXY_CLIENT_IP": "true",
            "TRUSTED_PROXY_NETWORKS": "172.16.0.0/12",
            "BACKUP_ROOT": str(tmp_path / "stage"),
            "BACKUP_TEMP_ROOT": str(tmp_path / "temp"),
            "BACKUP_MIRROR_ROOT": str(mirror),
            "BACKUP_KEY_FILE": str(key),
            "SECURE_HSTS_SECONDS": "86400",
        }
    )
    return env


def test_production_settings_fail_closed_and_valid_secure_configuration_loads(tmp_path):
    env = _production_settings_env(tmp_path)
    valid = subprocess.run(
        [
            str(Path(".venv/Scripts/python.exe")),
            "-c",
            (
                "import config.settings as s; "
                "assert s.STORAGES['staticfiles']['BACKEND'].endswith("
                "'EAMManifestStaticFilesStorage')"
            ),
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    assert valid.returncode == 0, valid.stderr
    env["SECURE_HSTS_SECONDS"] = "0"
    invalid = subprocess.run(
        [str(Path(".venv/Scripts/python.exe")), "-c", "import config.settings"],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode != 0
    assert "SECURE_HSTS_SECONDS" in invalid.stderr


def test_local_settings_are_postgresql_debug_false_and_exact_loopback(tmp_path):
    env = _production_settings_env(tmp_path)
    env.update(
        {
            "EAM_ENVIRONMENT": "local",
            "DEBUG": "false",
            "ALLOWED_HOSTS": "127.0.0.1",
            "CSRF_TRUSTED_ORIGINS": "http://127.0.0.1:8765",
            "QR_BASE_URL": "http://127.0.0.1:8765",
            "SECURE_SSL_REDIRECT": "false",
            "SESSION_COOKIE_SECURE": "false",
            "CSRF_COOKIE_SECURE": "false",
            "TRUST_PROXY_SSL_HEADER": "false",
            "TRUST_PROXY_CLIENT_IP": "false",
            "TRUSTED_PROXY_NETWORKS": "",
        }
    )
    valid = subprocess.run(
        [
            str(Path(".venv/Scripts/python.exe")),
            "-c",
            (
                "import config.settings as s; "
                "assert s.EAM_ENVIRONMENT == 'local'; "
                "assert s.DEBUG is False; "
                "assert s.DATABASE_ENGINE == 'postgresql'; "
                "assert s.QR_BASE_URL == 'http://127.0.0.1:8765'"
            ),
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    assert valid.returncode == 0, valid.stderr

    for key, value, expected in (
        ("DEBUG", "true", "DEBUG=false"),
        ("DB_ENGINE", "sqlite", "PostgreSQL"),
        ("QR_BASE_URL", "http://localhost:8765", "127.0.0.1:8765"),
    ):
        invalid_env = env.copy()
        invalid_env[key] = value
        result = subprocess.run(
            [str(Path(".venv/Scripts/python.exe")), "-c", "import config.settings"],
            cwd=Path.cwd(),
            env=invalid_env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected in result.stderr


@pytest.mark.parametrize(
    ("setting", "value", "expected_error"),
    [
        ("TRUST_PROXY_SSL_HEADER", "false", "TRUST_PROXY_SSL_HEADER"),
        ("ALLOWED_HOSTS", ".company.lan,eam.company.lan", "ALLOWED_HOSTS"),
        (
            "CSRF_TRUSTED_ORIGINS",
            "https://eam.company.lan,https://*.company.lan",
            "CSRF_TRUSTED_ORIGINS",
        ),
        (
            "CSRF_TRUSTED_ORIGINS",
            "https://eam.company.lan/unsafe/path",
            "CSRF_TRUSTED_ORIGINS",
        ),
    ],
)
def test_production_settings_reject_insecure_proxy_and_wildcard_origins(
    tmp_path, setting, value, expected_error
):
    env = _production_settings_env(tmp_path)
    env[setting] = value
    result = subprocess.run(
        [str(Path(".venv/Scripts/python.exe")), "-c", "import config.settings"],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.parametrize(
    ("qr_base_url", "allowed_hosts", "csrf_origin", "expected_error"),
    [
        (
            "https://192.168.1.111",
            "192.168.1.111",
            "https://192.168.1.111",
            "固定内网 DNS HTTPS",
        ),
        (
            "https://qr.company.lan",
            "eam.company.lan",
            "https://qr.company.lan",
            "ALLOWED_HOSTS",
        ),
        (
            "https://eam.company.lan",
            "eam.company.lan",
            "https://other.company.lan",
            "CSRF_TRUSTED_ORIGINS",
        ),
    ],
)
def test_production_qr_origin_rejects_machine_bound_or_mismatched_configuration(
    tmp_path, qr_base_url, allowed_hosts, csrf_origin, expected_error
):
    env = _production_settings_env(tmp_path)
    env.update(
        {
            "QR_BASE_URL": qr_base_url,
            "ALLOWED_HOSTS": allowed_hosts,
            "CSRF_TRUSTED_ORIGINS": csrf_origin,
        }
    )

    result = subprocess.run(
        [str(Path(".venv/Scripts/python.exe")), "-c", "import config.settings"],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_production_compose_is_version_pinned_and_does_not_publish_app_or_database():
    compose = Path("deploy/compose.yaml").read_text(encoding="utf-8")
    dockerfile = Path("deploy/Dockerfile").read_text(encoding="utf-8")
    caddy = Path("deploy/Caddyfile").read_text(encoding="utf-8")
    lock = Path("requirements/production.lock").read_text(encoding="utf-8")
    assert (
        "postgres:18.6-alpine@sha256:"
        "d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
    ) in compose
    assert (
        "caddy:2.11.4-alpine@sha256:"
        "5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
    ) in compose
    assert "python:3.14.7-slim@sha256:" in dockerfile
    assert '"80:80"' in compose and '"443:443"' in compose
    assert "5432:5432" not in compose and "8000:8000" not in compose
    assert "db_admin_password" in compose
    assert "db_migration_password" in compose
    assert "db_runtime_password" in compose
    assert "grant_runtime_database_privileges" in compose
    assert "gunicorn" in dockerfile and "runserver" not in dockerfile
    assert "--access-logfile" not in dockerfile
    assert "tls internal" in caddy and "reverse_proxy app:8000" in caddy
    assert "handle_path /static/*" in caddy
    assert "gunicorn==26.1.0" in lock and "cryptography==50.0.0" in lock


@override_settings(
    TRUST_PROXY_CLIENT_IP=True,
    TRUSTED_PROXY_NETWORKS=["172.16.0.0/12"],
)
def test_client_ip_header_is_used_only_from_trusted_proxy():
    from apps.core.middleware import TrustedProxyClientIpMiddleware

    middleware = TrustedProxyClientIpMiddleware(
        lambda request: JsonResponse({"ip": request.client_ip_address})
    )
    factory = RequestFactory()
    trusted = middleware(
        factory.get(
            "/healthz/",
            REMOTE_ADDR="172.20.0.4",
            HTTP_X_FORWARDED_FOR="192.168.1.50",
        )
    )
    assert json.loads(trusted.content)["ip"] == "192.168.1.50"
    spoofed = middleware(
        factory.get(
            "/healthz/",
            REMOTE_ADDR="203.0.113.10",
            HTTP_X_FORWARDED_FOR="192.168.1.99",
        )
    )
    assert json.loads(spoofed.content)["ip"] == "203.0.113.10"
