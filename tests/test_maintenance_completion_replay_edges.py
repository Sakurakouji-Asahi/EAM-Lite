"""Additional independent-connection and existing-result permission contracts."""
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import date
import hashlib
import json
from queue import SimpleQueue
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import close_old_connections, connection
from django.test import Client, override_settings

from apps.audit.models import AuditLog
from apps.maintenance.domain import add_calendar_cycle, business_date
from apps.maintenance.models import MaintenanceRecord
from apps.maintenance.services import complete_maintenance
from tests.test_maintenance_completion_coherence import (
    open_page, payload, photo_facts, post_completion_photo, require_token,
    writer_update,
)
from tests.test_sprint3_support import JPEG_BYTES, make_employee, make_user
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db(transaction=True)


def replay_facts(ctx):
    facts = photo_facts(ctx)
    facts["responsible_employee_id"] = str(ctx["plan"].responsible_employee_id)
    facts["business_audits"] = AuditLog.objects.filter(company=ctx["company"]).count()
    return facts


def test_postgresql_same_page_multipart_retries_share_one_completion_and_photo(tmp_path, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL independent-connection multipart replay acceptance")
    ctx = maintenance_context("COMPEDGECONCURRENT")
    client, url, page = open_page(ctx)
    original = payload(page, content="同一页面的两个真实并发照片提交")
    token = require_token(ctx, page)
    captured_date = page.context["form"]["scheduled_date"].value()
    second_client = Client()
    second_client.force_login(ctx["responsible_user"])
    barrier = Barrier(2)
    observed_results = SimpleQueue()
    from apps.maintenance import views
    real_complete = views.complete_maintenance

    def synchronized_complete(**kwargs):
        # The view atomic block has started, but the service has not taken
        # Company or MaintenancePlan locks. Never put a barrier after that lock.
        assert kwargs["return_created"] is True
        assert kwargs["idempotency_key"] == original["idempotency_key"]
        assert kwargs["expected_instance_date"] == captured_date
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '10s'")
            cursor.execute("SET LOCAL statement_timeout = '20s'")
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid = cursor.fetchone()[0]
        # Timeout and BrokenBarrierError propagate as worker errors, never pass.
        barrier.wait(timeout=15)
        record, created = real_complete(**kwargs)
        observed_results.put((backend_pid, str(record.pk), created))
        return record, created

    monkeypatch.setattr(views, "complete_maintenance", synchronized_complete)

    def worker(worker_client):
        close_old_connections()
        try:
            # Each request owns a new upload stream and a separate client session.
            return post_completion_photo(worker_client, url, original)
        finally:
            close_old_connections()

    responses, errors = [], []
    with override_settings(MEDIA_ROOT=tmp_path):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, worker_client) for worker_client in (client, second_client)]
            for future in futures:
                try:
                    responses.append(future.result(timeout=35))
                except Exception as exc:
                    barrier.abort()
                    errors.append(f"{type(exc).__name__}: {exc}")
        assert not errors, f"Concurrent HTTP worker failed or timed out: {errors}"
        assert len(responses) == 2 and [response.status_code for response in responses] == [302, 302]
        assert responses[0].url == responses[1].url
        observations = [observed_results.get_nowait(), observed_results.get_nowait()]
        assert observed_results.empty()
        assert len({row[0] for row in observations}) == 2, "Requests did not use independent PostgreSQL sessions"
        assert len({row[1] for row in observations}) == 1
        assert all(type(row[2]) is bool for row in observations)
        assert sorted(row[2] for row in observations) == [False, True], "Exactly one request must own creation and upload"
        facts = photo_facts(ctx, redirect=responses[0].url)
        assert facts["records"] == facts["completed_audits"] == facts["operation_markers"] == 1
        assert facts["attachments"] == facts["attachment_links"] == facts["attachment_uploaded_audits"] == 1
        assert facts["record_id"] == observations[0][1]
        assert facts["photos"][0]["sha256"] == hashlib.sha256(JPEG_BYTES).hexdigest()
        assert default_storage.exists(facts["photos"][0]["storage_key"])
        assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1
        print("\nMAINTENANCE_REPLAY_EDGES_CONCURRENCY " + json.dumps({
            "scenario": "same_page_multipart_same_key", "backend_pids": sorted(row[0] for row in observations),
            "created_flags": sorted(row[2] for row in observations),
            "http_codes": [response.status_code for response in responses],
            "redirects": [response.url for response in responses], "facts": facts,
            "idempotency_key": original["idempotency_key"], "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "file_sha256": hashlib.sha256(JPEG_BYTES).hexdigest(),
        }, ensure_ascii=False, sort_keys=True))
    record = MaintenanceRecord.objects.get(maintenance_plan=ctx["plan"])
    assert record.scheduled_date == captured_date
    assert record.idempotency_key == original["idempotency_key"]
    assert ctx["plan"].next_maintenance_date == add_calendar_cycle(
        record.completed_date, ctx["plan"].cycle_value, ctx["plan"].cycle_unit,
    )
    assert token == original["completion_instance"]


@pytest.mark.parametrize("entry,revocation", [
    ("http", "role"), ("http", "assignee"),
    ("direct", "role"), ("direct", "assignee"),
])
def test_successful_completion_replay_rechecks_revoked_permission(tmp_path, entry, revocation):
    ctx = maintenance_context("COMPEDGEPERM" + entry + revocation)
    client, url, page = open_page(ctx)
    original = payload(page, content="原已完成请求重放也须重新核权")
    token = require_token(ctx, page)
    captured_date = page.context["form"]["scheduled_date"].value()
    stale_plan = copy(ctx["plan"])
    with override_settings(MEDIA_ROOT=tmp_path):
        first = post_completion_photo(client, url, original)
        assert first.status_code == 302
        saved = replay_facts(ctx)
        assert saved["records"] == saved["completed_audits"] == saved["operation_markers"] == 1
        assert saved["attachments"] == saved["attachment_links"] == saved["attachment_uploaded_audits"] == 1
        assert saved["photos"][0]["sha256"] == hashlib.sha256(JPEG_BYTES).hexdigest()
        if revocation == "role":
            ctx["responsible_user"].groups.clear()
        else:
            other_user = make_user("completion-replay-reassigned-" + entry, "employee")
            other_owner = make_employee(ctx["company"], ctx["department"], "REPLAY-NEW-" + entry, user=other_user)
            writer_update(ctx, owner=other_owner)
        # An existing marker is present. Snapshot after the authorized revocation.
        revoked = replay_facts(ctx)
        if revocation == "assignee":
            assert stale_plan.responsible_employee_id != ctx["plan"].responsible_employee_id
        else:
            assert not ctx["responsible_user"].groups.exists()
        if entry == "http":
            replay = post_completion_photo(client, url, original)
            assert replay.status_code in (403, 404), "Existing completion replay bypassed current HTTP permission"
        else:
            with pytest.raises(PermissionDenied):
                complete_maintenance(
                    actor=ctx["responsible_user"], plan=stale_plan,
                    scheduled_date=captured_date,
                    completed_date=date.fromisoformat(original["completed_date"]),
                    actual_content=original["actual_content"], result=original["result"],
                    problem_description=original["problem_description"], remark=original["remark"],
                    idempotency_key=original["idempotency_key"], expected_instance_date=captured_date,
                    return_created=True,
                )
        assert replay_facts(ctx) == revoked, "Denied existing-result replay changed records, photos, or business audits"
        assert default_storage.exists(revoked["photos"][0]["storage_key"])
        assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1
        assert token == original["completion_instance"]
        print("\nMAINTENANCE_REPLAY_EDGES_PERMISSION " + json.dumps({
            "scenario": "existing_result_permission_recheck", "entry": entry, "revocation": revocation,
            "http": replay.status_code if entry == "http" else None, "permission_denied": True,
            "before": revoked, "after": replay_facts(ctx), "idempotency_key": original["idempotency_key"],
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        }, ensure_ascii=False, sort_keys=True))
