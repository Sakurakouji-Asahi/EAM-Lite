"""Real HTTP confirmation must bind the exact draft lines displayed to its user."""
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
import json
import threading

import pytest
from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.supplies import views
from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from apps.supplies.posting_preview import document_contents
from apps.supplies.services import create_supply_document
from tests.test_sprint15_services import supply_context
from tests.test_sprint14_support import make_user

pytestmark = pytest.mark.django_db(transaction=True)


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def edit_payload(page):
    result = {}
    for form in [page.context["form"], page.context["formset"].management_form,
                 *page.context["formset"].forms]:
        for field in form:
            value = field.value()
            if isinstance(field.field.widget, forms.CheckboxInput):
                if value:
                    result[field.html_name] = "on"
            else:
                result[field.html_name] = "" if value is None else str(value)
    return result


def stock_effects(document):
    return {
        "status": SupplyDocument.objects.get(pk=document.pk).status,
        "ledgers": list(SupplyStockLedger.objects.filter(document=document).order_by("pk")
                        .values("item_id", "quantity_delta", "amount_delta")),
        "balances": list(SupplyStockBalance.objects.filter(company_id=document.company_id)
                         .order_by("warehouse_id", "item_id")
                         .values("warehouse_id", "item_id", "quantity_on_hand", "amount_on_hand")),
        "posting_audits": AuditLog.objects.filter(action="supply_document_post",
                                                   object_id=str(document.pk)).count(),
        "comparison_audits": AuditLog.objects.filter(action="supply_posting_comparison",
                                                      object_id=str(document.pk)).count(),
    }


@pytest.mark.parametrize("change", ["quantity_and_cost", "new_line"])
def test_http_confirmation_rejects_edit_committed_between_preview_and_token(monkeypatch, change):
    assert connection.vendor == "postgresql"
    company, actor, _, _, _, warehouse, item, extra_item = supply_context()
    writer = make_user("posting-coherence-writer", "warehouse")
    document = create_supply_document(
        actor=actor, company=company, document_type="receipt",
        data={"business_date": date(2026, 10, 3), "target_warehouse": warehouse,
              "idempotency_key": "posting-coherence-receipt", "remark": "保持原单头"},
        lines=[{"item": item, "quantity": Decimal("2"),
                "entered_unit_cost": Decimal("10"), "line_remark": "用户正在核对的行"}],
    )
    reader, writer_page_client = Client(), Client()
    reader.force_login(actor)
    writer_page_client.force_login(writer)
    edit_url = reverse("supplies:document-edit", args=[document.pk])
    post_url = reverse("supplies:document-post", args=[document.pk])
    edit_page = writer_page_client.get(edit_url)
    assert edit_page.status_code == 200
    data = edit_payload(edit_page)
    assert data["expected_revision"]
    if change == "quantity_and_cost":
        data["lines-0-quantity"] = "3.0000"
        data["lines-0-entered_unit_cost"] = "12.000000"
    else:
        data.update({"lines-1-item": str(extra_item.pk), "lines-1-quantity": "7.0000",
                     "lines-1-entered_unit_cost": "2.500000", "lines-1-line_remark": "另一人新增行"})
    original_header = {key: getattr(document, key) for key in
                       ("document_type", "business_date", "source_warehouse_id",
                        "target_warehouse_id", "department_id", "employee_id", "remark")}
    original_line_id = document.lines.get().pk
    actual_token = views.preview_token
    writer_result, writer_errors = {}, []
    calls = 0

    def independent_writer():
        try:
            client = Client()
            client.force_login(get_user_model().objects.get(pk=writer.pk))
            response = client.post(edit_url, data)
            writer_result["status"] = response.status_code
            writer_result["errors"] = str(response.context["form"].errors) if response.context else ""
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            connections.close_all()

    def between_display_snapshot_and_signature(token_actor, token_document, preview):
        nonlocal calls
        calls += 1
        if calls == 1:
            # The actual GET has already calculated the rows that its template
            # will display. A separate PG connection completes a legal HTTP edit
            # before the real signer runs; no business result is mocked.
            assert preview["rows"][0]["line"].quantity == Decimal("2.0000")
            assert preview["rows"][0]["unit_cost"] == Decimal("10.000000")
            assert preview["total_amount"] == Decimal("20.00")
            thread = threading.Thread(target=independent_writer, daemon=True)
            thread.start()
            thread.join(timeout=60)
            assert not thread.is_alive(), "Independent legal draft edit did not commit"
            assert not writer_errors, repr(writer_errors)
            assert writer_result == {"status": 302, "errors": ""}
        return actual_token(token_actor, token_document, preview)

    monkeypatch.setattr(views, "preview_token", between_display_snapshot_and_signature)
    confirmation = reader.get(post_url)
    assert confirmation.status_code == 200 and calls == 1
    preview = confirmation.context["posting_preview"]
    assert len(preview["rows"]) == 1
    assert preview["rows"][0]["line"].pk == original_line_id
    assert preview["rows"][0]["line"].quantity == Decimal("2.0000")
    assert preview["total_amount"] == Decimal("20.00")
    table = TableRows()
    table.feed(confirmation.content.decode())
    displayed = [row for row in table.rows if row and item.item_code in row[0]]
    assert len(displayed) == 1 and displayed[0][1].startswith("2.0000 ")
    assert displayed[0][3] == "20.00"
    token = confirmation.context["form"]["preview_token"].value()
    signed = signing.loads(token, salt="supply-posting-preview", max_age=28800)
    assert signed["actor"] == actor.pk and signed["document"] == str(document.pk)
    assert signed["total"] == "20.00"
    document.refresh_from_db()
    assert original_header == {key: getattr(document, key) for key in original_header}
    assert document.status == "draft"
    assert not document.lines.filter(pk=original_line_id).exists()
    assert stock_effects(document) == {"status": "draft", "ledgers": [], "balances": [],
                                       "posting_audits": 0, "comparison_audits": 0}
    live_contents = document_contents(document)
    live_lines = list(document.lines.order_by("line_no").values(
        "id", "item_id", "quantity", "entered_unit_cost", "line_remark"))
    post_data = {"confirm": "on", "idempotency_key": document.idempotency_key,
                 "preview_token": token}
    submitted = reader.post(post_url, post_data)
    observed = stock_effects(document)
    print("COHERENCE_OBSERVATION=" + json.dumps({
        "change": change, "writer": writer_result, "displayed_html_rows": displayed,
        "displayed_line_id": str(original_line_id), "displayed_total": "20.00",
        "signed_rows": signed["rows"], "signed_contents": signed["contents"],
        "latest_contents_before_submit": live_contents,
        "signed_bound_new_contents": signed["contents"] == live_contents,
        "latest_lines_before_submit": live_lines, "actual_post_response": submitted.status_code,
        "actual_post_effects": observed,
    }, default=str, sort_keys=True), flush=True)
    # The old confirmation must not post quantities its user never saw.
    assert submitted.status_code == 200
    assert "草稿内容已被修改" in submitted.content.decode()
    assert observed == {"status": "draft", "ledgers": [], "balances": [],
                        "posting_audits": 0, "comparison_audits": 0}
    assert list(document.lines.order_by("line_no").values(
        "id", "item_id", "quantity", "entered_unit_cost", "line_remark")) == live_lines

    fresh = reader.get(post_url)
    expected_total = Decimal("36.00") if change == "quantity_and_cost" else Decimal("37.50")
    assert fresh.context["posting_preview"]["total_amount"] == expected_total
    fresh_data = {"confirm": "on", "idempotency_key": document.idempotency_key,
                  "preview_token": fresh.context["form"]["preview_token"].value()}
    assert reader.post(post_url, fresh_data).status_code == 302
    completed = stock_effects(document)
    assert completed["status"] == "posted"
    assert len(completed["ledgers"]) == (1 if change == "quantity_and_cost" else 2)
    assert sum(row["amount_delta"] for row in completed["ledgers"]) == expected_total
    quantities = {str(row["item_id"]): row["quantity_delta"] for row in completed["ledgers"]}
    assert quantities[str(item.pk)] == (Decimal("3.0000") if change == "quantity_and_cost" else Decimal("2.0000"))
    if change == "new_line":
        assert quantities[str(extra_item.pk)] == Decimal("7.0000")
    assert completed["posting_audits"] == completed["comparison_audits"] == 1
    assert reader.post(post_url, fresh_data).status_code == 302
    assert stock_effects(document) == completed
