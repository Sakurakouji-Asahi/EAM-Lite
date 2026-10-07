from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.supplies.models import SupplyStockLedger
from apps.supplies.services import publish_supply_count_task
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import seed_supply_stock
from tests.test_sprint17_services import make_count


pytestmark = pytest.mark.django_db


@pytest.fixture
def count_entry(client):
    company, actor, _, _, warehouse, _, first, second = supply_context()
    for index, (item, quantity) in enumerate(((first, "5.1234"), (second, "0.0002"))):
        seed_supply_stock(actor=actor, company=company, warehouse=warehouse, item=item,
            quantity=quantity, unit_cost="10", key=f"assistant-count-stock-{index}")
    task = make_count(actor=actor, company=company, domain="warehouse_stock",
        warehouse=warehouse, key="assistant-count-task")
    publish_supply_count_task(actor=actor, task=task)
    client.force_login(actor)
    return task


def test_count_summary_shortcuts_preserve_search_and_size_and_empty_search_is_useful(client, count_entry):
    task = count_entry
    item_code = task.lines.order_by("item_code_snapshot").first().item_code_snapshot
    url = reverse("supplies:count-task-bulk-entry", args=[task.pk])
    page = client.get(url, {"q": item_code, "row_view": "unrecorded", "page_size": 50, "page": 7})
    assert page.status_code == 200
    assert len(page.context["row_forms"]) == 1
    assert page.context["count_summary"]["total"] == 2
    for scope, link in page.context["count_query_links"].items():
        query = parse_qs(urlsplit(link).query, keep_blank_values=True)
        assert query["q"] == [item_code] and query["page_size"] == ["50"]
        assert query["row_view"] == ["" if scope == "all" else scope]
        assert "page" not in query
    html = page.content.decode()
    assert "supply-count-entry.js?v=20261004" in html
    for marker in ("data-count-entry", "data-count-unit", "data-count-expected", "data-count-next-blank", "data-count-next-reason"):
        assert marker in html
    blank = client.get(url, {"q": "DOES-NOT-MATCH-ANY-COUNT-ITEM"})
    assert blank.status_code == 200
    assert "当前筛选下没有可录入的明细" in blank.content.decode()
    assert "line_manifest" not in blank.content.decode()
    assert client.get(url, {"page_size": 999999}).status_code == 400


def test_bulk_error_links_keep_zero_and_other_row_input_without_partial_save(client, count_entry):
    task = count_entry
    url = reverse("supplies:count-task-bulk-entry", args=[task.pk])
    page = client.get(url)
    first, second = page.context["row_forms"]
    before = SupplyStockLedger.objects.count()
    audit_before = AuditLog.objects.filter(action="supply_count_record").count()
    response = client.post(url, {"line_manifest": page.context["line_manifest"],
        f"{first.prefix}-counted_quantity": "0", f"{first.prefix}-expected_counted_at": "",
        f"{second.prefix}-counted_quantity": str(second.line.expected_quantity),
        f"{second.prefix}-remark": "现场复核完成", f"{second.prefix}-expected_counted_at": ""})
    assert response.status_code == 200
    assert [row.line.pk for row in response.context["entry_error_rows"]] == [first.line.pk]
    returned = {row.line.pk: row for row in response.context["row_forms"]}
    assert returned[first.line.pk]["counted_quantity"].value() == "0"
    assert returned[second.line.pk]["remark"].value() == "现场复核完成"
    assert returned[second.line.pk]["counted_quantity"].value() == str(second.line.expected_quantity)
    html = response.content.decode()
    assert f'href="#count-row-{first.line.pk}"' in html
    assert "1 行需要修正，当前输入已保留，本页尚未保存" in html
    assert 'data-unsaved-guard="true"' in html
    assert not task.lines.filter(counted_quantity__isnull=False).exists()
    assert SupplyStockLedger.objects.count() == before
    assert AuditLog.objects.filter(action="supply_count_record").count() == audit_before


def test_single_entry_retains_back_query_and_adds_unsaved_difference_context(client, count_entry):
    task = count_entry
    line = task.lines.order_by("item_code_snapshot").first()
    url = reverse("supplies:count-line-record", args=[task.pk, line.pk])
    query = "q=CHAIR&row_view=unrecorded&page_size=50&page=2"
    page = client.get(url, {"return_query": query})
    assert page.status_code == 200
    html = page.content.decode()
    assert "supply-count-entry.js?v=20261004" in html
    assert "data-count-feedback" in html and "data-count-unit-totals" in html
    assert task.task_no in html and line.item_code_snapshot in html
    assert 'data-unsaved-guard="false"' in html
    assert page.context["return_query"] == query
    error = client.post(url, {"counted_quantity": "-1", "remark": "保留的现场备注",
        "return_query": query, "expected_counted_at": ""})
    assert error.status_code == 200
    assert error.context["form"]["counted_quantity"].value() == "-1"
    assert error.context["form"]["remark"].value() == "保留的现场备注"
    assert error.context["return_query"] == query
    assert 'data-unsaved-guard="true"' in error.content.decode()
    line.refresh_from_db()
    assert line.counted_quantity is None


def test_count_preview_is_exact_zero_is_recorded_and_opposite_differences_do_not_hide_rows():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for browser quantity verification")
    script = r"""
const assert = require('node:assert/strict');
const {parseQuantity, formatQuantity, inspectLine, summarizeLines} = require(process.argv[1]);
assert.equal(parseQuantity('0'), 0n);
assert.equal(parseQuantity('1e-4'), 1n);
assert.equal(parseQuantity('99999999999999.9999'), 999999999999999999n);
for (const input of ['-1', '1.00001', 'NaN', 'Infinity', '1e99', '100000000000000']) assert.equal(parseQuantity(input), null);
assert.equal(formatQuantity(-1n), '-0.0001');
assert.equal(inspectLine({expected:'1',counted:'',remark:''}).state, 'blank');
assert.equal(inspectLine({expected:'1',counted:'0',remark:'盘亏'}).state, 'different');
assert.equal(inspectLine({expected:'0',counted:'0',remark:''}).state, 'same');
assert.equal(inspectLine({expected:'1',counted:'1',badInput:true}).state, 'invalid');
const summary = summarizeLines([
  {expected:'0.1',counted:'0.2',unit:'盒',remark:''},
  {expected:'0.2',counted:'0.1',unit:'盒',remark:'复核后确认盘亏'},
  {expected:'0.0002',counted:'0',unit:'把',remark:'现场无实物'},
  {expected:'10',counted:'',unit:'盒'},
  {expected:'3',counted:'1.00001',unit:'把'},
]);
assert.equal(summary.recorded, 3);
assert.equal(summary.blank, 1);
assert.equal(summary.invalid, 1);
assert.equal(summary.different, 3);
assert.equal(summary.needsReason, 1);
assert.equal(summary.units.size, 2);
assert.deepEqual(summary.units.get('盒'), {expected:3000n,counted:3000n,difference:0n});
assert.deepEqual(summary.units.get('把'), {expected:2n,counted:0n,difference:-2n});
assert.equal(formatQuantity(summary.units.get('盒').counted), '0.3');
"""
    result = subprocess.run([node, "-e", script, str(Path(settings.BASE_DIR) / "static/js/supply-count-entry.js")],
        check=False, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
