import runpy
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from config.env import parse_bool, parse_database_engine, read_int_env, read_secret_env


def _load_settings(monkeypatch, *, debug, qr_base_url):
    monkeypatch.setenv("SECRET_KEY", "settings-test-secret")
    monkeypatch.setenv("DEBUG", str(debug).lower())
    monkeypatch.setenv("ALLOWED_HOSTS", "localhost")
    monkeypatch.setenv("DB_ENGINE", "sqlite")
    monkeypatch.setenv("QR_BASE_URL", qr_base_url)
    return runpy.run_path(Path(__file__).parents[1] / "config" / "settings.py")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_strict_boolean_parser_accepts_only_documented_values(value, expected):
    assert parse_bool(value, name="DEBUG") is expected


@pytest.mark.parametrize("value", ["", "enabled", "2", "truthy"])
def test_strict_boolean_parser_rejects_ambiguous_values(value):
    with pytest.raises(ImproperlyConfigured):
        parse_bool(value, name="DEBUG")


def test_database_engine_accepts_only_postgresql_or_explicit_sqlite():
    assert parse_database_engine("postgresql") == "postgresql"
    assert parse_database_engine("sqlite") == "sqlite"
    with pytest.raises(ImproperlyConfigured):
        parse_database_engine("mysql")
    with pytest.raises(ImproperlyConfigured):
        parse_database_engine("")


def test_business_locale_timezone_currency_and_selected_database():
    assert settings.LANGUAGE_CODE == "zh-hans"
    assert settings.TIME_ZONE == "Asia/Shanghai"
    assert settings.USE_TZ is True
    assert settings.BUSINESS_CURRENCY == "CNY"
    assert settings.QR_BASE_URL.startswith("https://")
    assert connection.vendor == settings.DATABASE_ENGINE
    assert settings.LOGIN_FAILURE_WINDOW_SECONDS > 0
    assert settings.LOGIN_FAILURE_PAIR_LIMIT > 0


def test_django_server_access_logs_use_the_redacting_formatter():
    server_logger = settings.LOGGING["loggers"]["django.server"]

    assert server_logger["handlers"] == ["console"]
    assert server_logger["propagate"] is False
    assert settings.LOGGING["handlers"]["console"]["formatter"] == "safe"


def test_local_launcher_readiness_uses_health_endpoint_not_visible_login_copy():
    launcher = Path("scripts/start_eam_lite_local.ps1").read_text(encoding="utf-8")

    assert '${localUrl}healthz/' in launcher
    assert '"status"\\s*:\\s*"ok"' in launcher
    assert "登录 EAM-Lite" not in launcher


def test_secret_can_come_from_external_file_but_never_both_sources(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("file-secret-value\n", encoding="utf-8")
    monkeypatch.delenv("SPRINT12_SECRET", raising=False)
    monkeypatch.setenv("SPRINT12_SECRET_FILE", str(secret_file))
    assert read_secret_env("SPRINT12_SECRET") == "file-secret-value"
    monkeypatch.setenv("SPRINT12_SECRET", "direct-secret")
    with pytest.raises(ImproperlyConfigured, match="只能配置其中一个"):
        read_secret_env("SPRINT12_SECRET")


def test_qr_base_url_allows_http_in_debug(monkeypatch):
    loaded = _load_settings(
        monkeypatch,
        debug=True,
        qr_base_url="http://192.168.1.10:8765",
    )

    assert loaded["QR_BASE_URL"] == "http://192.168.1.10:8765"
    assert loaded["QR_BASE_URL_IS_DURABLE"] is False


def test_stable_dns_https_qr_base_is_marked_durable(monkeypatch):
    loaded = _load_settings(
        monkeypatch,
        debug=False,
        qr_base_url="https://eam.company.lan",
    )

    assert loaded["QR_BASE_URL_IS_DURABLE"] is True


def test_qr_base_url_rejects_http_outside_debug(monkeypatch):
    with pytest.raises(ImproperlyConfigured):
        _load_settings(
            monkeypatch,
            debug=False,
            qr_base_url="http://192.168.1.10:8765",
        )


def test_private_attachment_and_upload_temp_roots_are_separate_from_static():
    roots = [
        Path(settings.STATIC_ROOT).resolve(),
        Path(settings.MEDIA_ROOT).resolve(),
        Path(settings.IMPORT_TEMP_ROOT).resolve(),
    ]
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            assert first != second
            assert first not in second.parents
            assert second not in first.parents
    assert Path(settings.FILE_UPLOAD_TEMP_DIR).resolve() == roots[2]
    assert settings.STORAGES["default"]["BACKEND"].endswith(
        "PrivateFileSystemStorage"
    )


@pytest.mark.parametrize("value", ["", "1.5", "true", "0", "-1"])
def test_login_limit_integer_environment_values_are_strictly_positive(
    monkeypatch, value
):
    monkeypatch.setenv("TEST_LOGIN_LIMIT", value)
    with pytest.raises(ImproperlyConfigured):
        read_int_env("TEST_LOGIN_LIMIT", minimum=1)
