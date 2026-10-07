"""Invalid field reports stay actionable without losing the captured operation."""
from urllib.parse import urlencode

import pytest
from django.urls import reverse

from apps.maintenance.domain import business_date
from tests.test_sprint9_services import _complete
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db


def test_completion_error_summary_preserves_input_and_captured_instance(client):
    context = maintenance_context("ERRORCOMPLETION")
    client.force_login(context["responsible_user"])
    url = reverse("maintenance:plan-complete", args=[context["plan"].pk])
    opened = client.get(url)
    assert "data-maintenance-error-summary" not in opened.content.decode()
    payload = {
        "completion_instance": opened.context["form"]["completion_instance"].value(),
        "idempotency_key": opened.context["form"]["idempotency_key"].value(),
        "completed_date": business_date().isoformat(), "actual_content": "   ",
        "result": "problem_found", "problem_description": "   ", "remark": "备注保留 <现场>",
    }
    failed = client.post(url, payload)
    html = failed.content.decode()
    assert failed.status_code == 200
    assert html.index("data-maintenance-error-summary") < html.index("maintenance-standard-title")
    assert 'href="#id_actual_content"' in html
    assert 'href="#id_problem_description"' in html
    for field in ("completion_instance", "idempotency_key", "completed_date", "result", "remark"):
        assert failed.context["form"][field].value() == payload[field]
    assert "&lt;现场&gt;" in html
    assert context["plan"].records.count() == 0

    # Hidden instance validation also remains visible; do not re-sign failed POST input.
    payload.update(completion_instance="invalid-captured-instance", actual_content="完成紧固", problem_description="发现松动")
    stale = client.post(url, payload)
    assert stale.context["form"].non_field_errors()
    message = stale.context["form"].non_field_errors()[0]
    assert stale.content.decode().count(str(message)) == 1
    assert stale.context["form"]["completion_instance"].value() == payload["completion_instance"]
    assert context["plan"].records.count() == 0
    payload.pop("completion_instance")
    missing = client.post(url, payload)
    assert "completion_instance" in missing.context["form"].errors
    hidden_message = missing.context["form"].errors["completion_instance"][0]
    assert missing.content.decode().count(str(hidden_message)) == 1
    assert 'data-maintenance-error-field="id_completion_instance"' not in missing.content.decode()


def test_problem_close_error_keeps_confirmation_return_and_saves_only_after_correction(client):
    context = maintenance_context("ERRORCLOSE")
    record = _complete(context, "error-close-source", result="problem_found", problem_description="护罩松动，等待复查")
    problem = record.problem
    client.force_login(context["equipment"])
    url = reverse("maintenance:problem-close", args=[problem.pk])
    return_to = reverse("maintenance:problem-list") + "?" + urlencode({"q": "ERRORCLOSE", "status": "open"})
    opened = client.get(url, {"return_to": return_to})
    payload = {"idempotency_key": opened.context["form"]["idempotency_key"].value(),
               "closure_note": "   ", "confirm": "on", "return_to": return_to}
    failed = client.post(url, payload)
    html = failed.content.decode()
    assert failed.status_code == 200
    assert 'href="#id_closure_note"' in html
    assert "maintenance-error-navigation.js" in html
    assert failed.context["form"]["confirm"].value()
    assert failed.context["form"]["idempotency_key"].value() == payload["idempotency_key"]
    assert failed.context["cancel_url"] == return_to
    problem.refresh_from_db()
    assert problem.status == "open" and problem.closure_note == ""
    payload["closure_note"] = "已紧固并由设备管理员复查确认"
    saved = client.post(url, payload)
    assert saved.status_code == 302 and saved.url == return_to
    problem.refresh_from_db()
    record.refresh_from_db()
    assert problem.status == "closed" and problem.closure_note == payload["closure_note"]
    assert record.status == "confirmed"
