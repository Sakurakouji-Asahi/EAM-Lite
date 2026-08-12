from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from config.env import (
    parse_database_engine,
    read_bool_env,
    read_env,
    read_int_env,
    read_list_env,
    resolve_configured_path,
)


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = read_env("SECRET_KEY")
DEBUG = read_bool_env("DEBUG")
ALLOWED_HOSTS = read_list_env("ALLOWED_HOSTS", required=True)
CSRF_TRUSTED_ORIGINS = read_list_env(
    "CSRF_TRUSTED_ORIGINS", default="", required=False
)

if not DEBUG and "*" in ALLOWED_HOSTS:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("DEBUG=false 时 ALLOWED_HOSTS 不得包含通配符 *")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.audit.apps.AuditConfig",
    "apps.masterdata.apps.MasterDataConfig",
    "apps.coding.apps.CodingConfig",
    "apps.imports.apps.ImportsConfig",
    "apps.core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.masterdata.context_processors.masterdata_navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASE_ENGINE = parse_database_engine(read_env("DB_ENGINE", "postgresql"))
if DATABASE_ENGINE == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": read_env("DB_NAME"),
            "USER": read_env("DB_USER"),
            "PASSWORD": read_env("DB_PASSWORD"),
            "HOST": read_env("DB_HOST"),
            "PORT": read_int_env("DB_PORT", 5432, minimum=1, maximum=65535),
            "CONN_MAX_AGE": 0,
        }
    }
else:
    sqlite_path = read_env("SQLITE_PATH", "var/dev.sqlite3")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": (
                sqlite_path
                if sqlite_path == ":memory:"
                else resolve_configured_path(BASE_DIR, sqlite_path)
            ),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation.MinimumLengthValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation.CommonPasswordValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation.NumericPasswordValidator"
        )
    },
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

BUSINESS_CURRENCY = read_env("BUSINESS_CURRENCY", "CNY").upper()
if BUSINESS_CURRENCY != "CNY":
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("Sprint 0 的 BUSINESS_CURRENCY 必须为 CNY")

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = resolve_configured_path(
    BASE_DIR, read_env("STATIC_ROOT", "var/static")
)
MEDIA_ROOT = resolve_configured_path(BASE_DIR, read_env("MEDIA_ROOT", "var/media"))
# Django may spool uploads larger than FILE_UPLOAD_MAX_MEMORY_SIZE here.  It is
# deliberately separate from both static files and the durable attachment
# store so a reverse proxy cannot accidentally publish an in-flight upload.
IMPORT_TEMP_ROOT = resolve_configured_path(
    BASE_DIR, read_env("IMPORT_TEMP_ROOT", "var/tmp")
)
IMPORT_TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def _path_contains(parent, child):
    parent = Path(parent).resolve()
    child = Path(child).resolve()
    return child == parent or parent in child.parents


for _private_root, _public_root, _label in (
    (MEDIA_ROOT, STATIC_ROOT, "MEDIA_ROOT"),
    (IMPORT_TEMP_ROOT, STATIC_ROOT, "IMPORT_TEMP_ROOT"),
    (IMPORT_TEMP_ROOT, MEDIA_ROOT, "IMPORT_TEMP_ROOT"),
):
    if _path_contains(_public_root, _private_root) or _path_contains(
        _private_root, _public_root
    ):
        raise ImproperlyConfigured(
            f"{_label} 必须与静态或附件目录完全分离，不能相同或互相嵌套"
        )

MEDIA_URL = "/protected-media/"
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 21 * 1024 * 1024
FILE_UPLOAD_TEMP_DIR = str(IMPORT_TEMP_ROOT)

STORAGES = {
    "default": {
        "BACKEND": "apps.core.storage.PrivateFileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
LOGIN_FAILURE_WINDOW_SECONDS = read_int_env(
    "LOGIN_FAILURE_WINDOW_SECONDS", 900, minimum=1
)
LOGIN_FAILURE_PAIR_LIMIT = read_int_env(
    "LOGIN_FAILURE_PAIR_LIMIT", 5, minimum=1
)

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = read_bool_env("SECURE_SSL_REDIRECT", "false")
SESSION_COOKIE_SECURE = read_bool_env("SESSION_COOKIE_SECURE", "false")
CSRF_COOKIE_SECURE = read_bool_env("CSRF_COOKIE_SECURE", "false")
if read_bool_env("TRUST_PROXY_SSL_HEADER", "false"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

LOG_LEVEL = read_env("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "LOG_LEVEL 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL"
    )

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "safe": {
            "()": "apps.core.logging.RedactingFormatter",
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "safe",
        }
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        }
    },
}
