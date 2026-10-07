from datetime import date
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.masterdata.models import Company
from apps.supplies.draft_revision import supply_document_revision_snapshot
from apps.supplies.models import SupplyDocument, SupplyStockLedger
from apps.supplies.services import create_supply_document, post_supply_document
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_company, make_supply_category, make_supply_item, make_supply_warehouse, make_user
from tests.test_supply_draft_edit_revision import payload


pytestmark = pytest.mark.django_db


@pytest.fixture
def copy_context(client):
    company, actor, department, employee, source, target, paper, chair = supply_context()
    client.force_login(actor)
    return company, actor, department, employee, source, target, paper, chair


def reference(context, kind="receipt"):
    company, actor, department, employee, source, target, paper, chair = context
    data = {"business_date": date(2026, 8, 26), "idempotency_key": "document-copy-original-" + kind,
        "remark": "原单事件说明", "source_warehouse": source if kind != "receipt" else None,
        "target_warehouse": target if kind != "issue" else None,
        "department": department if kind == "issue" else None, "employee": employee if kind == "issue" else None}
    if kind == "receipt":
        data.update(external_reference="ORIGINAL-INVOICE", counterparty_name="原供应单位")
    return create_supply_document(actor=actor, company=company, document_type=kind, data=data,
        lines=[{"item": item, "quantity": Decimal(quantity),
            "entered_unit_cost": Decimal("10.123456") if kind == "receipt" else None,
            "line_remark": "原单每行说明"} for item, quantity in ((paper, "2.3456"), (chair, "0.0002"))])


@pytest.mark.parametrize("kind", ["receipt", "issue", "transfer"])
def test_reference_get_prefills_current_choices_exact_quantities_and_fresh_identity_without_writes(client, copy_context, kind):
    document = reference(copy_context, kind)
    if kind == "receipt":
        post_supply_document(actor=copy_context[1], document=document)
        document.refresh_from_db()
    before = supply_document_revision_snapshot(document)
    effects = (SupplyDocument.objects.count(), SupplyStockLedger.objects.count(), AuditLog.objects.count())
    back = reverse("supplies:document-list") + "?q=copy&status=posted&page=2"
    url = reverse("supplies:document-create", args=[kind])
    page = client.get(url, {"copy_from": str(document.pk), "return_to": back})
    assert page.status_code == 200
    form = page.context["form"]
    assert form["business_date"].value() == timezone.localdate()
    assert form["idempotency_key"].value() != document.idempotency_key
    assert "expected_revision" not in form.fields and not form["remark"].value()
    if kind == "receipt":
        assert form["counterparty_name"].value() == "原供应单位"
        assert not form["external_reference"].value()
    for name in ("source_warehouse", "target_warehouse", "department", "employee"):
        if name in form.fields:
            assert str(form[name].value()) == str(getattr(document, name + "_id"))
    rows = page.context["formset"].forms
    for row, original in zip(rows[:2], document.lines.all(), strict=True):
        assert str(row["item"].value()) == str(original.item_id)
        assert row["quantity"].value() == original.quantity
        assert not row["line_remark"].value()
        if kind == "receipt":
            assert row["entered_unit_cost"].value() is None
        else:
            assert "entered_unit_cost" not in row.fields
    assert rows[2]["quantity"].value() is None
    assert page.context["document_form_back_url"] == reverse("supplies:document-detail", args=[document.pk]) + "?" + urlencode({"return_to": back})
    assert supply_document_revision_snapshot(SupplyDocument.objects.get(pk=document.pk)) == before
    assert (SupplyDocument.objects.count(), SupplyStockLedger.objects.count(), AuditLog.objects.count()) == effects


def test_reference_unavailable_objects_are_left_blank_and_rows_are_not_silently_dropped(client, copy_context):
    document = reference(copy_context, "issue")
    _, _, department, employee, source, _, paper, _ = copy_context
    source.is_active = False
    source.save(update_fields=["is_active"])
    department.is_active = False
    department.save(update_fields=["is_active"])
    paper.is_active = False
    paper.save(update_fields=["is_active"])
    page = client.get(reverse("supplies:document-create", args=["issue"]), {"copy_from": str(document.pk)})
    assert page.status_code == 200
    form = page.context["form"]
    assert form["source_warehouse"].value() is None
    assert form["department"].value() is None and form["employee"].value() is None
    rows = page.context["formset"].forms
    assert rows[0]["item"].value() is None and rows[0]["quantity"].value() == Decimal("2.3456")
    assert len(rows) == 3 and len(page.context["copy_notices"]) == 4
    assert "原数量已保留" in page.content.decode()


def test_reference_post_keeps_user_changes_errors_and_saves_a_separate_neutral_draft(client, copy_context):
    document = reference(copy_context)
    before = supply_document_revision_snapshot(document)
    url = reverse("supplies:document-create", args=["receipt"]) + "?" + urlencode({"copy_from": str(document.pk)})
    page = client.get(url)
    data = {**payload(page), "copy_from": str(document.pk), "lines-0-item": str(copy_context[7].pk),
        "lines-0-quantity": "1.2345", "lines-0-line_remark": "本次明细", "lines-1-DELETE": "on",
        "external_reference": "NEW-INVOICE", "remark": "新单事件说明", "next_action": "detail"}
    rejected = client.post(url, data)
    assert rejected.status_code == 200 and rejected.context["formset"].forms[0].errors["entered_unit_cost"]
    assert rejected.context["formset"].forms[0]["item"].value() == data["lines-0-item"]
    assert rejected.context["formset"].forms[0]["quantity"].value() == "1.2345"
    assert rejected.context["form"]["idempotency_key"].value() == data["idempotency_key"]
    assert SupplyDocument.objects.count() == 1
    saved = client.post(url, {**data, "lines-0-entered_unit_cost": "3.000001"})
    assert saved.status_code == 302
    created = SupplyDocument.objects.get(idempotency_key=data["idempotency_key"])
    assert created.pk != document.pk and created.document_no != document.document_no and created.status == "draft"
    assert created.external_reference == "NEW-INVOICE" and created.remark == "新单事件说明"
    line = created.lines.get()
    assert line.item_id == copy_context[7].pk and line.quantity == Decimal("1.2345")
    assert line.entered_unit_cost == Decimal("3.000001")
    assert line.posted_unit_cost is None and line.source_issue_line_id is None and line.source_custody_id is None
    assert not SupplyStockLedger.objects.exists()
    assert supply_document_revision_snapshot(SupplyDocument.objects.get(pk=document.pk)) == before


def test_reference_access_rejects_foreign_malformed_type_mismatch_and_read_only_roles(client, copy_context):
    document = reference(copy_context)
    url = reverse("supplies:document-create", args=["receipt"])
    assert client.get(url, {"copy_from": "INVALID"}).status_code == 404
    assert client.get(reverse("supplies:document-create", args=["issue"]), {"copy_from": str(document.pk)}).status_code == 404
    assert client.get(url, {"copy_from": str(document.pk), "stock_item": str(copy_context[6].pk)}).status_code == 404
    other = make_company("COPY-OTHER", active=False)
    Company.objects.filter(pk=copy_context[0].pk).update(is_active=False)
    Company.objects.filter(pk=other.pk).update(is_active=True)
    try:
        item = make_supply_item(other, make_supply_category(other), "COPY-FOREIGN")
        foreign = create_supply_document(actor=copy_context[1], company=other, document_type="receipt",
            data={"business_date": date(2026, 8, 26), "target_warehouse": make_supply_warehouse(other, "COPY-OTHER-WH"),
                "idempotency_key": "copy-foreign"},
            lines=[{"item": item, "quantity": Decimal("1"), "entered_unit_cost": Decimal("2")}])
    finally:
        Company.objects.filter(pk=other.pk).update(is_active=False)
        Company.objects.filter(pk=copy_context[0].pk).update(is_active=True)
    assert client.get(url, {"copy_from": str(foreign.pk)}).status_code == 404
    client.force_login(make_user("copy-management", "management"))
    detail = client.get(reverse("supplies:document-detail", args=[document.pk]))
    assert detail.status_code == 200 and not detail.context["document_copy_url"]
    assert client.get(url, {"copy_from": str(document.pk)}).status_code == 403
    assert client.post(url, {"copy_from": str(document.pk)}).status_code == 403
