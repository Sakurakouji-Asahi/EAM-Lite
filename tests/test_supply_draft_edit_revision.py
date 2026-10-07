from datetime import date
from decimal import Decimal
import threading
import time
from unittest.mock import patch

import pytest
from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.supplies import views
from apps.supplies.draft_revision import SUPPLY_EDIT_REVISION_SALT, supply_document_revision_snapshot
from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from apps.supplies.services import create_supply_document, update_draft_document
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_user

pytestmark = pytest.mark.django_db(transaction=True)


def payload(page):
    result = {}
    for form in [page.context["form"], page.context["formset"].management_form, *page.context["formset"].forms]:
        for field in form:
            value = field.value()
            if isinstance(field.field.widget, forms.CheckboxInput):
                if value:
                    result[field.html_name] = "on"
            else:
                result[field.html_name] = "" if value is None else str(value)
    return result


def rows(document):
    return [{"item": line.item, "quantity": line.quantity, "entered_unit_cost": line.entered_unit_cost,
             "line_remark": line.line_remark} for line in document.lines.order_by("line_no")]


def fingerprint(document):
    return supply_document_revision_snapshot(SupplyDocument.objects.get(pk=document.pk))


def effects():
    return AuditLog.objects.count(), SupplyStockBalance.objects.count(), SupplyStockLedger.objects.count()


def unchanged(document, before, counters):
    assert fingerprint(document) == before
    assert effects() == counters


@pytest.fixture
def ctx():
    assert connection.vendor == "postgresql"
    company, a, department, _, source, warehouse, item, extra_item = supply_context()
    b = make_user("supply-revision-b", "warehouse")
    document = create_supply_document(
        actor=a, company=company, document_type="receipt",
        data={"business_date": date(2026, 10, 3), "target_warehouse": warehouse,
              "idempotency_key": "supply-revision-one", "remark": "原单据备注"},
        lines=[{"item": item, "quantity": Decimal("5"), "entered_unit_cost": Decimal("3.5"),
                "line_remark": "原明细备注"}],
    )
    ca, cb = Client(), Client()
    ca.force_login(a)
    cb.force_login(b)
    return {"company": company, "a": a, "b": b, "ca": ca, "cb": cb,
            "document": document, "item": item, "extra_item": extra_item, "warehouse": warehouse,
            "department": department, "source": source,
            "url": reverse("supplies:document-edit", args=[document.pk])}


@pytest.mark.parametrize("change", ["quantity_and_remark", "new_line"])
def test_two_old_pages_reject_lost_update_and_fresh_reopen_saves(ctx, change):
    ca, cb, doc, url = ctx["ca"], ctx["cb"], ctx["document"], ctx["url"]
    pa, pb = payload(ca.get(url)), payload(cb.get(url))
    assert pa["expected_revision"] != pb["expected_revision"]  # actor binding
    old_b_token = pb["expected_revision"]
    pa["remark"] = "A 已保存备注"
    if change == "quantity_and_remark":
        pa["lines-0-quantity"] = "14.0000"
        pa["lines-0-line_remark"] = "A 已核对明细"
    else:
        pa.update({"lines-1-item": str(ctx["extra_item"].pk), "lines-1-quantity": "9.0000",
                   "lines-1-entered_unit_cost": "2.750000", "lines-1-line_remark": "A 新增第二行"})
    assert ca.post(url, pa).status_code == 302
    saved, counters = fingerprint(doc), effects()
    pb["remark"] = "B 仅改备注"
    pb["next_action"] = "post"
    rejected = cb.post(url, pb)
    assert rejected.status_code == 200
    assert rejected.context["form"].errors["expected_revision"]
    assert rejected.context["form"]["expected_revision"].value() == old_b_token
    assert rejected.context["form"]["remark"].value() == "B 仅改备注"
    assert rejected.context["formset"].forms[0]["quantity"].value() == "5.0000"
    assert 'data-unsaved-guard="true"' in rejected.content.decode()
    assert "重新打开最新草稿" in rejected.content.decode()
    unchanged(doc, saved, counters)
    doc.refresh_from_db()
    assert doc.status == "draft" and doc.remark == "A 已保存备注"
    saved_rows = rows(doc)
    if change == "quantity_and_remark":
        assert len(saved_rows) == 1 and saved_rows[0]["quantity"] == Decimal("14.0000")
        assert saved_rows[0]["line_remark"] == "A 已核对明细"
    else:
        assert len(saved_rows) == 2 and saved_rows[1]["item"].pk == ctx["extra_item"].pk
        assert saved_rows[1]["quantity"] == Decimal("9.0000")
    fresh = payload(cb.get(url))
    assert fresh["expected_revision"] != old_b_token
    fresh["remark"] = "B 已核对最新版本"
    assert cb.post(url, fresh).status_code == 302
    doc.refresh_from_db()
    assert doc.remark == "B 已核对最新版本" and doc.status == "draft"
    assert rows(doc) == saved_rows
    assert effects() == (counters[0] + 1, 0, 0)


@pytest.mark.parametrize("bad_token", ["missing", "tampered", "other_actor", "other_document", "other_company", "expired"])
def test_missing_or_invalid_bound_token_has_no_side_effect(ctx, bad_token):
    ca, doc, url = ctx["ca"], ctx["document"], ctx["url"]
    data = payload(ca.get(url))
    if bad_token == "missing":
        before, counters = fingerprint(doc), effects()
        empty = ca.post(url, {})
        assert empty.status_code == 200
        assert empty.context["form"].is_bound and empty.context["formset"].is_bound
        assert empty.context["form"].errors["expected_revision"]
        assert 'data-unsaved-guard="true"' in empty.content.decode()
        unchanged(doc, before, counters)
        data.pop("expected_revision")
    elif bad_token == "tampered":
        data["expected_revision"] += "tampered"
    elif bad_token == "other_actor":
        data["expected_revision"] = payload(ctx["cb"].get(url))["expected_revision"]
    else:
        signed = signing.loads(data["expected_revision"], salt=SUPPLY_EDIT_REVISION_SALT)
        if bad_token == "other_document":
            signed["document"] = "00000000-0000-0000-0000-000000000001"
        elif bad_token == "other_company":
            signed["company"] = "00000000-0000-0000-0000-000000000001"
        if bad_token == "expired":
            with patch("django.core.signing.time.time", return_value=time.time() - 25 * 60 * 60):
                data["expected_revision"] = signing.dumps(signed, salt=SUPPLY_EDIT_REVISION_SALT, compress=True)
        else:
            data["expected_revision"] = signing.dumps(signed, salt=SUPPLY_EDIT_REVISION_SALT, compress=True)
    data["remark"] = "不能保存的输入"
    data["next_action"] = "post"
    before, counters = fingerprint(doc), effects()
    response = ca.post(url, data)
    assert response.status_code == 200 and response.context["form"].errors["expected_revision"]
    assert response.context["form"]["remark"].value() == data["remark"]
    assert response.context["form"]["expected_revision"].value() == data.get("expected_revision")
    unchanged(doc, before, counters)


def test_invalid_form_keeps_old_token_then_rejects_after_other_save(ctx):
    ca, doc, url = ctx["ca"], ctx["document"], ctx["url"]
    data = payload(ca.get(url))
    old_token = data["expected_revision"]
    data.update({"remark": "尚未保存的备注", "lines-0-quantity": "invalid-number"})
    before, counters = fingerprint(doc), effects()
    invalid = ca.post(url, data)
    assert invalid.status_code == 200
    assert invalid.context["formset"].forms[0].errors["quantity"]
    assert invalid.context["form"]["expected_revision"].value() == old_token
    assert invalid.context["formset"].forms[0]["quantity"].value() == "invalid-number"
    unchanged(doc, before, counters)
    update_draft_document(actor=ctx["b"], document=doc, data={"remark": "另一人已保存"}, lines=rows(doc))
    saved, counters = fingerprint(doc), effects()
    data["lines-0-quantity"] = "5.0000"
    rejected = ca.post(url, data)
    assert rejected.status_code == 200 and rejected.context["form"].errors["expected_revision"]
    assert rejected.context["form"]["expected_revision"].value() == old_token
    assert rejected.context["form"]["remark"].value() == "尚未保存的备注"
    unchanged(doc, saved, counters)


@pytest.mark.parametrize("writer_change", ["header", "new_line", "revoke_permission"])
def test_independent_writer_after_form_validation_is_checked_under_fresh_lock(ctx, monkeypatch, writer_change):
    data = payload(ctx["ca"].get(ctx["url"]))
    data.update({"remark": "旧页面提交", "next_action": "post"})
    actual_update = views.update_draft_document
    committed = {}
    errors = []

    def writer():
        try:
            actor = get_user_model().objects.get(pk=ctx["b"].pk)
            doc = SupplyDocument.objects.get(pk=ctx["document"].pk)
            if writer_change == "revoke_permission":
                get_user_model().objects.get(pk=ctx["a"].pk).groups.clear()
            else:
                new_rows = rows(doc)
                if writer_change == "new_line":
                    new_rows.append({"item": ctx["extra_item"], "quantity": Decimal("9"),
                                     "entered_unit_cost": Decimal("2.75"), "line_remark": "独立writer新增行"})
                update_draft_document(actor=actor, document=doc, data={"remark": "独立writer提交"}, lines=new_rows)
            committed.update(fingerprint=fingerprint(doc), effects=effects())
        except BaseException as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    def between_valid_form_and_lock(**kwargs):
        # This hook runs only after both actual bound forms validated. The
        # writer uses another real PG connection and commits before row lock.
        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "Independent writer did not finish"
        assert not errors, repr(errors)
        return actual_update(**kwargs)

    monkeypatch.setattr(views, "update_draft_document", between_valid_form_and_lock)
    response = ctx["ca"].post(ctx["url"], data)
    assert committed
    assert response.status_code == (403 if writer_change == "revoke_permission" else 200)
    if writer_change != "revoke_permission":
        assert response.context["form"].errors["expected_revision"]
        assert response.context["form"]["expected_revision"].value() == data["expected_revision"]
    unchanged(ctx["document"], committed["fingerprint"], committed["effects"])
    assert SupplyDocument.objects.get(pk=ctx["document"].pk).status == "draft"


def test_trusted_service_call_without_browser_revision_remains_supported(ctx):
    doc = ctx["document"]
    before = effects()
    updated = update_draft_document(actor=ctx["a"], document=doc, data={"remark": "受信服务保存"}, lines=rows(doc))
    assert updated.remark == "受信服务保存" and updated.status == "draft"
    assert rows(updated)[0]["quantity"] == Decimal("5.0000")
    assert effects() == (before[0] + 1, 0, 0)


@pytest.mark.parametrize("document_type", ["opening", "receipt", "issue", "transfer"])
def test_fresh_signed_page_saves_each_manual_draft_type(ctx, document_type):
    data = {"business_date": date(2026, 10, 3), "idempotency_key": "revision-type-" + document_type}
    if document_type in {"opening", "receipt"}:
        data["target_warehouse"] = ctx["warehouse"]
    elif document_type == "issue":
        data.update(source_warehouse=ctx["source"], department=ctx["department"])
    else:
        data.update(source_warehouse=ctx["source"], target_warehouse=ctx["warehouse"])
    document = create_supply_document(
        actor=ctx["a"], company=ctx["company"], document_type=document_type, data=data,
        lines=[{"item": ctx["item"], "quantity": Decimal("5"),
                "entered_unit_cost": Decimal("3.5") if document_type in {"opening", "receipt"} else None}],
    )
    url = reverse("supplies:document-edit", args=[document.pk])
    page = ctx["ca"].get(url)
    assert page.status_code == 200
    data = payload(page)
    assert data["expected_revision"]
    data["remark"] = "已核对该类型草稿"
    before = effects()
    response = ctx["ca"].post(url, data)
    assert response.status_code == 302
    document.refresh_from_db()
    assert document.status == "draft" and document.remark == data["remark"]
    assert document.lines.get().quantity == Decimal("5.0000")
    assert effects() == (before[0] + 1, 0, 0)
