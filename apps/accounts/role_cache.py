"""Role lookup reuse limited to one read-only HTTP request."""
from contextlib import contextmanager
from contextvars import ContextVar

_request_roles = ContextVar("eam_request_roles", default=None)


@contextmanager
def cache_request_roles():
    token = _request_roles.set({})
    try:
        yield
    finally:
        _request_roles.reset(token)


def load_role_names(user, loader):
    cache = _request_roles.get()
    if cache is None:
        return set(loader())
    key = (user._state.db, user.pk)
    if key not in cache:
        cache[key] = frozenset(loader())
    return set(cache[key])
