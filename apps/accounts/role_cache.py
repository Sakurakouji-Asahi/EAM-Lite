"""Role lookup reuse limited to one read-only HTTP request."""
from contextlib import contextmanager
from contextvars import ContextVar

_request_roles = ContextVar("eam_request_roles", default=None)
_request_values = ContextVar("eam_request_values", default=None)


@contextmanager
def cache_request_roles():
    token = _request_roles.set({})
    values_token = _request_values.set({})
    try:
        yield
    finally:
        _request_values.reset(values_token)
        _request_roles.reset(token)


def load_request_value(key, loader):
    """Reuse one read-only lookup, such as the current company, per request."""
    cache = _request_values.get()
    if cache is None:
        return loader()
    if key not in cache:
        cache[key] = loader()
    return cache[key]


def load_role_names(user, loader):
    cache = _request_roles.get()
    if cache is None:
        return set(loader())
    key = (user._state.db, user.pk)
    if key not in cache:
        cache[key] = frozenset(loader())
    return set(cache[key])
