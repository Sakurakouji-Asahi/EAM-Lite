import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from config.env import (
    parse_database_engine,
    read_bool_env,
    read_env,
    read_int_env,
    read_list_env,
    read_secret_env,
    resolve_configured_path,
)


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = read_secret_env("SECRET_KEY")
DEBUG = read_bool_env("DEBUG")
EAM_ENVIRONMENT = read_env("EAM_ENVIRONMENT", "development").strip().lower()
if EAM_ENVIRONMENT not in {"development", "test", "production"}:
    raise ImproperlyConfigured(
        "EAM_ENVIRONMENT 必须是 development、test 或 production"
    )
ALLOWED_HOSTS = read_list_env("ALLOWED_HOSTS", required=True)
CSRF_TRUSTED_ORIGINS = read_list_env(
    "CSRF_TRUSTED_ORIGINS", default="", required=False
)

if not DEBUG and "*" in ALLOWED_HOSTS:
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
    "apps.assets.apps.AssetsConfig",
    "apps.finance.apps.FinanceConfig",
    "apps.inventory.apps.InventoryConfig",
    "apps.maintenance.apps.MaintenanceConfig",
    "apps.offboarding.apps.OffboardingConfig",
    "apps.supplies.apps.SuppliesConfig",
    "apps.imports.apps.ImportsConfig",
    "apps.reports.apps.ReportsConfig",
    "apps.operations.apps.OperationsConfig",
    "apps.core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.TrustedProxyClientIpMiddleware",
    "apps.core.middleware.CorrelationIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.core.middleware.QrOpaqueOriginCsrfCompatibilityMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.operations.middleware.BackupWriteFreezeMiddleware",
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
                "apps.assets.context_processors.asset_navigation",
                "apps.finance.context_processors.finance_navigation",
                "apps.inventory.context_processors.inventory_navigation",
                "apps.maintenance.context_processors.maintenance_navigation",
                "apps.offboarding.context_processors.offboarding_navigation",
                "apps.supplies.context_processors.supplies_navigation",
                "apps.reports.context_processors.report_navigation",
                "apps.operations.context_processors.operations_navigation",
                "apps.core.context_processors.application_navigation",
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
            "PASSWORD": read_secret_env("DB_PASSWORD"),
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
    raise ImproperlyConfigured("Sprint 0 的 BUSINESS_CURRENCY 必须为 CNY")

# Printed QR codes are durable identifiers.  The deployment URL therefore
# comes from configuration rather than the incoming Host header.  Production
# must point this at the approved LAN HTTPS name; the local default keeps
# development and automated tests deterministic without an external service.
QR_BASE_URL = read_env("QR_BASE_URL", "https://localhost").rstrip("/")
_qr_base = urlsplit(QR_BASE_URL)
_allowed_qr_schemes = {"http", "https"} if DEBUG else {"https"}
try:
    _qr_port = _qr_base.port
except ValueError as exc:
    raise ImproperlyConfigured("QR_BASE_URL 端口无效") from exc
if (
    _qr_base.scheme not in _allowed_qr_schemes
    or not _qr_base.hostname
    or _qr_base.username is not None
    or _qr_base.password is not None
    or _qr_base.query
    or _qr_base.fragment
    or _qr_base.path not in {"", "/"}
):
    raise ImproperlyConfigured(
        "QR_BASE_URL 必须是无凭据、查询参数和路径的内网应用根地址；"
        "仅 DEBUG=true 时允许 http://"
    )
try:
    ipaddress.ip_address(_qr_base.hostname)
except ValueError:
    _qr_hostname_is_ip = False
else:
    _qr_hostname_is_ip = True
_qr_hostname = _qr_base.hostname.casefold()
QR_BASE_URL_IS_DURABLE = (
    _qr_base.scheme == "https"
    and _qr_port is None
    and not _qr_hostname_is_ip
    and _qr_hostname != "localhost"
    and not _qr_hostname.endswith(".localhost")
    and "." in _qr_hostname.strip(".")
)

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
BACKUP_ROOT = resolve_configured_path(
    BASE_DIR, read_env("BACKUP_ROOT", "var/backups")
)
BACKUP_TEMP_ROOT = resolve_configured_path(
    BASE_DIR, read_env("BACKUP_TEMP_ROOT", "var/backup-tmp")
)
_backup_mirror_value = read_env(
    "BACKUP_MIRROR_ROOT", "", allow_blank=True
).strip()
BACKUP_MIRROR_ROOT = (
    resolve_configured_path(BASE_DIR, _backup_mirror_value)
    if _backup_mirror_value
    else None
)
BACKUP_RETENTION_DAYS = read_int_env(
    "BACKUP_RETENTION_DAYS", 30, minimum=30, maximum=3650
)
BACKUP_DOWNLOAD_GRANT_MINUTES = read_int_env(
    "BACKUP_DOWNLOAD_GRANT_MINUTES", 10, minimum=1, maximum=60
)
BACKUP_RECENT_AUTH_MINUTES = read_int_env(
    "BACKUP_RECENT_AUTH_MINUTES", 15, minimum=1, maximum=120
)
BACKUP_MAX_AGE_HOURS = read_int_env(
    "BACKUP_MAX_AGE_HOURS", 24, minimum=1, maximum=168
)
BACKUP_KEY_FILE = read_env("BACKUP_KEY_FILE", "", allow_blank=True).strip()
BACKUP_PG_MODE = read_env("BACKUP_PG_MODE", "native").strip().lower()
BACKUP_PG_DUMP_BIN = read_env("BACKUP_PG_DUMP_BIN", "pg_dump")
BACKUP_PG_RESTORE_BIN = read_env("BACKUP_PG_RESTORE_BIN", "pg_restore")
BACKUP_DOCKER_BIN = read_env("BACKUP_DOCKER_BIN", "docker")
BACKUP_POSTGRES_CONTAINER = read_env(
    "BACKUP_POSTGRES_CONTAINER", "eam-lite-sprint0-pg"
)
APP_COMMIT_SHA = read_env("APP_COMMIT_SHA", "unknown", allow_blank=True).strip()
DATABASE_RUNTIME_ROLE = read_env(
    "DATABASE_RUNTIME_ROLE", "", allow_blank=True
).strip()


def _path_contains(parent, child):
    parent = Path(parent).resolve()
    child = Path(child).resolve()
    return child == parent or parent in child.parents


for _private_root, _public_root, _label in (
    (MEDIA_ROOT, STATIC_ROOT, "MEDIA_ROOT"),
    (IMPORT_TEMP_ROOT, STATIC_ROOT, "IMPORT_TEMP_ROOT"),
    (IMPORT_TEMP_ROOT, MEDIA_ROOT, "IMPORT_TEMP_ROOT"),
    (BACKUP_ROOT, STATIC_ROOT, "BACKUP_ROOT"),
    (BACKUP_ROOT, MEDIA_ROOT, "BACKUP_ROOT"),
    (BACKUP_ROOT, IMPORT_TEMP_ROOT, "BACKUP_ROOT"),
    (BACKUP_TEMP_ROOT, STATIC_ROOT, "BACKUP_TEMP_ROOT"),
    (BACKUP_TEMP_ROOT, MEDIA_ROOT, "BACKUP_TEMP_ROOT"),
    (BACKUP_TEMP_ROOT, IMPORT_TEMP_ROOT, "BACKUP_TEMP_ROOT"),
    (BACKUP_TEMP_ROOT, BACKUP_ROOT, "BACKUP_TEMP_ROOT"),
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
        "BACKEND": (
            "apps.core.storage.EAMManifestStaticFilesStorage"
            if EAM_ENVIRONMENT == "production"
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
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
SESSION_COOKIE_AGE = read_int_env("SESSION_COOKIE_AGE", 28_800, minimum=300)
SESSION_EXPIRE_AT_BROWSER_CLOSE = read_bool_env(
    "SESSION_EXPIRE_AT_BROWSER_CLOSE", "true"
)
SECURE_SSL_REDIRECT = read_bool_env("SECURE_SSL_REDIRECT", "false")
SESSION_COOKIE_SECURE = read_bool_env("SESSION_COOKIE_SECURE", "false")
CSRF_COOKIE_SECURE = read_bool_env("CSRF_COOKIE_SECURE", "false")
if read_bool_env("TRUST_PROXY_SSL_HEADER", "false"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
TRUST_PROXY_CLIENT_IP = read_bool_env("TRUST_PROXY_CLIENT_IP", "false")
TRUSTED_PROXY_NETWORKS = read_list_env(
    "TRUSTED_PROXY_NETWORKS", default="", required=False
)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = read_int_env("SECURE_HSTS_SECONDS", 0, minimum=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = read_bool_env(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", "false"
)
SECURE_HSTS_PRELOAD = read_bool_env("SECURE_HSTS_PRELOAD", "false")

if EAM_ENVIRONMENT == "production":
    if len(SECRET_KEY) < 50:
        raise ImproperlyConfigured("production SECRET_KEY 至少需要 50 个字符")
    if DEBUG:
        raise ImproperlyConfigured("production 环境必须 DEBUG=false")
    if DATABASE_ENGINE != "postgresql":
        raise ImproperlyConfigured("production 环境必须使用 PostgreSQL")
    if not QR_BASE_URL_IS_DURABLE:
        raise ImproperlyConfigured(
            "production QR_BASE_URL 必须是无端口的固定内网 DNS HTTPS 根地址；"
            "不得使用 IP、localhost 或临时主机名"
        )
    if "*" in ALLOWED_HOSTS or _qr_hostname not in {
        host.casefold() for host in ALLOWED_HOSTS
    }:
        raise ImproperlyConfigured(
            "production QR_BASE_URL 主机名必须精确列入 ALLOWED_HOSTS，且不得使用通配符"
        )
    if QR_BASE_URL not in {origin.rstrip("/") for origin in CSRF_TRUSTED_ORIGINS}:
        raise ImproperlyConfigured(
            "production QR_BASE_URL 必须精确列入 CSRF_TRUSTED_ORIGINS"
        )
    if not (SECURE_SSL_REDIRECT and SESSION_COOKIE_SECURE and CSRF_COOKIE_SECURE):
        raise ImproperlyConfigured(
            "production 环境必须启用 SSL 重定向和 Secure Session/CSRF Cookie"
        )
    if not TRUST_PROXY_CLIENT_IP or not TRUSTED_PROXY_NETWORKS:
        raise ImproperlyConfigured(
            "production 环境必须配置受信任反向代理网络以记录真实客户端 IP"
        )
    if not CSRF_TRUSTED_ORIGINS or any(
        not origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS
    ):
        raise ImproperlyConfigured(
            "production 环境必须配置准确的 HTTPS CSRF_TRUSTED_ORIGINS"
        )
    if SECURE_HSTS_SECONDS <= 0:
        raise ImproperlyConfigured("production 环境必须显式配置 SECURE_HSTS_SECONDS")
    if not BACKUP_KEY_FILE or not BACKUP_MIRROR_ROOT:
        raise ImproperlyConfigured(
            "production 环境必须配置仓库外 BACKUP_KEY_FILE 和独立 BACKUP_MIRROR_ROOT"
        )
    if not Path(BACKUP_KEY_FILE).is_file():
        raise ImproperlyConfigured("production BACKUP_KEY_FILE 必须是可读的外部文件")
    if not Path(BACKUP_MIRROR_ROOT).is_dir():
        raise ImproperlyConfigured("production BACKUP_MIRROR_ROOT 必须是已挂载目录")
    _production_backup_roots = (
        Path(BACKUP_ROOT).resolve(),
        Path(BACKUP_TEMP_ROOT).resolve(),
        Path(BACKUP_MIRROR_ROOT).resolve(),
    )
    for _index, _first in enumerate(_production_backup_roots):
        for _second in _production_backup_roots[_index + 1 :]:
            if (
                _first == _second
                or _first in _second.parents
                or _second in _first.parents
            ):
                raise ImproperlyConfigured(
                    "production 备份暂存、临时和镜像目录必须完全分离"
                )

LOG_LEVEL = read_env("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
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
        "django.server": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        }
    },
}
