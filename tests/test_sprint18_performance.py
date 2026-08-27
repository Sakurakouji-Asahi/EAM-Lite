import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.reports.queries import build_report_dataset
from apps.reports.supply_queries import build_supply_dashboard
from apps.supplies.models import SupplyStockLedger
from tests.test_sprint18_reports import report_context


pytestmark = pytest.mark.django_db


def test_dashboard_and_report_page_query_counts_are_bounded():
    context = report_context()
    with CaptureQueriesContext(connection) as dashboard_queries:
        dashboard = build_supply_dashboard(
            actor=context["warehouse_user"], company=context["company"]
        )
    assert dashboard["stock_combination_count"] == 2
    assert len(dashboard_queries) <= 25

    with CaptureQueriesContext(connection) as ledger_queries:
        dataset = build_report_dataset(
            actor=context["warehouse_user"],
            company=context["company"],
            report_key="supply_stock_ledger",
            filters={},
        )
        first_page = dataset.rows[:50]
    assert len(first_page) == 4
    assert len(ledger_queries) <= 12

    with CaptureQueriesContext(connection) as custody_queries:
        custody = build_report_dataset(
            actor=context["warehouse_user"],
            company=context["company"],
            report_key="supply_custody_balance",
            filters={},
        )
        assert len(custody.rows[:50]) == 1
    assert len(custody_queries) <= 12


def test_stock_ledger_pagination_index_is_tracked():
    names = {index.name for index in SupplyStockLedger._meta.indexes}
    assert "supply_ledger_company_time_idx" in names


def test_performance_generator_defaults_to_no_write():
    with pytest.raises(CommandError, match="默认不写入"):
        call_command(
            "benchmark_supply_reports",
            items=10,
            warehouses=1,
            users=1,
            ledgers=10,
            custodies=1,
        )
