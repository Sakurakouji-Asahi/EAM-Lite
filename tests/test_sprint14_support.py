from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from apps.supplies.services import create_supply_document
from tests.test_sprint13_support import *  # noqa: F403


def make_user(username, *roles):
    user = get_user_model().objects.create_user(
        username=username,
        password="Valid-Password-2026!",
        display_name=username,
    )
    for role in roles:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


def make_supply_document(
    *,
    actor,
    company,
    warehouse,
    item,
    document_type="opening",
    quantity="10.0000",
    unit_cost="100.000000",
    key="s14-document",
    line_remark="",
):
    return create_supply_document(
        actor=actor,
        company=company,
        document_type=document_type,
        data={
            "business_date": date(2026, 8, 26),
            "target_warehouse": warehouse,
            "idempotency_key": key,
        },
        lines=[
            {
                "item": item,
                "quantity": Decimal(quantity),
                "entered_unit_cost": Decimal(unit_cost),
                "line_remark": line_remark,
            }
        ],
    )
