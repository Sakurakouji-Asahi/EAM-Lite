from decimal import Decimal
from pathlib import Path
import shutil
import subprocess

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from apps.supplies.services import post_supply_document
from tests.test_sprint15_services import make_return, supply_context
from tests.test_sprint15_support import make_issue_document, seed_supply_stock
from tests.test_sprint16_services import issued_custody


pytestmark = pytest.mark.django_db


def issued_consumable(client):
    company, actor, department, _, source, target, item, _ = supply_context()
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=item,
                      quantity="5.1234", unit_cost="10", key="assistant-opening")
    issue = make_issue_document(actor=actor, company=company, warehouse=source, item=item,
                                department=department, quantity="5.1234", key="assistant-issue")
    post_supply_document(actor=actor, document=issue)
    client.force_login(actor)
    return company, actor, target, issue.lines.get()


def test_consumable_return_limit_and_units_follow_only_effectively_posted_returns(client):
    company, actor, target, source_line = issued_consumable(client)
    returned = make_return(actor=actor, company=company, target=target, source_line=source_line,
                           quantity="2.0123", key="assistant-effective-return")
    post_supply_document(actor=actor, document=returned)
    make_return(actor=actor, company=company, target=target, source_line=source_line,
                quantity="1", key="assistant-pending-return")
    before = SupplyStockLedger.objects.count()
    response = client.get(reverse("supplies:consumable-return-create", args=[source_line.pk]))
    assert response.status_code == 200
    assert response.context["returned_quantity"] == Decimal("2.0123")
    assert response.context["returnable_quantity"] == Decimal("3.1111")
    attrs = response.context["form"].fields["quantity"].widget.attrs
    assert attrs["max"] == attrs["data-quantity-limit"] == "3.1111"
    assert attrs["data-quantity-unit"] == source_line.item.unit
    assert f"3.1111 {source_line.item.unit}" in response.content.decode()
    assert response.context["quantity_remaining_label"] == "过账后预计可退"
    assert SupplyStockLedger.objects.count() == before


@pytest.mark.parametrize("action", ["return", "transfer", "loss", "scrap"])
def test_custody_action_assistant_matches_phase_and_keeps_writeoff_blank(client, action):
    values = issued_custody(quantity="2.3456")
    actor, custody = values[1], values[-1]
    client.force_login(actor)
    if action == "return":
        url = reverse("supplies:durable-return-create", args=[custody.pk])
    elif action == "transfer":
        url = reverse("supplies:custody-transfer", args=[custody.pk])
    else:
        url = reverse("supplies:custody-write-off", args=[custody.pk, action])
    response = client.get(url)
    assert response.status_code == 200
    attrs = response.context["form"].fields["quantity"].widget.attrs
    assert attrs["max"] == attrs["data-quantity-limit"] == "2.3456"
    assert attrs["data-quantity-unit"] == "把"
    assert "supply-quantity-assistant.js" in response.content.decode()
    if action in {"loss", "scrap"}:
        assert response.context["form"]["quantity"].value() is None
        assert not response.context["allow_fill_full_quantity"]
        assert "data-quantity-fill-all" not in response.content.decode()
    else:
        assert response.context["allow_fill_full_quantity"]
    assert response.context["quantity_remaining_label"] == (
        "过账后预计仍在管" if action == "return" else "完成后预计仍在管"
    )


def test_partial_durable_return_still_uses_exact_quantity_and_existing_draft_replay(client):
    values = issued_custody(quantity="2.3456")
    actor, target, item, custody = values[1], values[5], values[6], values[-1]
    client.force_login(actor)
    url = reverse("supplies:durable-return-create", args=[custody.pk])
    payload = {"target_warehouse": str(target.pk), "quantity": "1.2345", "business_date": "2026-08-26",
               "reason": "只归还部分数量", "idempotency_key": "assistant-partial-durable"}
    assert client.post(url, payload).status_code == 302
    assert client.post(url, payload).status_code == 302
    assert SupplyDocument.objects.filter(idempotency_key=payload["idempotency_key"]).count() == 1
    document = SupplyDocument.objects.get(idempotency_key=payload["idempotency_key"])
    assert document.lines.get().quantity == Decimal("1.2345")
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("2.3456")  # draft does not change custody
    post_supply_document(actor=actor, document=document)
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal("1.1111")
    assert SupplyStockBalance.objects.get(warehouse=target, item=item).quantity_on_hand == Decimal("1.2345")


def test_return_snapshot_is_a_hint_and_existing_posting_rechecks_changed_availability(client):
    company, actor, target, source_line = issued_consumable(client)
    url = reverse("supplies:consumable-return-create", args=[source_line.pk])
    assert client.get(url).context["form"].fields["quantity"].widget.attrs["max"] == "5.1234"
    first = make_return(actor=actor, company=company, target=target, source_line=source_line,
                        quantity="4", key="assistant-concurrent-first")
    post_supply_document(actor=actor, document=first)
    # Another draft may have been prepared from an earlier view. The UI adds
    # no reservations or new posting rules; the existing service remains final.
    stale = make_return(actor=actor, company=company, target=target, source_line=source_line,
                        quantity="2", key="assistant-stale-draft")
    before = list(SupplyStockBalance.objects.order_by("pk").values())
    ledgers = SupplyStockLedger.objects.count()
    with pytest.raises(ValidationError):
        post_supply_document(actor=actor, document=stale)
    assert list(SupplyStockBalance.objects.order_by("pk").values()) == before
    assert SupplyStockLedger.objects.count() == ledgers
    stale.refresh_from_db()
    assert stale.status == "draft"
    assert client.get(url).context["form"].fields["quantity"].widget.attrs["max"] == "1.1234"


def test_exhausted_return_has_zero_limit_and_keeps_existing_server_rejection(client):
    company, actor, target, source_line = issued_consumable(client)
    complete = make_return(actor=actor, company=company, target=target, source_line=source_line,
                           quantity="5.1234", key="assistant-return-complete")
    post_supply_document(actor=actor, document=complete)
    url = reverse("supplies:consumable-return-create", args=[source_line.pk])
    response = client.get(url)
    assert response.context["form"].fields["quantity"].widget.attrs["max"] == "0.0000"
    assert 'type="submit" disabled' in response.content.decode()
    before = SupplyDocument.objects.count()
    rejected = client.post(url, {"target_warehouse": str(target.pk), "quantity": "1", "reason": "已全部退回",
                                "business_date": "2026-08-26", "idempotency_key": "assistant-over-complete"})
    assert rejected.status_code == 200
    assert rejected.context["form"].errors["quantity"]
    assert SupplyDocument.objects.count() == before


def test_quantity_assistant_preserves_large_number_and_partial_return_precision():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for quantity feedback verification")
    script = r"""
const assert = require('node:assert/strict');
const {quantityFeedback} = require(process.argv[1]);
assert.deepEqual(quantityFeedback('2.3456', '1.2345'), {state: 'partial', remaining: '1.1111'});
assert.deepEqual(quantityFeedback('0.3', '0.1'), {state: 'partial', remaining: '0.2'});
assert.deepEqual(quantityFeedback('99999999999999.9999', '0.0001'), {state: 'partial', remaining: '99999999999999.9998'});
assert.deepEqual(quantityFeedback('3.1111', '3.1112'), {state: 'over', excess: '0.0001'});
assert.deepEqual(quantityFeedback('2', '2'), {state: 'full', remaining: '0'});
assert.deepEqual(quantityFeedback('0', '1'), {state: 'none'});
assert.deepEqual(quantityFeedback('2', ''), {state: 'empty'});
for (const value of ['0', '-1', '1.00000', '1e-5', '100000000000000', 'invalid']) {
  assert.deepEqual(quantityFeedback('2', value), {state: 'invalid'}, value);
}
assert.deepEqual(quantityFeedback('2', '1e-4'), {state: 'partial', remaining: '1.9999'});
"""
    result = subprocess.run(
        [node, "-e", script, str(Path(settings.BASE_DIR) / "static/js/supply-quantity-assistant.js")],
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
