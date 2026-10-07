"""Audit reading aids preserve scope, redaction, and exact query meaning."""
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.forms import AuditLogFilterForm
from apps.audit.models import AuditLog
from apps.audit.workspace import history_workspace
from tests.test_sprint3_support import make_company, make_user


pytestmark = pytest.mark.django_db


@pytest.fixture
def audit_context():
    company = make_company("AUDITWORKSPACE")
    hr = make_user("audit-workspace-hr", "hr")
    admin = make_user("audit-workspace-admin", "system_admin")
    employee = make_user("audit-workspace-employee", "employee")
    correlation = uuid.uuid4()
    target = str(uuid.uuid4())
    log = AuditLog.objects.create(company=company, user=hr, action="asset_draft_update",
        object_type="Asset", object_id=target, correlation_id=correlation,
        old_data_json={"asset_name": "原设备", "original_cost": "123456.78", "password": "hidden-before"},
        new_data_json={"asset_name": "<script>new-device</script>", "original_cost": "987654.32", "password": "hidden-after", "notes": "现场备注"})
    sibling = AuditLog.objects.create(company=company, user=hr, action="asset_draft_create",
        object_type="Asset", object_id=target, correlation_id=correlation,
        new_data_json={"asset_name": "同对象设备"})
    AuditLog.objects.create(company=company, user=admin, action="asset_draft_update",
        object_type="Asset", object_id=target, correlation_id=correlation,
        new_data_json={"asset_name": "HR无权查看的记录"})
    return company, hr, admin, employee, log, sibling


def test_summary_and_related_navigation_use_redacted_scoped_records_without_writes(client, audit_context):
    _company, hr, _admin, _employee, log, sibling = audit_context
    client.force_login(hr)
    original = list(AuditLog.objects.order_by("pk").values())
    page = client.get(reverse("audit:log-list"), {"action": "asset_draft_update", "object_id": log.object_id})
    assert page.status_code == 200 and "no-store" in page["Cache-Control"]
    projected = page.context["page_obj"].object_list
    assert [item["id"] for item in projected] == [log.pk]
    changes = projected[0]["changes"]
    assert any(item["label"] == "资产名称" and item["before"] == "原设备" for item in changes)
    html = page.content.decode()
    assert "&lt;script&gt;new-device&lt;/script&gt;" in html
    for private in ("123456.78", "987654.32", "hidden-before", "hidden-after", "HR无权查看的记录"):
        assert private not in html
    assert "原值" not in {item["label"] for item in changes}
    same_object = client.get(projected[0]["object_history_url"])
    assert {item["id"] for item in same_object.context["page_obj"].object_list} == {log.pk, sibling.pk}
    same_request = client.get(projected[0]["correlation_history_url"])
    assert {item["id"] for item in same_request.context["page_obj"].object_list} == {log.pk, sibling.pk}
    assert "action" not in parse_qs(urlsplit(projected[0]["object_history_url"]).query)
    assert list(AuditLog.objects.order_by("pk").values()) == original


def test_time_shortcuts_and_single_filter_removal_keep_applied_conditions_and_reset_page(audit_context):
    company, hr, _admin, _employee, log, _sibling = audit_context
    end = timezone.make_aware(datetime(2026, 10, 4, 10, 35, 23))
    params = {"q": "设备", "actor": hr.pk, "action": "asset_draft_update", "object_type": "Asset",
        "object_id": log.object_id, "start_at": (end - timedelta(days=2)).isoformat(),
        "end_at": end.isoformat(), "page_size": "25", "page": "3"}
    form = AuditLogFilterForm(params, company=company)
    assert form.is_valid(), form.errors
    workspace = history_workspace(form, now=end)
    for shortcut in workspace["audit_time_shortcuts"]:
        query = {key: values[0] for key, values in parse_qs(urlsplit(shortcut["url"]).query).items()}
        assert "page" not in query
        for key in ("q", "actor", "action", "object_type", "object_id", "page_size"):
            assert query[key] == str(params[key])
        candidate = AuditLogFilterForm(query, company=company)
        assert candidate.is_valid(), candidate.errors
        assert candidate.cleaned_data["end_at"] == end
        start = candidate.cleaned_data["start_at"]
        assert start == ({"今日": end.replace(hour=0, minute=0, second=0, microsecond=0),
            "最近 7 天": end - timedelta(days=7),
            "本月": end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)}[shortcut["label"]])
    chip = next(item for item in workspace["audit_filter_chips"] if item["label"] == "关键词")
    query = parse_qs(urlsplit(chip["url"]).query)
    assert "q" not in query and "page" not in query
    assert query["object_id"] == [log.object_id]
    assert query["start_at"] == [params["start_at"]]


def test_empty_results_and_invalid_exact_filters_remain_visible_and_protected(client, audit_context):
    _company, hr, _admin, employee, _log, _sibling = audit_context
    client.force_login(hr)
    empty = client.get(reverse("audit:log-list"), {"action": "no.such.action"})
    assert empty.status_code == 200 and empty.context["page_obj"].paginator.count == 0
    assert "当前条件下没有操作日志" in empty.content.decode()
    assert "操作日志时间范围" in empty.content.decode()
    invalid = client.get(reverse("audit:log-list"), {"correlation_id": "bad-id"})
    assert invalid.status_code == 400 and invalid.context["form"].errors["correlation_id"]
    assert 'class="audit-advanced" open' in invalid.content.decode()
    invalid_date = client.get(reverse("audit:log-list"), {"start_at": "2026-99-99T01:00"})
    assert invalid_date.status_code == 400 and invalid_date.context["form"].errors["start_at"]
    assert client.get(reverse("audit:log-list"), {"unsupported": "value"}).status_code == 400
    client.force_login(employee)
    assert client.get(reverse("audit:log-list")).status_code == 403


def test_datetime_inputs_show_valid_local_values_for_defaults_and_offset_links(audit_context):
    company, _hr, _admin, _employee, _log, _sibling = audit_context
    form = AuditLogFilterForm({}, company=company)
    assert form.is_valid(), form.errors
    for name in ("start_at", "end_at"):
        value = form.fields[name].widget.format_value(form[name].value())
        assert "+" not in value and len(value.split(".")[1]) == 3
        assert datetime.fromisoformat(value).tzinfo is None
    form = AuditLogFilterForm({"start_at": "2026-10-04T08:35:23.123+08:00",
        "end_at": "2026-10-04T09:35:23.456+08:00"}, company=company)
    assert form.is_valid(), form.errors
    assert form.fields["start_at"].widget.format_value(form["start_at"].value()) == "2026-10-04T08:35:23.123"
    assert form.fields["end_at"].widget.format_value(form["end_at"].value()) == "2026-10-04T09:35:23.456"
