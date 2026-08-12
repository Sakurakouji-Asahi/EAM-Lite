import pytest
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from config.env import parse_bool, parse_database_engine, read_int_env


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
    assert connection.vendor == settings.DATABASE_ENGINE
    assert settings.LOGIN_FAILURE_WINDOW_SECONDS > 0
    assert settings.LOGIN_FAILURE_PAIR_LIMIT > 0


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
