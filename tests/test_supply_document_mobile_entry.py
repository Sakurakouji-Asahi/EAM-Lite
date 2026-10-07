from decimal import Decimal

import pytest
from django.urls import reverse

from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from tests.test_sprint15_services import supply_context
from tests.test_supply_draft_edit_revision import payload


@pytest.mark.django_db
def test_line_error_navigation_retains_exact_input_and_deleted_error_does_not_block_draft(client):
    _, actor, _, _, warehouse, _, paper, chair = supply_context()
    client.force_login(actor)
    url = reverse("supplies:document-create", args=["receipt"])
    page = client.get(url)
    data = {**payload(page), "target_warehouse": str(warehouse.pk), "lines-TOTAL_FORMS": "2",
        "lines-0-item": str(paper.pk), "lines-0-quantity": "1.2345", "lines-0-entered_unit_cost": "0",
        "lines-1-item": str(chair.pk), "lines-1-quantity": "0.0002", "lines-1-entered_unit_cost": "3.000001",
        "lines-1-line_remark": "现场说明保留", "next_action": "detail"}
    rejected = client.post(url, data)
    assert rejected.status_code == 200
    assert rejected.context["formset"].forms[0].errors["line_remark"]
    assert rejected.context["formset"].forms[1]["quantity"].value() == "0.0002"
    assert rejected.context["formset"].forms[1]["entered_unit_cost"].value() == "3.000001"
    assert rejected.context["formset"].forms[1]["line_remark"].value() == "现场说明保留"
    html = rejected.content.decode()
    assert 'href="#supply-line-0"' in html and 'id="supply-line-0"' in html
    assert 'href="#supply-line-1"' not in html
    assert "单据明细需要修正，当前输入已保留" in html
    assert "0 成本时必填原因" in html
    for name in ("lines-0-item", "lines-0-quantity", "lines-1-entered_unit_cost", "idempotency_key"):
        assert html.count(f'name="{name}"') == 1
    assert not SupplyDocument.objects.exists()
    assert not SupplyStockBalance.objects.exists() and not SupplyStockLedger.objects.exists()

    saved = client.post(url, {**data, "lines-0-DELETE": "on"})
    assert saved.status_code == 302
    document = SupplyDocument.objects.get(idempotency_key=data["idempotency_key"])
    assert document.status == "draft"
    line = document.lines.get()
    assert line.item_id == chair.pk
    assert line.quantity == Decimal("0.0002") and line.entered_unit_cost == Decimal("3.000001")
    assert line.line_remark == "现场说明保留"
    assert not SupplyStockBalance.objects.exists() and not SupplyStockLedger.objects.exists()
