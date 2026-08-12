import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


_MISSING = object()
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def read_env(name, default=_MISSING, *, allow_blank=False):
    value = os.environ.get(name, _MISSING)
    if value is _MISSING:
        if default is _MISSING:
            raise ImproperlyConfigured(f"缺少必需环境变量：{name}")
        return default
    if not allow_blank and not value.strip():
        raise ImproperlyConfigured(f"环境变量 {name} 不得为空")
    return value


def parse_bool(value, *, name):
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f"环境变量 {name} 必须是 true/false、1/0、yes/no 或 on/off"
    )


def read_bool_env(name, default=_MISSING):
    return parse_bool(read_env(name, default), name=name)


def read_list_env(name, default="", *, required=False):
    raw = read_env(name, default, allow_blank=not required)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if required and not values:
        raise ImproperlyConfigured(f"环境变量 {name} 至少需要一个值")
    return values


def read_int_env(name, default=_MISSING, *, minimum=None, maximum=None):
    raw = read_env(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"环境变量 {name} 必须是整数") from exc
    if minimum is not None and value < minimum:
        raise ImproperlyConfigured(f"环境变量 {name} 不得小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ImproperlyConfigured(f"环境变量 {name} 不得大于 {maximum}")
    return value


def parse_database_engine(value):
    normalized = value.strip().lower()
    if normalized not in {"postgresql", "sqlite"}:
        raise ImproperlyConfigured(
            "DB_ENGINE 只能是 postgresql 或显式选择的 sqlite"
        )
    return normalized


def resolve_configured_path(base_dir, value):
    path = Path(value)
    return path if path.is_absolute() else base_dir / path
