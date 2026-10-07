"""Real maintenance edit pages must not overwrite a newer plan or completion."""
from datetime import timedelta
import json
import threading

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection, connections
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.maintenance.domain import add_calendar_cycle, business_date
from apps.maintenance.models import MaintenancePlan, MaintenanceRecord
from apps.maintenance.services import (
    complete_maintenance, create_maintenance_plan, update_maintenance_plan,
)
from tests.test_sprint3_support import make_user
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db(transaction=True)


def form_payload(page):
    assert page.status_code == 200
    return {field.html_name: "" if field.value() is None else str(field.value())
            for field in page.context["form"]}


def persisted_plan(plan):
    return MaintenancePlan._base_manager.filter(pk=plan.pk).values().get()


def plan_audits(plan):
    return list(AuditLog.objects.filter(object_type="MaintenancePlan", object_id=str(plan.pk))
                .order_by("created_at", "pk").values("id", "action", "old_data_json", "new_data_json"))


def committed_http_writer(*, actor_id, get_url, edit):
    """A real second request commits through a separate PostgreSQL connection."""
    main_pid = None
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        main_pid = cursor.fetchone()[0]
    observed, errors = {}, []

    def write():
        try:
            writer = Client()
            writer.force_login(get_user_model().objects.get(pk=actor_id))
            page = writer.get(get_url)
            data = form_payload(page)
            data.update(edit)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                observed["backend_pid"] = cursor.fetchone()[0]
            response = writer.post(get_url, data)
            observed["status"] = response.status_code
            observed["errors"] = str(response.context["form"].errors) if response.context else ""
            observed["redirect"] = response.url if response.status_code == 302 else ""
        except BaseException as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive(), "Independent legal HTTP writer did not commit"
    assert not errors, repr(errors)
    assert observed["backend_pid"] != main_pid
    assert observed["status"] == 302 and observed["errors"] == "", observed
    return observed


def assert_rejected_without_mutation(response, *, plan, old_data, expected_plan, expected_audits):
    assert response.status_code == 200
    assert "expected_revision" in response.context["form"].errors
    assert "重新打开最新编辑页面" in response.content.decode()
    assert response.context["form"].is_bound
    for name in ("name", "cycle_value", "cycle_unit", "responsible_employee", "standard_content", "first_due_date"):
        assert str(response.context["form"][name].value()) == str(old_data[name]), name
    if "expected_revision" in old_data:
        assert response.context["form"]["expected_revision"].value() == old_data["expected_revision"]
    assert persisted_plan(plan) == expected_plan
    assert plan_audits(plan) == expected_audits


def test_old_plan_edit_page_preserves_independent_newer_save(client):
    assert connection.vendor == "postgresql"
    context = maintenance_context("PLANREV-SAVE")
    plan = context["plan"]
    url = reverse("maintenance:plan-edit", args=[plan.pk])
    client.force_login(context["equipment"])
    old = form_payload(client.get(url))
    old["name"] = "旧页面中尚未提交的名称"
    initial = persisted_plan(plan)
    writer = committed_http_writer(actor_id=context["equipment"].pk, get_url=url, edit={
        "name": "另一页面已经保存的计划名称", "cycle_value": "3", "cycle_unit": "week",
        "responsible_employee": str(context["equipment_employee"].pk), "advance_notice_days": "5",
        "standard_content": "最新的安全核验标准，旧页面必须保留它",
        "first_due_date": (plan.first_due_date + timedelta(days=5)).isoformat(),
    })
    accepted = persisted_plan(plan)
    audits = plan_audits(plan)
    assert accepted["updated_at"] == initial["updated_at"]  # No fake timestamp or mocked business clock.
    assert accepted["responsible_employee_id"] == context["equipment_employee"].pk
    assert accepted["next_maintenance_date"] == accepted["first_due_date"]
    rejected = client.post(url, old)
    print("MAINTENANCE_EDIT_OBSERVATION=" + json.dumps({
        "case": "independent_newer_http_save", "writer": writer,
        "old_page": old, "accepted_plan": accepted, "actual_plan_after_old_post": persisted_plan(plan),
        "actual_response": rejected.status_code, "audit_count_before": len(audits),
        "audit_count_after": len(plan_audits(plan)),
    }, default=str, sort_keys=True), flush=True)
    assert_rejected_without_mutation(rejected, plan=plan, old_data=old,
                                    expected_plan=accepted, expected_audits=audits)
    fresh = form_payload(client.get(url))
    assert fresh["name"] == accepted["name"] and fresh["standard_content"] == accepted["standard_content"]
    fresh["name"] = "核对最新页后修改的计划名称"
    saved = client.post(url, fresh)
    assert saved.status_code == 302
    final = persisted_plan(plan)
    assert final["name"] == fresh["name"]
    assert final["responsible_employee_id"] == accepted["responsible_employee_id"]
    assert final["standard_content"] == accepted["standard_content"]
    assert len(plan_audits(plan)) == len(audits) + 1


def test_completion_changed_due_dates_invalidates_old_edit_without_rewriting_history(client):
    assert connection.vendor == "postgresql"
    context = maintenance_context("PLANREV-DONE")
    plan = context["plan"]
    url = reverse("maintenance:plan-edit", args=[plan.pk])
    client.force_login(context["equipment"])
    old = form_payload(client.get(url))
    initial = persisted_plan(plan)
    old.update({"name": "完成前旧页面的修改", "cycle_value": "2", "cycle_unit": "week"})
    writer = committed_http_writer(actor_id=context["responsible_user"].pk,
        get_url=reverse("maintenance:plan-complete", args=[plan.pk]), edit={
            "completed_date": business_date().isoformat(), "actual_content": "独立请求按标准完成检查",
            "result": "normal", "problem_description": "", "remark": "真实完成实例必须保留",
        })
    completed = persisted_plan(plan)
    assert completed["updated_at"] == initial["updated_at"]
    assert completed["last_maintenance_date"] == business_date()
    assert completed["next_maintenance_date"] == add_calendar_cycle(business_date(), 1, "month")
    records = list(MaintenanceRecord.objects.filter(maintenance_plan=plan).values())
    assert len(records) == 1 and records[0]["status"] == "confirmed"
    audit_rows = list(AuditLog.objects.order_by("pk").values())
    audits = plan_audits(plan)
    rejected = client.post(url, old)
    print("MAINTENANCE_EDIT_OBSERVATION=" + json.dumps({
        "case": "independent_http_completion", "writer": writer,
        "initial_dates": [initial["last_maintenance_date"], initial["next_maintenance_date"]],
        "completed_dates": [completed["last_maintenance_date"], completed["next_maintenance_date"]],
        "actual_plan_after_old_post": persisted_plan(plan), "actual_response": rejected.status_code,
    }, default=str, sort_keys=True), flush=True)
    assert_rejected_without_mutation(rejected, plan=plan, old_data=old,
                                    expected_plan=completed, expected_audits=audits)
    assert list(MaintenanceRecord.objects.filter(maintenance_plan=plan).values()) == records
    assert list(AuditLog.objects.order_by("pk").values()) == audit_rows
    fresh = form_payload(client.get(url))
    fresh.update({"cycle_value": "2", "cycle_unit": "week"})
    assert client.post(url, fresh).status_code == 302
    current = persisted_plan(plan)
    assert current["last_maintenance_date"] == completed["last_maintenance_date"]
    assert current["next_maintenance_date"] == add_calendar_cycle(business_date(), 2, "week")
    assert list(MaintenanceRecord.objects.filter(maintenance_plan=plan).values()) == records


def test_old_preview_cannot_replace_its_revision_and_then_overwrite_newer_plan(client):
    assert connection.vendor == "postgresql"
    context = maintenance_context("PLANREV-PREVIEW")
    plan = context["plan"]
    url = reverse("maintenance:plan-edit", args=[plan.pk])
    client.force_login(context["equipment"])
    old = form_payload(client.get(url))
    committed_http_writer(actor_id=context["equipment"].pk, get_url=url,
        edit={"name": "已保存的新预览依据", "standard_content": "新标准", "cycle_value": "4"})
    accepted, audits = persisted_plan(plan), plan_audits(plan)
    old.update({"cycle_value": "2", "action": "preview"})
    preview = client.post(url, old)
    print("MAINTENANCE_EDIT_OBSERVATION=" + json.dumps({
        "case": "stale_preview", "actual_response": preview.status_code,
        "actual_preview": preview.context.get("date_preview"),
        "actual_errors": dict(preview.context["form"].errors),
        "latest_plan": persisted_plan(plan),
    }, default=str, sort_keys=True), flush=True)
    assert_rejected_without_mutation(preview, plan=plan, old_data=old,
                                    expected_plan=accepted, expected_audits=audits)
    assert not preview.context.get("date_preview")
    old.pop("action")
    saved = client.post(url, old)
    assert_rejected_without_mutation(saved, plan=plan, old_data=old,
                                    expected_plan=accepted, expected_audits=audits)


def test_preview_retains_original_revision_and_fresh_save_recalculates_only_future(client):
    context = maintenance_context("PLANREV-FRESH")
    plan = context["plan"]
    record = complete_maintenance(actor=context["responsible_user"], plan=plan,
        scheduled_date=plan.next_maintenance_date, completed_date=business_date(),
        actual_content="先完成一次", result="normal", idempotency_key="planrev-fresh-complete")
    plan.refresh_from_db()
    client.force_login(context["equipment"])
    url = reverse("maintenance:plan-edit", args=[plan.pk])
    data = form_payload(client.get(url))
    assert data["expected_revision"]
    snapshot, audits = persisted_plan(plan), plan_audits(plan)
    records = list(MaintenanceRecord.objects.filter(maintenance_plan=plan).values())
    data.update({"cycle_value": "2", "cycle_unit": "month", "action": "preview"})
    preview = client.post(url, data)
    assert preview.status_code == 200 and not preview.context["form"].errors
    assert preview.context["date_preview"]["next_date"] == add_calendar_cycle(record.completed_date, 2, "month")
    assert preview.context["form"]["expected_revision"].value() == data["expected_revision"]
    assert persisted_plan(plan) == snapshot and plan_audits(plan) == audits
    data = form_payload(preview)
    saved = client.post(url, data)
    assert saved.status_code == 302
    current = persisted_plan(plan)
    assert current["next_maintenance_date"] == add_calendar_cycle(record.completed_date, 2, "month")
    assert list(MaintenanceRecord.objects.filter(maintenance_plan=plan).values()) == records
    assert len(plan_audits(plan)) == len(audits) + 1


@pytest.mark.parametrize("invalidity", ["missing", "tampered", "other_actor", "other_plan", "expired"])
def test_invalid_revision_preserves_inputs_and_cannot_write(client, monkeypatch, invalidity):
    context = maintenance_context("PLANREV-INVALID")
    plan = context["plan"]
    url = reverse("maintenance:plan-edit", args=[plan.pk])
    client.force_login(context["equipment"])
    data = form_payload(client.get(url))
    token = data["expected_revision"]
    snapshot, audits = persisted_plan(plan), plan_audits(plan)
    data["name"] = "签名错误时也要保留此输入"
    if invalidity == "missing":
        data.pop("expected_revision")
    elif invalidity == "tampered":
        data["expected_revision"] = token + "invalid"
    elif invalidity == "other_actor":
        actor = make_user("planrev-other-equipment", "equipment")
        client.force_login(actor)
        data["expected_revision"] = form_payload(client.get(url))["expected_revision"]
        client.force_login(context["equipment"])
    elif invalidity == "other_plan":
        other = create_maintenance_plan(actor=context["equipment"], company=context["company"],
            asset=context["asset"], name="另一保养计划", cycle_value=1, cycle_unit="week",
            responsible_employee=context["responsible"], advance_notice_days=1,
            standard_content="另一计划标准", first_due_date=plan.first_due_date)
        data["expected_revision"] = form_payload(client.get(reverse("maintenance:plan-edit", args=[other.pk])))["expected_revision"]
    else:
        original_clock = signing.time.time
        future = original_clock() + 24 * 60 * 60 + 5
        monkeypatch.setattr(signing.time, "time", lambda: future)
    response = client.post(url, data)
    assert_rejected_without_mutation(response, plan=plan, old_data=data,
                                    expected_plan=snapshot, expected_audits=audits)


def test_empty_post_is_bound_and_does_not_reissue_a_page_revision(client):
    context = maintenance_context("PLANREV-EMPTY")
    plan = context["plan"]
    client.force_login(context["equipment"])
    snapshot, audits = persisted_plan(plan), plan_audits(plan)
    response = client.post(reverse("maintenance:plan-edit", args=[plan.pk]), {})
    assert response.status_code == 200
    assert response.context["form"].is_bound
    assert "expected_revision" in response.context["form"].errors
    assert response.context["form"]["expected_revision"].value() in (None, "")
    assert 'data-unsaved-guard="true"' in response.content.decode()
    assert persisted_plan(plan) == snapshot and plan_audits(plan) == audits


def test_role_removed_after_page_open_still_blocks_edit_before_any_write(client):
    context = maintenance_context("PLANREV-PERMISSION")
    plan = context["plan"]
    client.force_login(context["equipment"])
    url = reverse("maintenance:plan-edit", args=[plan.pk])
    data = form_payload(client.get(url))
    snapshot, audits = persisted_plan(plan), plan_audits(plan)
    context["equipment"].groups.clear()
    assert client.post(url, data).status_code in (403, 404)
    assert persisted_plan(plan) == snapshot and plan_audits(plan) == audits


def test_trusted_service_can_update_without_browser_revision_but_snapshot_service_rechecks_fresh_lock(client):
    from apps.maintenance.plan_revision import decode_plan_edit_revision
    context = maintenance_context("PLANREV-SERVICE")
    plan = context["plan"]
    client.force_login(context["equipment"])
    token = form_payload(client.get(reverse("maintenance:plan-edit", args=[plan.pk])))["expected_revision"]
    revision = decode_plan_edit_revision(token=token, actor=context["equipment"], company=context["company"], plan=plan)
    values = {name: getattr(plan, name) for name in (
        "name", "cycle_value", "cycle_unit", "responsible_employee", "advance_notice_days", "standard_content", "first_due_date")}
    updated = update_maintenance_plan(actor=context["equipment"], plan=plan,
        **{**values, "name": "受控内部服务的最新内容"})
    snapshot, audits = persisted_plan(plan), plan_audits(plan)
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError, match="其他操作更新"):
        update_maintenance_plan(actor=context["equipment"], plan=plan,
            expected_revision=revision, **values)
    assert persisted_plan(plan) == snapshot and plan_audits(plan) == audits
    assert updated.name == snapshot["name"]
