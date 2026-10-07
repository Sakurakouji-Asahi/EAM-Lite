from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest
from django.conf import settings
from django.urls import reverse

from apps.supplies.forms import SupplyDocumentLineEntryForm
from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_company, make_supply_category, make_supply_item


class ItemOptions(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.options = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "option" and attrs.get("value"):
            self.options[attrs["value"]] = attrs


@pytest.mark.django_db
def test_line_item_unit_metadata_uses_only_current_company_active_choices():
    company, actor, _, _, _, _, paper, chair = supply_context()
    paper.unit = '盒 & "大" <20>'
    paper.save(update_fields=["unit"])
    inactive = make_supply_item(company, paper.category, "DISABLED", is_active=False)
    other = make_company("OTHER", active=False)
    foreign = make_supply_item(other, make_supply_category(other), "FOREIGN")
    form = SupplyDocumentLineEntryForm(actor=actor, company=company, document_type="issue")
    options = ItemOptions(str(form["item"])).options
    assert set(options) == {str(paper.pk), str(chair.pk)}
    assert options[str(paper.pk)]["data-item-unit"] == paper.unit
    assert options[str(chair.pk)]["data-item-unit"] == chair.unit
    assert str(inactive.pk) not in options and str(foreign.pk) not in options


@pytest.mark.django_db
@pytest.mark.parametrize("document_type", ["opening", "receipt", "issue", "transfer"])
def test_document_form_includes_preview_hooks_without_writing_stock(client, document_type):
    _, actor, *_ = supply_context()
    client.force_login(actor)
    response = client.get(reverse("supplies:document-create", args=[document_type]))
    assert response.status_code == 200
    html = response.content.decode()
    for marker in ("supply-line-preview", "supply-line-count", "supply-unit-totals", "data-line-unit", "data-line-deleted"):
        assert marker in html
    assert 'data-item-unit="把"' in html
    assert 'id="id_lines-__prefix__-DELETE"' in html
    assert SupplyDocument.objects.count() == SupplyStockBalance.objects.count() == SupplyStockLedger.objects.count() == 0


def test_preview_quantity_arithmetic_is_exact_and_keeps_units_separate():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for browser arithmetic verification")
    script = r"""
const assert = require('node:assert/strict');
const {parseQuantity, formatQuantity, summarizeLines} = require(process.argv[1]);
const valid = new Map([
  ['0.0001', 1n], ['.1', 1000n], ['0.2', 2000n], ['1e3', 10000000n],
  ['1e-4', 1n], ['+2.3400', 23400n], ['0002.5', 25000n],
  ['99999999999999.9999', 999999999999999999n],
]);
for (const [input, expected] of valid) assert.equal(parseQuantity(input), expected, input);
for (const input of ['', '0', '-1', 'NaN', 'Infinity', '1e99', '1e-9', '1.00001', '1.00000', '100000000000000', '1,200']) {
  assert.equal(parseQuantity(input), null, input);
}
assert.equal(formatQuantity(parseQuantity('0.1') + parseQuantity('0.2')), '0.3');
assert.equal(formatQuantity(parseQuantity('99999999999999.9999') + 1n), '100000000000000');
const summary = summarizeLines([
  {item: 'paper', unit: '盒', quantity: '0.1'},
  {item: 'paper', unit: '盒', quantity: '0.2'},
  {item: 'chair', unit: '把', quantity: '2'},
  {item: 'paper', unit: '盒', quantity: '100', deleted: true},
  {item: '', unit: '', quantity: ''},
  {item: '', unit: '', quantity: '', hasOtherInput: true},
  {item: 'paper', unit: '盒', quantity: '0'},
]);
assert.deepEqual(summary, {
  activeRows: 3, itemCount: 2, deletedRows: 1, blankRows: 1, incompleteRows: 2,
  totals: [{unit: '盒', quantity: '0.3'}, {unit: '把', quantity: '2'}],
});
// Undoing deletion uses the retained row input again; no mutation is required.
const line = {item: 'paper', unit: '盒', quantity: '7', deleted: true};
assert.deepEqual(summarizeLines([line]).totals, []);
line.deleted = false;
assert.deepEqual(summarizeLines([line]).totals, [{unit: '盒', quantity: '7'}]);
assert.equal(line.quantity, '7');
"""
    result = subprocess.run(
        [node, "-e", script, str(Path(settings.BASE_DIR) / "static/js/supply-document-form.js")],
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
