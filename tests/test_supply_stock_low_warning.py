from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.models import Company
from apps.reports.queries import build_report_dataset
from apps.supplies.models import SupplyStockBalance, SupplyStockLedger
from apps.supplies.services import create_supply_document, post_supply_document
from tests.test_supply_stock_query_workspace import stock_query_context
from tests.test_sprint15_support import make_company, make_supply_category, make_supply_item, make_supply_warehouse, make_user


pytestmark = pytest.mark.django_db


def opening(context, items, key):
    company, actor, source, _, _, _ = context
    document = create_supply_document(actor=actor, company=company, document_type="opening",
        data={"business_date": date(2026, 8, 26), "target_warehouse": source, "idempotency_key": key},
        lines=[{"item": item, "quantity": Decimal("1.0000"), "entered_unit_cost": Decimal("1.000001")} for item in items])
    post_supply_document(actor=actor, document=document)


def set_warning(item, warehouse, minimum):
    item.default_warehouse = warehouse
    item.minimum_stock_quantity = Decimal(minimum)
    item.save(update_fields=["default_warehouse", "minimum_stock_quantity"])


def test_default_warehouse_exact_shortage_matches_existing_report_without_counting_missing_or_other_warehouses(client, stock_query_context):
    company, actor, source, target, paper, chair = stock_query_context
    set_warning(paper, source, "10.1234")
    set_warning(chair, source, "3.0001")
    missing = make_supply_item(company, paper.category, "LOW-MISSING", default_warehouse=source, minimum_stock_quantity=Decimal("9.1234"))
    equal = make_supply_item(company, paper.category, "LOW-EQUAL", default_warehouse=source, minimum_stock_quantity=Decimal("1"))
    unconfigured = make_supply_item(company, paper.category, "LOW-UNCONFIGURED", minimum_stock_quantity=Decimal("5"))
    inactive = make_supply_item(company, paper.category, "LOW-INACTIVE", default_warehouse=source, minimum_stock_quantity=Decimal("5"))
    opening(stock_query_context, [equal, unconfigured, inactive], "stock-low-special-cases")
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])
    page = client.get(reverse("supplies:stock-balance-list"))
    warnings = [row for row in page.context["page_obj"] if row.is_low_stock]
    assert {(row.item_id, row.warehouse_id): row.low_stock_shortage for row in warnings} == {
        (paper.pk, source.pk): Decimal("0.1234"), (chair.pk, source.pk): Decimal("0.0001")}
    assert page.context["stock_summary"]["low_count"] == 2
    assert not next(row for row in page.context["page_obj"] if row.warehouse_id == target.pk).is_low_stock
    assert "缺口 0.1234 个" in page.content.decode() and "缺口 0.0001 把" in page.content.decode()
    filtered = client.get(reverse("supplies:stock-balance-list"), {"quantity_state": "low"})
    assert {row.item_id for row in filtered.context["page_obj"]} == {paper.pk, chair.pk}
    report = build_report_dataset(actor=actor, company=company, report_key="supply_low_stock", filters={"low_stock_scope": "formal"})
    report_rows = {row["item_code"]: row["shortage_quantity"] for row in report.rows}
    assert report_rows == {paper.item_code: Decimal("0.1234"), chair.item_code: Decimal("0.0001"), missing.item_code: Decimal("9.1234")}
    assert "尚未入库的物品" in filtered.content.decode()
    assert client.get(filtered.context["stock_full_low_report_url"]).status_code == 200


def test_low_stock_summary_covers_all_pages_and_filter_removal_keeps_original_query(client, stock_query_context):
    company, _, source, _, paper, _ = stock_query_context
    items = [make_supply_item(company, paper.category, f"LOW-PAGE-{index:02d}",
        default_warehouse=source, minimum_stock_quantity=Decimal("1.0001")) for index in range(26)]
    opening(stock_query_context, items, "stock-low-page-opening")
    origin = reverse("supplies:stock-ledger-list") + "?q=original&page=2"
    filters = {"q": "LOW-PAGE-", "warehouse": str(source.pk), "item_type": "consumable", "return_to": origin}
    page = client.get(reverse("supplies:stock-balance-list"), {**filters, "quantity_state": "low", "page": "2"})
    assert len(page.context["page_obj"]) == 1
    assert page.context["stock_summary"]["low_count"] == 26
    assert page.context["stock_summary"]["balance_count"] == 26
    assert page.context["page_obj"][0].low_stock_shortage == Decimal("0.0001")
    assert parse_qs(urlsplit(page.context["stock_low_url"]).query) == {**{key: [value] for key, value in filters.items()}, "quantity_state": ["low"]}
    assert parse_qs(urlsplit(page.context["stock_low_remove_url"]).query) == {key: [value] for key, value in filters.items()}
    removed = client.get(page.context["stock_low_remove_url"])
    assert removed.context["selected_quantity_state"] == "" and removed.context["stock_summary"]["balance_count"] == 26
    archive = client.get(page.context["page_obj"][0].item_detail_url)
    assert archive.context["stock_return_url"] == page.wsgi_request.get_full_path()
    empty = client.get(reverse("supplies:stock-balance-list"), {**filters, "warehouse": str(stock_query_context[3].pk), "quantity_state": "low"})
    assert empty.context["stock_summary"]["low_count"] == 0 and not list(empty.context["page_obj"])


def test_low_warning_is_read_only_company_scoped_and_preserves_existing_role_permissions(client, stock_query_context):
    company, actor, source, _, paper, _ = stock_query_context
    set_warning(paper, source, "10.1234")
    other = make_company("LOW-OTHER", active=False)
    foreign_wh = make_supply_warehouse(other, "FOREIGN-WH")
    foreign_item = make_supply_item(other, make_supply_category(other), "LOW-FOREIGN", default_warehouse=foreign_wh, minimum_stock_quantity=Decimal("5"))
    Company.objects.filter(pk=company.pk).update(is_active=False)
    Company.objects.filter(pk=other.pk).update(is_active=True)
    try:
        opening((other, actor, foreign_wh, None, foreign_item, None), [foreign_item], "stock-low-foreign-opening")
    finally:
        Company.objects.filter(pk=other.pk).update(is_active=False)
        Company.objects.filter(pk=company.pk).update(is_active=True)
    client.force_login(make_user("stock-low-equipment", "equipment"))
    before = (list(SupplyStockBalance.objects.order_by("pk").values()), list(SupplyStockLedger.objects.order_by("pk").values()), AuditLog.objects.count())
    page = client.get(reverse("supplies:stock-balance-list"), {"quantity_state": "low"})
    assert page.status_code == 200 and page.context["show_cost"]
    assert page.context["stock_summary"]["amount"] == Decimal("100.00")
    assert "移动平均成本" in page.content.decode() and "库存金额" in page.content.decode()
    assert [row.item_id for row in page.context["page_obj"]] == [paper.pk]
    assert not {"average_unit_cost", "amount_on_hand"}.intersection(page.context["page_obj"][0].get_deferred_fields())
    for warehouse in (str(foreign_wh.pk), "invalid"):
        rejected = client.get(reverse("supplies:stock-balance-list"), {"quantity_state": "low", "warehouse": warehouse})
        assert rejected.context["stock_summary"]["low_count"] == 0
    assert before == (list(SupplyStockBalance.objects.order_by("pk").values()), list(SupplyStockLedger.objects.order_by("pk").values()), AuditLog.objects.count())
    client.force_login(make_user("stock-low-employee", "employee"))
    assert client.get(reverse("supplies:stock-balance-list"), {"quantity_state": "low"}).status_code == 403
