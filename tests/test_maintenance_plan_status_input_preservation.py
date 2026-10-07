"""Unexecuted HTTP draft: rejected status changes retain user input."""
from html import escape
import json
import re

import pytest
from django.core.serializers.json import DjangoJSONEncoder
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.maintenance.models import MaintenancePlan
from apps.maintenance.services import set_maintenance_plan_status
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def persisted_plan(plan):
    return MaintenancePlan._base_manager.filter(pk=plan.pk).values().get()


def plan_audits(plan):
    return list(AuditLog.objects.filter(
        object_type="MaintenancePlan", object_id=str(plan.pk),
    ).order_by("created_at", "pk").values(
        "id", "action", "old_data_json", "new_data_json",
    ))


def test_http_real_resume_rejection_preserves_target_reason_and_business_state(client):
    ctx = maintenance_context("STATUSINPUTREJECT")
    plan = set_maintenance_plan_status(
        actor=ctx["equipment"], plan=ctx["plan"], status="suspended",
        reason="等待核验保养条件",
    )
    # The existing factory/save path allows this editable asset flag. The real
    # status Service will reload it under its normal plan lock and reject resume.
    ctx["asset"].is_maintenance_required = False
    ctx["asset"].save(update_fields={"is_maintenance_required"})
    before, audits = persisted_plan(plan), plan_audits(plan)
    url = reverse("maintenance:plan-status", args=[plan.pk])
    client.force_login(ctx["equipment"])
    page = client.get(url)
    assert page.status_code == 200
    reason = "拟恢复：请核验 <保养条件> & 责任人"
    response = client.post(url, {"status": "active", "reason": reason})
    html = response.content.decode()
    after, after_audits = persisted_plan(plan), plan_audits(plan)
    print("MAINTENANCE_STATUS_INPUT_PRESERVATION " + json.dumps({
        "scenario": "real_resume_rejected_after_asset_became_ineligible",
        "first_get_http": page.status_code, "post_http": response.status_code,
        "actor_id": str(ctx["equipment"].pk), "company_id": str(ctx["company"].pk),
        "plan_id": str(plan.pk), "submitted_status": "active", "entered_reason": reason,
        "escaped_reason_in_html": escape(reason) in html,
        "rejection_message_in_html": "该资产未标记为需要保养。" in html,
        "before": {"plan": before, "audits": audits},
        "after": {"plan": after, "audits": after_audits},
    }, cls=DjangoJSONEncoder, sort_keys=True))
    # This is the first POST outcome assertion on the unchanged before page;
    # its view has no context form, so candidate-only form checks must follow it.
    assert escape(reason) in html, "rejected status change discarded entered reason"
    assert response.status_code == 200
    assert not page.context["form"].is_bound
    assert page.context["form"]["status"].value() == "active"
    assert 'data-unsaved-guard="false"' in page.content.decode()
    form = response.context["form"]
    assert form.is_bound
    assert form["status"].value() == "active" and form["reason"].value() == reason
    assert "该资产未标记为需要保养。" in form.non_field_errors()
    assert "该资产未标记为需要保养。" in html
    assert escape(reason) in html
    assert '<option value="active" selected>' in html
    assert 'data-unsaved-guard="true"' in html
    assert reverse("maintenance:plan-detail", args=[plan.pk]) in html and "取消" in html
    assert persisted_plan(plan) == before and plan_audits(plan) == audits
    ctx["asset"].refresh_from_db()
    assert ctx["asset"].is_maintenance_required is False


def test_http_end_without_reason_retains_ended_target_and_visible_field_error(client):
    ctx = maintenance_context("STATUSINPUTEMPTY")
    plan = ctx["plan"]
    before, audits = persisted_plan(plan), plan_audits(plan)
    client.force_login(ctx["equipment"])
    response = client.post(
        reverse("maintenance:plan-status", args=[plan.pk]),
        {"status": "ended", "reason": ""},
    )
    assert response.status_code == 200
    form = response.context["form"]
    assert form.is_bound
    assert form["status"].value() == "ended" and form["reason"].value() == ""
    assert list(form.errors["reason"]) == ["终止计划必须填写原因。"]
    html = response.content.decode()
    assert "终止计划必须填写原因。" in html
    assert '<option value="ended" selected>' in html
    assert 'data-unsaved-guard="true"' in html
    assert persisted_plan(plan) == before and plan_audits(plan) == audits


def test_http_existing_suspend_resume_end_transitions_preserve_service_audits(client):
    ctx = maintenance_context("STATUSINPUTVALID")
    plan = ctx["plan"]
    client.force_login(ctx["equipment"])
    url = reverse("maintenance:plan-status", args=[plan.pk])
    detail = reverse("maintenance:plan-detail", args=[plan.pk])
    original = persisted_plan(plan)
    baseline_audits = plan_audits(plan)
    previous_status = "active"
    for index, (target, reason) in enumerate((
        ("suspended", ""), ("active", ""), ("ended", "已完成保养计划替换"),
    ), start=1):
        page = client.get(url)
        assert page.status_code == 200 and not page.context["form"].is_bound
        assert page.context["form"]["status"].value() == "active"
        assert page.context["form"].fields["reason"].required is False
        reason_widget = re.search(r'<textarea\b[^>]*\bname="reason"[^>]*>', page.content.decode())
        assert reason_widget is not None and not re.search(r'\brequired(?:\s|=|>)', reason_widget.group())
        response = client.post(url, {"status": target, "reason": reason})
        assert response.status_code == 302 and response.url == detail
        plan.refresh_from_db()
        assert plan.status == target
        assert plan.last_maintenance_date == original["last_maintenance_date"]
        assert plan.next_maintenance_date == original["next_maintenance_date"]
        assert plan.ended_by_disposal_id is None and plan.status_before_disposal is None
        if target == "ended":
            assert plan.ended_reason == "manual" and plan.ended_at is not None
        else:
            assert plan.ended_reason is None and plan.ended_at is None
        audits = plan_audits(plan)
        assert audits[:len(baseline_audits)] == baseline_audits
        assert len(audits) == len(baseline_audits) + index
        latest = audits[-1]
        assert latest["action"] == "maintenance.plan_status_changed"
        assert latest["old_data_json"] == {"status": previous_status}
        assert latest["new_data_json"] == {"status": target, "reason": reason}
        previous_status = target
