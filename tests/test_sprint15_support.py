from datetime import date
from decimal import Decimal

from apps.supplies.services import create_supply_document, post_supply_document
from tests.test_sprint14_support import *  # noqa: F403


def seed_supply_stock(
    *, actor, company, warehouse, item, quantity="10", unit_cost="100", key="s15-seed"
):
    document = create_supply_document(
        actor=actor,
        company=company,
        document_type="opening",
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
                "line_remark": "测试期初" if Decimal(unit_cost) == 0 else "",
            }
        ],
    )
    post_supply_document(document=document, actor=actor)
    return document


def make_issue_document(
    *,
    actor,
    company,
    warehouse,
    item,
    department,
    employee=None,
    quantity="1",
    key="s15-issue",
    remark="",
):
    return create_supply_document(
        actor=actor,
        company=company,
        document_type="issue",
        data={
            "business_date": date(2026, 8, 26),
            "source_warehouse": warehouse,
            "department": department,
            "employee": employee,
            "idempotency_key": key,
        },
        lines=[
            {
                "item": item,
                "quantity": Decimal(quantity),
                "entered_unit_cost": None,
                "line_remark": remark,
            }
        ],
    )
