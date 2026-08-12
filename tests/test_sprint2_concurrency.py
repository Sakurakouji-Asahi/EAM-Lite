from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connection

from apps.coding.services import clone_scheme, set_default_scheme
from apps.masterdata.models import AssetCodingScheme, IssuedCode, SequenceCounter
from tests.test_sprint2_support import make_active, make_company, make_user


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 2 concurrency acceptance requires PostgreSQL")


@pytest.fixture(autouse=True)
def close_connections_between_threads():
    close_old_connections()
    yield
    close_old_connections()


def _switch_default(*, scheme_id, actor_id, barrier):
    close_old_connections()
    from django.contrib.auth import get_user_model

    try:
        scheme = AssetCodingScheme.objects.get(pk=scheme_id)
        actor = get_user_model().objects.get(pk=actor_id)
        barrier.wait(timeout=10)
        set_default_scheme(actor=actor, scheme=scheme)
        return "ok"
    except (ValidationError, OperationalError):
        return "rejected"
    finally:
        close_old_connections()


def _clone(*, scheme_id, actor_id, barrier):
    close_old_connections()
    from django.contrib.auth import get_user_model

    try:
        scheme = AssetCodingScheme.objects.get(pk=scheme_id)
        actor = get_user_model().objects.get(pk=actor_id)
        barrier.wait(timeout=10)
        try:
            clone = clone_scheme(actor=actor, scheme=scheme)
        except ValidationError:
            return "rejected"
        return clone.version
    finally:
        close_old_connections()


def test_concurrent_default_switch_leaves_exactly_one_current_default():
    company = make_company()
    admin = make_user("concurrent-admin", "system_admin")
    first = make_active(actor=admin, company=company, key="ONE")
    second = make_active(actor=admin, company=company, key="TWO")
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _switch_default,
                scheme_id=scheme.pk,
                actor_id=admin.pk,
                barrier=barrier,
            )
            for scheme in (first, second)
        ]
        outcomes = [future.result(timeout=30) for future in futures]

    assert outcomes.count("ok") >= 1
    defaults = AssetCodingScheme.objects.filter(
        company=company, status="active", is_default=True
    )
    assert defaults.count() == 1
    assert defaults.get().pk in {first.pk, second.pk}
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0


def test_concurrent_clones_preserve_one_linear_version_chain():
    company = make_company()
    admin = make_user("clone-admin", "system_admin")
    source = make_active(actor=admin, company=company, key="CLONE-RACE")
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _clone,
                scheme_id=source.pk,
                actor_id=admin.pk,
                barrier=barrier,
            )
            for _ in range(2)
        ]
        outcomes = [future.result(timeout=30) for future in futures]

    # Both requests start from v1.  Once one creates v2, accepting the other
    # would branch the immutable previous_version chain.  It must be rejected
    # and the caller can refresh and explicitly clone v2 if v3 is still wanted.
    assert outcomes.count(2) == 1
    assert outcomes.count("rejected") == 1
    assert list(
        AssetCodingScheme.objects.filter(
            company=company, scheme_key=source.scheme_key
        ).order_by("version").values_list("version", flat=True)
    ) == [1, 2]
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
