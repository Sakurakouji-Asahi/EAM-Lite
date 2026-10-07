"""Business lookup respects both audit visibility and current object scope."""
import uuid

import pytest
from django.urls import reverse

from apps.audit.business_search import filter_business_audit
from apps.audit.models import AuditLog
from apps.audit.permissions import scoped_audit_logs
from apps.audit.services import write_audit_log
from apps.maintenance.services import close_maintenance_problem
from apps.offboarding.services import initiate_clearance
from tests.test_sprint3_support import make_company, make_department, make_employee, make_user
from tests.test_sprint8_services import _published, _scan
from tests.test_sprint8_support import inventory_context
from tests.test_sprint9_services import _complete
from tests.test_sprint9_support import maintenance_context
from tests.test_sprint10_support import formal_asset, offboarding_context

pytestmark = pytest.mark.django_db


def lookup(company, actor, query):
    return filter_business_audit(scoped_audit_logs(actor, company), actor=actor,
                                 company=company, query=query)


def test_employee_number_and_name_find_person_and_clearance_without_payload_search(client):
    ctx = offboarding_context("AUDITPERSON")
    formal_asset(ctx, "AUDITPERSON-A")
    employee_log = write_audit_log(company=ctx["company"], user=ctx["hr"], action="update",
        object_type="Employee", object_id=ctx["employee"].pk,
        new_data={"name": ctx["employee"].name, "mobile": "PRIVATE-PHONE-LOOKUP"})
    clearance = initiate_clearance(actor=ctx["hr"], employee=ctx["employee"],
                                  idempotency_key="audit-person-clearance")
    # Creation records the parent snapshot. Add a labelled child event fixture
    # to verify lookup of later item-level history as well.
    write_audit_log(company=ctx["company"], user=ctx["hr"], action="update",
        object_type="EmployeeAssetClearanceItem", object_id=clearance.items.get().pk,
        new_data={"remark": "测试用清退明细历史"})
    # Search includes leaving or disabled historical personnel as the current
    # scoped directory does, without asking the raw JSON for arbitrary text.
    ctx["employee"].refresh_from_db()
    unrelated = AuditLog.objects.create(company=ctx["company"], user=ctx["hr"], action="update",
        object_type="Department", object_id=str(ctx["employee"].pk),
        new_data_json={"remark": ctx["employee"].name})
    client.force_login(ctx["hr"])
    before = list(AuditLog.objects.order_by("pk").values())
    for query in (ctx["employee"].employee_no.lower(), ctx["employee"].name):
        response = client.get(reverse("audit:log-list"), {"q": query})
        assert response.status_code == 200
        rows = response.context["page_obj"].object_list
        assert employee_log.pk in {row["id"] for row in rows}
        assert any(row["object_type"] == "EmployeeAssetClearance"
                   and row["object_id"] == str(clearance.pk) for row in rows)
        assert any(row["object_type"] == "EmployeeAssetClearanceItem" for row in rows)
        assert unrelated.pk not in {row["id"] for row in rows}
    assert not lookup(ctx["company"], ctx["hr"], "PRIVATE-PHONE-LOOKUP").exists()
    assert list(AuditLog.objects.order_by("pk").values()) == before


def test_task_code_and_name_find_scan_evidence_and_keep_exact_and_role_filters(client):
    ctx, asset, qr = inventory_context("AUDITTASK")
    task = _published(ctx, "AUDITTASK-T")
    scan = _scan(ctx, task, qr, "audit-task-scan", actor=ctx["finance"])
    for query in (task.task_code.lower(), task.name):
        found = lookup(ctx["company"], ctx["finance"], query)
        assert found.filter(object_type="InventoryTask", object_id=str(task.pk)).exists()
        assert found.filter(object_type="InventoryScan", object_id=str(scan.pk)).exists()
    client.force_login(ctx["finance"])
    page = client.get(reverse("audit:log-list"), {"q": task.task_code,
        "object_type": "InventoryScan", "object_id": str(scan.pk)})
    assert page.status_code == 200
    assert {row["object_id"] for row in page.context["page_obj"].object_list} == {str(scan.pk)}
    # HR can read own audit rows but has no task scope; names cannot expand it.
    hr = make_user("audit-task-hr", "hr")
    AuditLog.objects.create(company=ctx["company"], user=hr, action="update",
        object_type="InventoryTask", object_id=str(task.pk))
    assert not lookup(ctx["company"], hr, task.name).exists()
    # Direct Asset UUIDs and JSON asset references must both resolve on SQLite
    # as well as PostgreSQL, without relying on the encoded UUID column format.
    asset_log = AuditLog.objects.create(company=ctx["company"], user=ctx["finance"], action="update",
        object_type="Asset", object_id=str(asset.pk))
    related_log = AuditLog.objects.create(company=ctx["company"], user=ctx["finance"], action="update",
        object_type="AssetMovement", object_id=str(uuid.uuid4()), new_data_json={"asset_id": str(asset.pk)})
    assert {asset_log.pk, related_log.pk} <= set(lookup(ctx["company"], ctx["finance"], asset.asset_code).values_list("pk", flat=True))


def test_plan_name_finds_completion_and_problem_while_preserving_audit_role_scope():
    ctx = maintenance_context("AUDITPLAN")
    record = _complete(ctx, "audit-plan-complete", result="problem_found",
                       problem_description="计划查询测试问题")
    close_maintenance_problem(actor=ctx["equipment"], problem=record.problem,
        closure_note="已复查测试问题", idempotency_key="audit-plan-problem-close")
    actor = make_user("audit-plan-reader", "system_admin", "finance")
    found = lookup(ctx["company"], actor, ctx["plan"].name)
    assert found.filter(object_type="MaintenancePlan", object_id=str(ctx["plan"].pk)).exists()
    assert found.filter(object_type="MaintenanceRecord", object_id=str(record.pk)).exists()
    assert found.filter(object_type="MaintenanceProblem", object_id=str(record.problem.pk)).exists()
    # Finance can resolve a plan but cannot read other users' maintenance audit
    # rows under the existing audit read matrix.
    assert not lookup(ctx["company"], ctx["finance"], ctx["plan"].name).exists()


def test_business_names_cannot_match_other_company_objects_or_same_integer_ids():
    company = make_company("AUDITLOOKUP")
    actor = make_user("audit-lookup-hr", "hr")
    department = make_department(company)
    employee = make_employee(company, department, "MATCH-LOCAL", active=False)
    foreign = make_company("AUDITFOREIGN", active=False)
    other = make_employee(foreign, make_department(foreign), "MATCH-FOREIGN")
    visible = AuditLog.objects.create(company=company, user=actor, action="update",
        object_type="Employee", object_id=str(employee.pk))
    wrong_type = AuditLog.objects.create(company=company, user=actor, action="update",
        object_type="Department", object_id=str(employee.pk))
    wrong_company = AuditLog.objects.create(company=foreign, user=actor, action="update",
        object_type="Employee", object_id=str(other.pk))
    misleading_reference = AuditLog.objects.create(company=company, user=actor, action="update",
        object_type="Employee", object_id=str(other.pk))
    assert set(lookup(company, actor, "MATCH-LOCAL").values_list("pk", flat=True)) == {visible.pk}
    assert not lookup(company, actor, "MATCH-FOREIGN").exists()
    assert not lookup(company, actor, "MATCH").filter(pk__in=[wrong_type.pk, wrong_company.pk, misleading_reference.pk]).exists()
