"""Small factories shared by the Sprint 2 acceptance tests.

The module intentionally contains no pytest tests of its own.  Its filename
still follows the worker ownership boundary (``tests/test_sprint2_*.py``).
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone

from apps.coding.services import activate_scheme, create_scheme
from apps.masterdata.models import Company


PASSWORD = "Valid-Password-2026!"


def make_user(username: str, *roles: str):
    user = get_user_model().objects.create_user(
        username=username,
        password=PASSWORD,
        display_name=username,
    )
    # Transactional PostgreSQL tests flush reference rows between tests.  Do
    # not make role availability depend on the order in which the suite runs.
    groups = [Group.objects.get_or_create(name=role)[0] for role in roles]
    user.groups.set(groups)
    return user


def make_company(code: str = "C1", *, active: bool = True):
    return Company.objects.create(
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 公司",
        short_name=code,
        is_active=active,
    )


def sequence_segments(*, length: int = 4, zero_pad: bool = True):
    return [
        {
            "sequence_order": 1,
            "segment_type": "sequence",
            "fixed_value": None,
            "format_string": None,
            "sequence_length": length,
            "zero_pad": zero_pad,
        }
    ]


def standard_scheme_data(
    *,
    key: str = "ASSET",
    name: str | None = None,
    sequence_start: int = 1,
    effective_from=None,
    effective_to=None,
    reset_mode: str = "never",
    category_scope_level=None,
):
    if effective_from is None:
        effective_from = timezone.localdate()
    return {
        "scheme_key": key,
        "name": name or key,
        "description": "Sprint 2 test scheme",
        "reset_mode": reset_mode,
        "sequence_start": sequence_start,
        "category_scope_level": category_scope_level,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


def make_draft(
    *,
    actor,
    company,
    key: str = "ASSET",
    name: str | None = None,
    sequence_start: int = 1,
    effective_from=None,
    effective_to=None,
    reset_mode: str = "never",
    category_scope_level=None,
    segments=None,
):
    return create_scheme(
        actor=actor,
        company=company,
        data=standard_scheme_data(
            key=key,
            name=name,
            sequence_start=sequence_start,
            effective_from=effective_from,
            effective_to=effective_to,
            reset_mode=reset_mode,
            category_scope_level=category_scope_level,
        ),
        segments=sequence_segments() if segments is None else segments,
    )


def make_active(**kwargs):
    actor = kwargs["actor"]
    draft = make_draft(**kwargs)
    return activate_scheme(actor=actor, scheme=draft)


def expired_interval():
    today = timezone.localdate()
    return today - timedelta(days=10), today - timedelta(days=1)
