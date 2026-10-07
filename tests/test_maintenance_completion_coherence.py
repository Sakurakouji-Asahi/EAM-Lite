"""Real GET -> independent controlled writer -> old POST regressions.

The first three tests are also valid on the unmodified ebd075 baseline.
Their intended assertions must fail there; the remaining tests need the candidate.
"""
import hashlib
import io
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from PIL import Image
from django.urls import reverse

from apps.assets.models import AttachmentLink
from apps.audit.models import AuditLog
from apps.masterdata.models import Attachment
from apps.maintenance.domain import business_date
from apps.maintenance.models import MaintenanceRecord
from apps.maintenance.services import (
    complete_maintenance, create_maintenance_plan, update_maintenance_plan,
    void_maintenance_record,
)
from tests.test_sprint3_support import JPEG_BYTES, make_company, make_employee, make_user
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db(transaction=True)
SALT = "maintenance.completion-instance.v1"
MAX_AGE = 24 * 60 * 60


def payload(page, *, completed_date=None, content="旧页面实际内容"):
    assert page.status_code == 200
    form = page.context["form"]
    # Disabled scheduled_date is intentionally absent, exactly as in a browser.
    result = {
        "idempotency_key": form["idempotency_key"].value(),
        "completed_date": (completed_date or business_date()).isoformat(),
        "actual_content": content, "result": "normal",
        "problem_description": "", "remark": "旧页面保留的备注",
        "security_class": "A0",
    }
    if "completion_instance" in form.fields:
        result["completion_instance"] = form["completion_instance"].value()
    return result


def state(ctx):
    ctx["plan"].refresh_from_db()
    return {
        "next_date": ctx["plan"].next_maintenance_date.isoformat(),
        "last_date": str(ctx["plan"].last_maintenance_date),
        "records": list(MaintenanceRecord.objects.filter(maintenance_plan=ctx["plan"])
                        .order_by("created_at", "pk").values(
                            "id", "scheduled_date", "completed_date", "content_snapshot", "status")),
        "completed_audits": AuditLog.objects.filter(
            company=ctx["company"], action="maintenance.completed").count(),
        "operation_markers": AuditLog.objects.filter(
            company=ctx["company"], action="maintenance.idempotency.complete").count(),
    }


def report(name, before, after, response):
    print("MAINTENANCE_COMPLETION_COHERENCE " + json.dumps({
        "scenario": name, "http": response.status_code, "before": before,
        "after": after,
    }, ensure_ascii=False, sort_keys=True, default=str))


def writer_update(ctx, *, due=None, owner=None):
    # No direct model update: exercise the existing independent production writer.
    plan = ctx["plan"]
    return update_maintenance_plan(
        actor=ctx["equipment"], plan=plan, name=plan.name,
        cycle_value=plan.cycle_value, cycle_unit=plan.cycle_unit,
        responsible_employee=owner or ctx["responsible"],
        advance_notice_days=plan.advance_notice_days,
        standard_content=plan.standard_content,
        first_due_date=due or plan.first_due_date,
    )


def open_page(ctx, *, actor=None, url=None):
    client = Client()
    client.force_login(actor or ctx["responsible_user"])
    url = url or reverse("maintenance:plan-complete", args=[ctx["plan"].pk])
    page = client.get(url)
    assert page.status_code == 200
    assert page.context["form"].fields["scheduled_date"].disabled
    return client, url, page


def require_token(ctx, page, *, actor=None, mode="current"):
    form = page.context["form"]
    token = form["completion_instance"].value()
    decoded = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    assert decoded == {
        "version": 1, "actor": str((actor or ctx["responsible_user"]).pk),
        "company": str(ctx["company"].pk), "plan": str(ctx["plan"].pk),
        "scheduled_date": form["scheduled_date"].value().isoformat(),
        "mode": mode,
    }
    return token


def test_old_completion_page_does_not_create_unseen_next_instance():
    ctx = maintenance_context("COMPOLDNEXT")
    client, url, page = open_page(ctx)
    old = payload(page)
    displayed = page.context["form"]["scheduled_date"].value()
    writer = complete_maintenance(
        actor=ctx["equipment"], plan=ctx["plan"], scheduled_date=displayed,
        completed_date=business_date() - timedelta(days=1),
        actual_content="独立办理人已完成旧实例", result="normal",
        idempotency_key="completion-independent-writer",
    )
    saved = state(ctx)
    assert writer.scheduled_date == displayed
    assert ctx["plan"].next_maintenance_date != displayed
    response = client.post(url, old)
    actual = state(ctx)
    report("independent_completion", saved, actual, response)
    assert actual == saved, "Old page recorded its content against an unseen next instance"
    assert response.status_code == 200
    assert response.context["form"]["actual_content"].value() == old["actual_content"]
    assert response.context["form"]["scheduled_date"].value() == displayed


def test_old_completion_page_does_not_adopt_changed_first_due_date():
    ctx = maintenance_context("COMPOLDDUE")
    client, url, page = open_page(ctx)
    old = payload(page)
    displayed = page.context["form"]["scheduled_date"].value()
    writer_update(ctx, due=displayed + timedelta(days=1))
    saved = state(ctx)
    response = client.post(url, old)
    actual = state(ctx)
    report("independent_plan_date_change", saved, actual, response)
    assert actual == saved, "Old page silently adopted a newly saved plan date"
    assert response.status_code == 200
    assert response.context["form"]["actual_content"].value() == old["actual_content"]
    assert response.context["form"]["scheduled_date"].value() == displayed


def test_successful_original_page_replay_returns_original_record():
    ctx = maintenance_context("COMPREPLAY")
    client, url, page = open_page(ctx)
    original = payload(page, content="成功提交应可安全重放")
    first = client.post(url, original)
    assert first.status_code == 302
    saved = state(ctx)
    response = client.post(url, original)
    actual = state(ctx)
    report("same_successful_page_replay", saved, actual, response)
    assert response.status_code == 302, "Identical successful page replay lost its stable instance date"
    assert response.url == first.url
    assert actual == saved
    changed = client.post(url, {**original, "remark": "同一个 key 的不同内容"})
    assert changed.status_code == 200
    assert "不同请求参数" in changed.content.decode()
    assert state(ctx) == saved


def test_invalid_post_keeps_original_signed_date_token_and_text():
    ctx = maintenance_context("COMPINVALID")
    client, url, page = open_page(ctx)
    token = require_token(ctx, page)
    displayed = page.context["form"]["scheduled_date"].value()
    original = payload(page)
    writer_update(ctx, due=displayed + timedelta(days=1))
    saved = state(ctx)
    response = client.post(url, {**original, "completed_date": "不是日期"})
    assert response.status_code == 200
    form = response.context["form"]
    assert "completed_date" in form.errors
    assert form["completion_instance"].value() == token
    assert form["scheduled_date"].value() == displayed
    assert form["actual_content"].value() == original["actual_content"]
    assert form["remark"].value() == original["remark"]
    assert state(ctx) == saved
    # Fixing the user's field cannot refresh the old token behind their back.
    again = client.post(url, original)
    assert again.status_code == 200
    assert again.context["form"]["completion_instance"].value() == token
    assert state(ctx) == saved


@pytest.mark.parametrize("kind", ["missing", "tampered", "expired"])
def test_completion_rejects_untrusted_instance_token_without_writes(kind):
    ctx = maintenance_context("COMPTOKEN" + kind)
    client, url, page = open_page(ctx)
    original = payload(page)
    token = require_token(ctx, page)
    if kind == "missing":
        original.pop("completion_instance")
    elif kind == "tampered":
        head, sep, signature = token.rpartition(":")
        original["completion_instance"] = head + sep + ("a" if signature[0] != "a" else "b") + signature[1:]
    else:
        # Advance the verifier's clock only after a genuine signed GET.
        decoded = signing.loads(token, salt=SALT)
        assert decoded["scheduled_date"] == page.context["form"]["scheduled_date"].value().isoformat()
    saved = state(ctx)
    if kind == "expired":
        import time
        with patch("django.core.signing.time.time", return_value=time.time() + MAX_AGE + 1):
            response = client.post(url, original)
    else:
        response = client.post(url, original)
    assert response.status_code == 200
    assert "completion_instance" in response.context["form"].errors
    assert "重新打开" in response.content.decode()
    assert state(ctx) == saved
    if kind == "missing":
        empty = client.post(url, {})
        assert empty.status_code == 200
        assert empty.context["form"].is_bound
        assert empty.context["form"]["completion_instance"].value() in (None, "")
        assert state(ctx) == saved


@pytest.mark.parametrize("identity", ["actor", "company", "plan"])
def test_signed_completion_instance_is_bound_to_its_get_context(identity):
    ctx = maintenance_context("COMPBIND" + identity)
    client, url, page = open_page(ctx)
    original = payload(page)
    require_token(ctx, page)
    if identity == "actor":
        _, _, other = open_page(ctx, actor=ctx["equipment"])
        original["completion_instance"] = require_token(ctx, other, actor=ctx["equipment"])
    elif identity == "company":
        from apps.maintenance.completion_instance import completion_instance_token
        # Another company's valid signature, without selecting or mutating that company.
        other_company = make_company("COMPBINDOTHER", active=False)
        from copy import copy
        foreign_plan = copy(ctx["plan"])
        foreign_plan.company_id = other_company.pk
        original["completion_instance"] = completion_instance_token(
            actor=ctx["responsible_user"], plan=foreign_plan,
            scheduled_date=page.context["form"]["scheduled_date"].value(),
        )
    else:
        plan = create_maintenance_plan(
            actor=ctx["equipment"], company=ctx["company"], asset=ctx["asset"],
            name="同一资产的另一保养计划", cycle_value=1, cycle_unit="month",
            responsible_employee=ctx["responsible"], advance_notice_days=3,
            standard_content="另一计划", first_due_date=ctx["plan"].first_due_date,
        )
        other_url = reverse("maintenance:plan-complete", args=[plan.pk])
        other = client.get(other_url)
        assert other.status_code == 200
        original["completion_instance"] = other.context["form"]["completion_instance"].value()
    saved = state(ctx)
    response = client.post(url, original)
    assert response.status_code == 200
    assert "completion_instance" in response.context["form"].errors
    assert state(ctx) == saved


def test_service_rechecks_fresh_date_and_permission_after_form_validation():
    ctx = maintenance_context("COMPLOCK")
    _, _, page = open_page(ctx)
    require_token(ctx, page)
    captured = page.context["form"]["scheduled_date"].value()
    # Keep this genuine GET object stale while another controlled writer commits.
    from copy import copy
    stale_plan = copy(ctx["plan"])
    from apps.maintenance.forms import MaintenanceCompletionForm
    form = MaintenanceCompletionForm(payload(page), actor=ctx["responsible_user"], plan=stale_plan)
    assert form.is_valid(), str(form.errors)
    assert form.cleaned_data["completion_instance"] == captured
    writer_update(ctx, due=captured + timedelta(days=1))
    saved = state(ctx)
    values = dict(
        actor=ctx["responsible_user"], plan=stale_plan, scheduled_date=captured,
        completed_date=business_date(), actual_content="服务锁前读取的旧页面",
        result="normal", idempotency_key="completion-fresh-lock",
        expected_instance_date=form.cleaned_data["completion_instance"],
    )
    with pytest.raises(ValidationError, match="计划日期已变化"):
        complete_maintenance(**values)
    assert state(ctx) == saved
    other_user = make_user("completion-lock-other", "employee")
    other_owner = make_employee(ctx["company"], ctx["department"], "COMPLOCK-OTHER", user=other_user)
    writer_update(ctx, owner=other_owner)
    saved_after_permission_change = state(ctx)
    with pytest.raises(PermissionDenied):
        complete_maintenance(**values)
    assert state(ctx) == saved_after_permission_change


def test_historical_redo_uses_displayed_date_and_rejects_current_mode_token():
    ctx = maintenance_context("COMPREDO")
    first_due = ctx["plan"].next_maintenance_date
    first = complete_maintenance(
        actor=ctx["equipment"], plan=ctx["plan"], scheduled_date=first_due,
        completed_date=business_date() - timedelta(days=2), actual_content="待修正的历史完成",
        result="normal", idempotency_key="completion-redo-first",
    )
    ctx["plan"].refresh_from_db()
    second = complete_maintenance(
        actor=ctx["equipment"], plan=ctx["plan"], scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=business_date() - timedelta(days=1), actual_content="后一有效实例",
        result="normal", idempotency_key="completion-redo-second",
    )
    void_maintenance_record(actor=ctx["equipment"], record=first,
                            reason="更正实际内容", idempotency_key="completion-redo-void")
    ctx["plan"].refresh_from_db()
    assert ctx["plan"].next_maintenance_date != first_due
    redo_url = reverse("maintenance:record-redo", args=[first.pk])
    client, _, page = open_page(ctx, actor=ctx["equipment"], url=redo_url)
    token = require_token(ctx, page, actor=ctx["equipment"], mode="redo")
    assert page.context["form"]["scheduled_date"].value() == first_due
    original = payload(page, completed_date=first.completed_date, content="历史实例修正后的内容")
    current = client.get(reverse("maintenance:plan-complete", args=[ctx["plan"].pk]))
    bad = {**original, "completion_instance": current.context["form"]["completion_instance"].value()}
    saved = state(ctx)
    rejected = client.post(redo_url, bad)
    assert rejected.status_code == 200
    assert "completion_instance" in rejected.context["form"].errors
    assert state(ctx) == saved
    response = client.post(redo_url, original)
    assert response.status_code == 302
    rebuilt = MaintenanceRecord.objects.get(content_snapshot=original["actual_content"])
    assert rebuilt.scheduled_date == first_due and rebuilt.pk != first.pk
    assert rebuilt.completed_date < second.completed_date
    ctx["plan"].refresh_from_db()
    assert ctx["plan"].next_maintenance_date.isoformat() == saved["next_date"]
    assert token == original["completion_instance"]


def photo_facts(ctx, *, redirect=None):
    # Scope to this plan's evidence; factory asset photos are separate evidence.
    links = AttachmentLink.objects.filter(maintenance_record__maintenance_plan=ctx["plan"])
    records = MaintenanceRecord.objects.filter(maintenance_plan=ctx["plan"])
    record = records.order_by("created_at", "pk").first()
    ctx["plan"].refresh_from_db()
    return {
        "records": records.count(),
        "attachments": Attachment.objects.filter(
            business_link__maintenance_record__maintenance_plan=ctx["plan"]).count(),
        "attachment_links": links.count(),
        "attachment_uploaded_audits": AuditLog.objects.filter(
            company=ctx["company"], action="maintenance.attachment_uploaded").count(),
        "completed_audits": AuditLog.objects.filter(
            company=ctx["company"], action="maintenance.completed").count(),
        "operation_markers": AuditLog.objects.filter(
            company=ctx["company"], action="maintenance.idempotency.complete").count(),
        "record_id": str(record.pk) if record else None,
        "redirect": redirect,
        "photos": [{"link_id": str(link.pk), "attachment_id": str(link.attachment_id),
                    "storage_key": link.attachment.storage_key,
                    "sha256": link.attachment.sha256, "security_class": link.security_class}
                   for link in links.select_related("attachment").order_by("pk")],
        "next_date": ctx["plan"].next_maintenance_date.isoformat(),
        "last_date": str(ctx["plan"].last_maintenance_date),
    }


def post_completion_photo(client, url, original, data=JPEG_BYTES, *, follow=False):
    # Multipart encoders consume streams: each request gets the same bytes afresh.
    return client.post(url, {**original, "uploaded_file": SimpleUploadedFile(
        "completion.jpg", data, content_type="image/jpeg")}, follow=follow)


@override_settings(MEDIA_ROOT="var/test-media-maintenance-completion-photo-replay")
def test_successful_photo_completion_replay_does_not_duplicate_evidence():
    ctx = maintenance_context("COMPPHOTOREPLAY")
    client, url, page = open_page(ctx)
    original = payload(page, content="同页照片完成可安全重放")
    token = require_token(ctx, page)
    first = post_completion_photo(client, url, original)
    assert first.status_code == 302
    saved = photo_facts(ctx, redirect=first.url)
    assert saved["records"] == saved["completed_audits"] == saved["operation_markers"] == 1
    assert saved["attachments"] == saved["attachment_links"] == saved["attachment_uploaded_audits"] == 1
    assert saved["photos"][0]["sha256"] == hashlib.sha256(JPEG_BYTES).hexdigest()
    replay = post_completion_photo(client, url, original)
    actual = photo_facts(ctx, redirect=getattr(replay, "url", None))
    print("MAINTENANCE_COMPLETION_PHOTO_COHERENCE " + json.dumps({
        "scenario": "same_successful_multipart_replay", "first_http": first.status_code,
        "replay_http": replay.status_code, "first": saved, "after": actual,
        "actor_id": str(ctx["responsible_user"].pk), "company_id": str(ctx["company"].pk),
        "plan_id": str(ctx["plan"].pk), "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "idempotency_key": original["idempotency_key"],
        "file_sha256": hashlib.sha256(JPEG_BYTES).hexdigest(),
    }, ensure_ascii=False, sort_keys=True))
    assert replay.status_code == 302
    assert replay.url == first.url
    assert (actual["attachments"] == actual["attachment_links"] == actual["attachment_uploaded_audits"] == 1), "Successful completion replay duplicated its original photo"
    assert actual == saved
    # Consume prior messages so the next response proves its own replay message.
    client.get(replay.url)
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(stream, format="JPEG")
    different_photo = stream.getvalue()
    Image.open(io.BytesIO(different_photo)).verify()
    assert different_photo != JPEG_BYTES
    third = post_completion_photo(client, url, original, different_photo, follow=True)
    assert third.redirect_chain == [(first.url, 302)]
    assert third.status_code == 200
    assert "返回原已保存记录，补充证据请在记录页上传" in third.content.decode()
    assert photo_facts(ctx, redirect=first.url) == saved


@override_settings(MEDIA_ROOT="var/test-media-maintenance-completion-photo-rollback")
def test_failed_photo_upload_rolls_back_completion_then_same_page_can_retry():
    ctx = maintenance_context("COMPPHOTOROLLBACK")
    client, url, page = open_page(ctx)
    original = payload(page, content="附件失败后保留输入重试")
    token = require_token(ctx, page)
    baseline = photo_facts(ctx)
    rejected = post_completion_photo(client, url, original, b"invalid JPEG image data")
    after_rejection = photo_facts(ctx)
    assert rejected.status_code == 200
    assert "图片" in rejected.content.decode()
    assert after_rejection == baseline, "Invalid completion photo did not roll back the entire completion"
    form = rejected.context["form"]
    token_kept = form["completion_instance"].value() == token
    assert token_kept
    assert form["idempotency_key"].value() == original["idempotency_key"]
    assert form["actual_content"].value() == original["actual_content"]
    assert form["remark"].value() == original["remark"]
    assert form["scheduled_date"].value() == page.context["form"]["scheduled_date"].value()
    # No new GET, key or signature: only the invalid photo is corrected.
    success = post_completion_photo(client, url, original)
    after_success = photo_facts(ctx, redirect=getattr(success, "url", None))
    print("MAINTENANCE_COMPLETION_PHOTO_ROLLBACK " + json.dumps({
        "scenario": "invalid_photo_then_same_page_valid_retry", "reject_http": rejected.status_code,
        "success_http": success.status_code, "before": baseline,
        "after_rejection": after_rejection, "after_success": after_success,
        "token_kept": token_kept, "idempotency_key": original["idempotency_key"],
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "file_sha256": hashlib.sha256(JPEG_BYTES).hexdigest(),
    }, ensure_ascii=False, sort_keys=True))
    assert success.status_code == 302
    assert after_success["records"] == after_success["completed_audits"] == after_success["operation_markers"] == 1
    assert after_success["attachments"] == after_success["attachment_links"] == after_success["attachment_uploaded_audits"] == 1
    assert after_success["photos"][0]["sha256"] == hashlib.sha256(JPEG_BYTES).hexdigest()
    record = MaintenanceRecord.objects.get(maintenance_plan=ctx["plan"])
    assert record.content_snapshot == original["actual_content"]
    assert record.idempotency_key == original["idempotency_key"]
