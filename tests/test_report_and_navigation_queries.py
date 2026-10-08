"""Report rows and page navigation keep database work independent of row count."""
import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.masterdata.models import Location
from apps.masterdata.permissions import company_initialized, current_company
from apps.reports.queries import build_report_dataset
from tests.test_unified_asset_identity import context, registered  # noqa: F401


pytestmark = pytest.mark.django_db


def _ledger(context):
    with CaptureQueriesContext(connection) as queries:
        dataset = build_report_dataset(
            actor=context["finance"], company=context["company"], report_key="asset_ledger"
        )
    return dataset, len(queries)


def test_ledger_location_paths_use_bounded_queries_and_keep_full_path(context):
    deep = Location.objects.create(
        company=context["company"], code="DEEP", normalized_code="deep",
        name="深层工位", parent=context["location"], location_type="position",
    )
    registered(context, key="path-first", location=deep)
    small, small_queries = _ledger(context)
    for index in range(6):
        registered(context, key=f"path-{index}", location=deep)
    large, large_queries = _ledger(context)
    expected = f"UNIFIED-L1 位置 / UNIFIED-L2 位置 / {context['location'].name} / 深层工位"
    assert small.row_count == 1 and large.row_count == 7
    assert {row["location"] for row in large.rows} == {expected}
    assert large_queries == small_queries


def test_report_snapshot_runs_inside_a_caller_transaction(context):
    registered(context, key="outer-transaction")
    with transaction.atomic():
        context["company"].refresh_from_db()
        dataset = build_report_dataset(
            actor=context["finance"], company=context["company"], report_key="asset_ledger"
        )
    assert dataset.row_count == 1


def test_read_only_page_reuses_company_and_initialization_lookups(context, client):
    client.force_login(context["finance"])
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("assets:asset-list"))
    assert response.status_code == 200
    company_reads = [q for q in queries.captured_queries if 'FROM "masterdata_company"' in q["sql"]]
    init_reads = [
        q for q in queries.captured_queries if 'FROM "masterdata_initializationsetting"' in q["sql"]
    ]
    assert len(company_reads) <= 2
    assert len(init_reads) <= 2


def test_company_lookup_is_not_reused_outside_a_request(context):
    company = current_company()
    assert company == context["company"]
    assert company_initialized(company)
    company.is_active = False
    company.save(update_fields=["is_active"])
    assert current_company() is None
    assert current_company(include_inactive=True) == context["company"]
    assert company_initialized(None) is False
