from __future__ import annotations

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from apps.assets.models import AttachmentLink
from apps.maintenance.models import (
    MaintenancePlan,
    MaintenanceProblem,
    MaintenanceRecord,
)
from apps.maintenance.services import (
    close_maintenance_problem,
    complete_maintenance,
    upload_maintenance_attachment,
    void_maintenance_attachment,
    void_maintenance_record,
)
from tests.test_sprint3_support import (
    JPEG_BYTES,
    direct_attachment,
    make_company,
    make_department,
    make_employee,
    make_user,
)
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db(transaction=True)


def _postgresql_only():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred/trigger acceptance")


def _enable_capability(name):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, 'on', true)",
            [f"eam_lite.{name}"],
        )


def _record_values(ctx, *, result="normal", key="S9-DB-record"):
    return {
        "company": ctx["company"],
        "maintenance_plan": ctx["plan"],
        "asset": ctx["asset"],
        "scheduled_date": ctx["plan"].next_maintenance_date,
        "completed_date": timezone.localdate(),
        "completed_by": ctx["responsible"],
        "content_snapshot": "数据库约束验收保养内容",
        "result": result,
        "status": "confirmed",
        "idempotency_key": key,
    }


def test_v1_model_surface_has_no_runtime_hour_severity_or_repair_order():
    assert set(MaintenancePlan.CycleUnit.values) == {"day", "week", "month", "year"}
    assert "runtime_hour" not in MaintenancePlan.CycleUnit.values
    assert "severity" not in {field.name for field in MaintenanceProblem._meta.fields}
    with pytest.raises(LookupError):
        apps.get_model("maintenance", "RepairOrder")

def test_postgresql_deferred_pair_rejects_problem_result_without_one_problem():
    _postgresql_only()
    ctx = maintenance_context("S9DBPAIRNONE")

    with pytest.raises(DatabaseError, match="exactly one problem"), transaction.atomic():
        _enable_capability("controlled_maintenance_record_insert")
        MaintenanceRecord._base_manager.create(
            **_record_values(
                ctx,
                result="problem_found",
                key="S9DBPAIRNONE-record",
            )
        )

    assert not MaintenanceRecord._base_manager.filter(
        company=ctx["company"], idempotency_key="S9DBPAIRNONE-record"
    ).exists()


def test_postgresql_rejects_problem_for_normal_record_and_preserves_pair():
    _postgresql_only()
    ctx = maintenance_context("S9DBPAIRNORMAL")

    with transaction.atomic():
        _enable_capability("controlled_maintenance_record_insert")
        record = MaintenanceRecord._base_manager.create(
            **_record_values(ctx, key="S9DBPAIRNORMAL-record")
        )

    with pytest.raises(DatabaseError), transaction.atomic():
        _enable_capability("controlled_maintenance_problem_insert")
        MaintenanceProblem._base_manager.create(
            company=ctx["company"],
            maintenance_record=record,
            asset=ctx["asset"],
            description="正常结果不应允许问题行",
            status="open",
        )

    assert not MaintenanceProblem._base_manager.filter(
        maintenance_record=record
    ).exists()


def test_postgresql_record_and_problem_are_append_only_at_database_layer():
    _postgresql_only()
    ctx = maintenance_context("S9DBAPPEND")
    record = complete_maintenance(
        actor=ctx["responsible_user"],
        plan=ctx["plan"],
        scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="已完成数据库不可变验收",
        result="problem_found",
        problem_description="发现需跟进问题",
        idempotency_key="S9DBAPPEND-complete",
    )
    problem = record.problem

    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE maintenance_maintenancerecord SET content_snapshot=%s WHERE id=%s",
            ["越权篡改", record.pk],
        )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM maintenance_maintenancerecord WHERE id=%s", [record.pk]
        )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE maintenance_maintenanceproblem SET description=%s WHERE id=%s",
            ["越权篡改", problem.pk],
        )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM maintenance_maintenanceproblem WHERE id=%s", [problem.pk]
        )

    record.refresh_from_db()
    problem.refresh_from_db()
    assert record.content_snapshot == "已完成数据库不可变验收"
    assert problem.description == "发现需跟进问题"


def test_postgresql_maintenance_attachment_requires_one_real_same_company_target():
    _postgresql_only()
    ctx = maintenance_context("S9DBATT")
    record = complete_maintenance(
        actor=ctx["responsible_user"],
        plan=ctx["plan"],
        scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="完成附件数据库验收",
        result="problem_found",
        problem_description="需要附件跟进",
        idempotency_key="S9DBATT-complete",
    )

    both_attachment = direct_attachment(
        ctx["company"],
        ctx["equipment"],
        key="private/maintenance/S9DBATT-both.jpg",
        filename="both.jpg",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=ctx["company"],
            attachment=both_attachment,
            maintenance_record=record,
            maintenance_problem=record.problem,
            role="maintenance",
            security_class="A0",
            created_by=ctx["equipment"],
        )

    wrong_role_attachment = direct_attachment(
        ctx["company"],
        ctx["equipment"],
        key="private/maintenance/S9DBATT-role.jpg",
        filename="role.jpg",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=ctx["company"],
            attachment=wrong_role_attachment,
            maintenance_record=record,
            role="photo",
            security_class="A0",
            created_by=ctx["equipment"],
        )

    other_company = make_company("S9DBATT-OTHER", active=False)
    other_attachment = direct_attachment(
        other_company,
        ctx["equipment"],
        key="private/maintenance/S9DBATT-other.jpg",
        filename="other.jpg",
    )
    with pytest.raises(DatabaseError), transaction.atomic():
        AttachmentLink._base_manager.create(
            company=ctx["company"],
            attachment=other_attachment,
            maintenance_problem=record.problem,
            role="maintenance",
            security_class="A0",
            created_by=ctx["equipment"],
        )


def test_postgresql_rejects_cross_company_plan_responsible_even_with_insert_capability():
    _postgresql_only()
    ctx = maintenance_context("S9DBCOMPANY")
    other_company = make_company("S9DBCOMPANY-OTHER", active=False)
    other_department = make_department(other_company, "S9DBCOMPANY-OTHER-D")
    other_employee = make_employee(
        other_company,
        other_department,
        "S9DBCOMPANY-OTHER-E",
    )

    with pytest.raises(DatabaseError, match="same company"), transaction.atomic():
        _enable_capability("controlled_maintenance_plan_insert")
        MaintenancePlan._base_manager.create(
            company=ctx["company"],
            asset=ctx["asset"],
            name="跨公司责任人计划",
            cycle_value=1,
            cycle_unit="month",
            advance_notice_days=1,
            responsible_employee=other_employee,
            standard_content="不应保存",
            first_due_date=timezone.localdate(),
            next_maintenance_date=timezone.localdate(),
            status="active",
        )


def test_postgresql_controlled_disposal_restore_rejects_inactive_responsible():
    _postgresql_only()
    ctx = maintenance_context("S9DBRESTORERESP")
    from apps.assets.lifecycle_services import complete_disposal
    from apps.masterdata.services import set_employee_active
    from tests.test_sprint9_disposal import _ready_disposal

    disposal = complete_disposal(
        actor=ctx["equipment"],
        disposal=_ready_disposal(ctx, "S9DBRESTORERESP"),
        idempotency_key="S9DBRESTORERESP-complete",
    )
    hr = make_user("s9dbrestoreresp-hr", "hr")
    set_employee_active(actor=hr, employee=ctx["responsible"], is_active=False)

    with pytest.raises(DatabaseError, match="active in same company"), transaction.atomic():
        _enable_capability("controlled_maintenance_plan_mutation")
        MaintenancePlan._base_manager.filter(pk=ctx["plan"].pk).update(
            status="active",
            ended_reason=None,
            ended_by_disposal=None,
            status_before_disposal=None,
            ended_at=None,
        )

    ctx["plan"].refresh_from_db()
    assert ctx["plan"].status == "ended"
    assert ctx["plan"].ended_by_disposal_id == disposal.pk


def test_actor_account_deletion_sets_nullable_history_fields_without_losing_facts(tmp_path):
    ctx = maintenance_context("S9DBACTOR")
    record = complete_maintenance(
        actor=ctx["equipment"],
        plan=ctx["plan"],
        scheduled_date=ctx["plan"].next_maintenance_date,
        completed_date=timezone.localdate(),
        actual_content="历史操作者删除测试",
        result="problem_found",
        problem_description="历史问题",
        idempotency_key="S9DBACTOR-complete",
    )
    problem = close_maintenance_problem(
        actor=ctx["equipment"],
        problem=record.problem,
        closure_note="历史处理说明",
        idempotency_key="S9DBACTOR-close",
    )
    with override_settings(MEDIA_ROOT=tmp_path):
        link = upload_maintenance_attachment(
            actor=ctx["equipment"],
            target=record,
            uploaded_file=SimpleUploadedFile(
                "history.jpg", JPEG_BYTES, content_type="image/jpeg"
            ),
        )
    link = void_maintenance_attachment(
        actor=ctx["equipment"], link=link, reason="历史附件作废"
    )
    record = void_maintenance_record(
        actor=ctx["equipment"],
        record=record,
        reason="历史记录作废",
        idempotency_key="S9DBACTOR-void-record",
    )
    completed_employee_id = record.completed_by_id

    get_user_model().objects.get(pk=ctx["equipment"].pk).delete()

    record.refresh_from_db()
    problem.refresh_from_db()
    link.refresh_from_db()
    link.attachment.refresh_from_db()
    ctx["equipment_employee"].refresh_from_db()
    assert record.status == "voided" and record.void_reason == "历史记录作废"
    assert record.completed_by_id == completed_employee_id
    assert record.voided_by_id is None
    assert problem.status == "closed" and problem.closure_note == "历史处理说明"
    assert problem.closed_by_id is None
    assert link.status == "voided" and link.void_reason == "历史附件作废"
    assert link.created_by_id is None and link.voided_by_id is None
    assert link.attachment.uploaded_by_id is None
    assert ctx["equipment_employee"].user_id is None
