"""Loan retries validate notes without changing legacy request hashes."""
from datetime import date, timedelta
from html.parser import HTMLParser

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import loan_asset, OPERATION_AUDIT_PREFIX
from apps.assets.models import Asset, AssetLoan, AssetMovement
from apps.audit.models import AuditLog
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db
MISMATCH = "相同幂等键已用于不同请求参数。"


class _HiddenForms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.current = [], None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self.current = {}
        elif tag == "input" and self.current is not None and values.get("type") == "hidden":
            self.current[values["name"]] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def _state(company, asset, key):
    loan_ids = [str(pk) for pk in AssetLoan.objects.filter(asset=asset).order_by("pk").values_list("pk", flat=True)]
    return {
        "loan_ids": loan_ids,
        "loan_movement_ids": [str(pk) for pk in AssetMovement.objects.filter(asset=asset, movement_type="loan").order_by("pk").values_list("pk", flat=True)],
        "loan_movement_remarks": list(AssetMovement.objects.filter(asset=asset, movement_type="loan").order_by("pk").values_list("remark", flat=True)),
        "operation_markers": AuditLog.objects.filter(company=company, action=OPERATION_AUDIT_PREFIX + ".loan", new_data_json__idempotency_key=key).count(),
        "loaned_audits": AuditLog.objects.filter(company=company, action="asset_lifecycle.loaned", object_type="AssetLoan", object_id__in=loan_ids).count(),
        "audit_total": AuditLog.objects.filter(company=company).count(),
        "asset_status": Asset.objects.get(pk=asset.pk).asset_status,
    }


def _saved_loan(prefix, *, remark="备注 A"):
    context, asset, _qr = active_asset_context(prefix)
    client = Client(enforce_csrf_checks=True)
    client.force_login(context["equipment"])
    url = reverse("assets:lifecycle-loan", args=[asset.pk])
    page = client.get(url)
    assert page.status_code == 200
    parser = _HiddenForms()
    parser.feed(page.content.decode("utf-8"))
    forms = [row for row in parser.forms if {"idempotency_key", "expected_status"} <= set(row)]
    assert len(forms) == 1
    payload = dict(forms[0])
    assert payload["csrfmiddlewaretoken"] and payload["idempotency_key"]
    assert payload["expected_status"] == asset.asset_status
    today = timezone.localdate()
    payload.update(borrower_type="external", borrower_employee="", borrower_name="借用人 A",
        borrower_organization="借用单位 A", loan_date=today.isoformat(),
        expected_return_date=(today + timedelta(days=7)).isoformat(), reason="现场借用", remark=remark)
    first = client.post(url, payload)
    assert first.status_code == 302
    assert first.url == reverse("assets:asset-detail", args=[asset.pk])
    loan = AssetLoan.objects.get(asset=asset, loan_idempotency_key=payload["idempotency_key"])
    assert loan.loan_movement.remark == remark
    state = _state(context["company"], asset, payload["idempotency_key"])
    assert len(state["loan_ids"]) == len(state["loan_movement_ids"]) == state["operation_markers"] == state["loaned_audits"] == 1
    return context, asset, client, url, payload, first, loan, state


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_same_get_key_changed_loan_remark_is_rejected_and_redisplayed():
    context, asset, client, url, payload, _first, _loan, initial = _saved_loan("LOANREMARKCHANGE")
    changed = dict(payload, remark="备注 B：旧页面补充说明")
    response = client.post(url, changed)
    after = _state(context["company"], asset, payload["idempotency_key"])
    assert response.status_code == 200
    assert response.context["form"].is_bound
    assert MISMATCH in response.context["form"].non_field_errors()
    assert response.context["form"]["remark"].value() == changed["remark"]
    assert response.context["form"]["idempotency_key"].value() == payload["idempotency_key"]
    assert after == initial


@override_settings(ALLOWED_HOSTS=["testserver"])
@pytest.mark.parametrize("remark", ["备注 A", ""])
def test_exact_original_loan_replay_returns_same_record_and_preserves_legacy_marker(remark):
    context, asset, client, url, payload, first, loan, initial = _saved_loan("LOANREMARKEXACT", remark=remark)
    marker = AuditLog.objects.get(company=context["company"], action=OPERATION_AUDIT_PREFIX + ".loan", new_data_json__idempotency_key=payload["idempotency_key"])
    assert "remark" not in marker.new_data_json["payload"]
    response = client.post(url, payload)
    assert response.status_code == 302 and response.url == first.url
    repeated = loan_asset(actor=context["equipment"], asset=asset, borrower_type="external",
        borrower_employee=None, borrower_name=payload["borrower_name"], borrower_organization=payload["borrower_organization"],
        loan_date=date.fromisoformat(payload["loan_date"]), expected_return_date=date.fromisoformat(payload["expected_return_date"]),
        handled_by=context["equipment"], reason=payload["reason"], idempotency_key=payload["idempotency_key"],
        expected_status=payload["expected_status"], remark=f"\n {remark} \n")
    assert repeated.pk == loan.pk and repeated.loan_movement_id == loan.loan_movement_id
    assert _state(context["company"], asset, payload["idempotency_key"]) == initial
    assert AuditLog.objects.get(pk=marker.pk).new_data_json == marker.new_data_json


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_original_borrower_payload_mismatch_still_rejects_without_writes():
    context, asset, client, url, payload, _first, _loan, initial = _saved_loan("LOANREMARKBORROWER")
    changed = dict(payload, borrower_name="借用人 B")
    response = client.post(url, changed)
    assert response.status_code == 200
    assert MISMATCH in response.context["form"].non_field_errors()
    assert response.context["form"]["borrower_name"].value() == changed["borrower_name"]
    assert response.context["form"]["remark"].value() == payload["remark"]
    assert _state(context["company"], asset, payload["idempotency_key"]) == initial
