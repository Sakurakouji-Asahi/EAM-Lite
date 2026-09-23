import html
import io
import re
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.assets.models import AttachmentLink
from apps.assets.services import upload_asset_attachment
from apps.inventory.models import InventoryTaskAsset
from apps.inventory.services import preview_inventory_assets, publish_inventory_task, scan_inventory_asset
from apps.reports.models import ExportLog
from apps.reports.queries import ReportValidationError, build_report_dataset
from tests.test_sprint3_support import make_asset, make_company, make_department, make_user, grant_scope, pdf_upload
from tests.test_sprint7_support import active_asset_context, add_target_assignment
from tests.test_sprint8_support import add_active_asset
from tests.test_sprint8_services import _draft, _scan

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def ledger():
    context, first, qr = active_asset_context("REPAIR")
    department, employee, location = add_target_assignment(context, "REPAIR2")
    second, _ = add_active_asset(context, "REPAIR2", department=department, employee=employee, location=location)
    draft = make_asset(actor=context["equipment"], company=context["company"], category=context["category"],
                       department=context["department"], employee=context["employee"], location=context["location"],
                       asset_name="待补资料草稿", serial_number="", is_maintenance_required=False)
    return context, first, second, draft, qr


def test_filtered_list_preview_excel_and_retry_keep_identical_rows(client, ledger):
    context, first, second, draft, _ = ledger
    client.force_login(context["finance"])
    filters = {"department": context["department"].pk, "location": context["location"].pk,
               "label_status": "attached", "has_serial_number": "yes", "has_attachments": "yes",
               "maintenance_required": "yes", "initialized_from": timezone.localdate().isoformat(),
               "initialized_to": timezone.localdate().isoformat()}
    response = client.get(reverse("assets:asset-list"), filters)
    assert response.context["page"].paginator.count == 1
    link = re.search(r'href="([^"]+)"[^>]*>报表与导出', response.content.decode()).group(1)
    preview = client.get(html.unescape(link))
    assert preview.status_code == 200
    assert preview.context["dataset"].row_count == 1
    assert preview.context["dataset"].rows[0]["asset_name"] == first.asset_name
    payload = {**preview.context["filters"], "idempotency_key": preview.context["idempotency_key"]}
    result = client.post(reverse("assets:asset-list-export"), payload)
    assert result.status_code == 302
    assert client.post(reverse("assets:asset-list-export"), payload)["Location"] == result["Location"]
    export = ExportLog.objects.get()
    assert export.row_count == 1
    assert export.filters_json["asset_list_filters"]["department"] == str(context["department"].pk)
    with default_storage.open(export.output_attachment.storage_key, "rb") as file:
        workbook = load_workbook(io.BytesIO(file.read()), data_only=True)
    values = [value for sheet in workbook for row in sheet.values for value in row]
    assert first.asset_name in values
    assert second.asset_name not in values and draft.asset_name not in values
    assert "筛选条件：部门" in values
    detail = client.get(result["Location"])
    assert 'asset_list_filters' not in detail.content.decode()


def test_draft_search_and_individual_view_survive_export(client, ledger):
    context, first, _, draft, _ = ledger
    client.force_login(context["equipment"])
    draft_preview = client.get(reverse("assets:asset-list-export"), {"q": draft.draft_number})
    assert draft_preview.context["dataset"].row_count == 1
    assert draft_preview.context["dataset"].rows[0]["asset_code"] == draft.draft_number
    durable = client.get(reverse("assets:asset-list-export"), {"view": "individual_durable"})
    assert durable.context["dataset"].row_count == 2
    assert all(row["asset_name"] != draft.asset_name for row in durable.context["dataset"].rows)


def test_export_rejects_other_company_bad_filters_and_employee_role(client, ledger):
    context, _, _, _, _ = ledger
    other = make_company("REPAIROTHER", active=False)
    department = make_department(other, "OTHER")
    client.force_login(context["finance"])
    for query in ({"department": department.pk}, {"has_attachments": "invalid"},
                  {"initialized_from": "2026-10-01", "initialized_to": "2026-09-01"},
                  {"unexpected": "x"}):
        assert client.get(reverse("assets:asset-list-export"), query).status_code == 400
    client.force_login(make_user("repair-employee", "employee"))
    assert client.get(reverse("assets:asset-list-export")).status_code == 403


def test_export_department_permission_and_direct_query_validation(client, ledger):
    context, first, second, _, _ = ledger
    manager = make_user("repair-manager", "department_manager")
    grant_scope(manager, context["company"], context["department"], descendants=False, assigned_by=context["admin"])
    client.force_login(manager)
    response = client.get(reverse("assets:asset-list-export"), {"department": second.department_id})
    assert response.status_code == 400
    assert first.asset_name not in response.content.decode()
    assert second.department.name not in response.content.decode()
    for key, value, name in (("employee", second.responsible_employee_id, second.responsible_employee.name), ("location", second.location_id, second.location.name)):
        response = client.get(reverse("assets:asset-list-export"), {key: value})
        assert response.status_code == 400
        assert name not in response.content.decode()
    with pytest.raises(ReportValidationError):
        build_report_dataset(actor=context["finance"], company=context["company"], report_key="fixed_asset_detail", filters={"asset_list_filters": {}})
    with pytest.raises(ReportValidationError):
        build_report_dataset(actor=context["finance"], company=context["company"], report_key="asset_ledger", filters={"asset_list_filters": {"sql": "bad"}})
    with pytest.raises(ReportValidationError):
        build_report_dataset(actor=context["finance"], company=context["company"], report_key="asset_ledger", filters={"asset_list_filters": {"department": 0}})


def test_more_filters_return_missing_labels_and_empty_serial_drafts(client, ledger):
    context, _, _, draft, _ = ledger
    client.force_login(context["equipment"])
    filters = {"label_status": "not_generated", "has_serial_number": "no", "has_attachments": "no", "maintenance_required": "no"}
    response = client.get(reverse("assets:asset-list"), filters)
    assert [asset.pk for asset in response.context["page"]] == [draft.pk]
    assert "更多筛选".encode() in response.content
    assert "最近盘点".encode() in response.content


def test_inventory_preview_is_read_only_and_publication_rechecks_population(client, ledger):
    context, first, _, _, _ = ledger
    task = _draft(context, "REPAIR-PREVIEW")
    client.force_login(context["finance"])
    for route in ("inventory:task-detail", "inventory:task-publish"):
        page = client.get(reverse(route, args=[task.pk]))
        assert page.status_code == 200
        assert "预计应盘 1 项".encode() in page.content
        assert first.asset_name.encode() in page.content
    task.refresh_from_db()
    assert task.status == "draft" and task.snapshot_at is None
    assert not InventoryTaskAsset.objects.filter(inventory_task=task).exists()
    added, _ = add_active_asset(context, "REPAIR-LATE")
    published = publish_inventory_task(actor=context["finance"], task=task)
    assert published.expected_asset_count == 2
    assert set(published.task_assets.values_list("asset_id", flat=True)) == {first.pk, added.pk}


def test_inventory_preview_permission_and_zero_count(client, ledger):
    context, _, _, _, _ = ledger
    empty_department = make_department(context["company"], "EMPTY")
    task = _draft(context, "REPAIR-EMPTY", scope_department=empty_department)
    with pytest.raises(PermissionDenied):
        preview_inventory_assets(actor=make_user("repair-preview-employee", "employee"), task=task)
    client.force_login(context["finance"])
    response = client.get(reverse("inventory:task-publish", args=[task.pk]))
    assert "预计应盘 0 项".encode() in response.content
    from copy import copy
    forged = copy(task)
    forged.scope_type = "company"
    assert preview_inventory_assets(actor=context["finance"], task=forged).count() == 0
    forged.company = make_company("REPAIR-PREVIEW-OTHER", active=False)
    with pytest.raises(PermissionDenied):
        preview_inventory_assets(actor=context["finance"], task=forged)


def test_hidden_controls_remain_submitted_without_visible_internal_labels(client, ledger):
    context, first, _, _, _ = ledger
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:lifecycle-transfer", args=[first.pk]))
    page = response.content.decode()
    for label in ("Idempotency key", "Expected status", "Expected department id", "Expected responsible employee id", "Expected location id"):
        assert label not in page
    assert 'name="expected_status"' in page and 'name="idempotency_key"' in page
    assert 'data-department-field="to_department"' in page
    assert 'data-department=' in page
    assert "pending_label → in_use" not in client.get(reverse("assets:asset-detail", args=[first.pk])).content.decode()
    invalid = client.post(reverse("assets:lifecycle-transfer", args=[first.pk]), {"reason": "校验失效页面", "expected_status": ""})
    assert "页面状态校验未通过，请刷新页面后重试。".encode() in invalid.content


def test_new_inventory_default_type_matches_scope_and_asset_options_link(client, ledger):
    context, _, _, _, _ = ledger
    client.force_login(context["finance"])
    page = client.get(reverse("inventory:task-create"))
    form = page.context["form"]
    assert form["inventory_type"].value() in (None, "department")
    assert form["scope_type"].value() == "department"
    asset_form = client.get(reverse("assets:asset-create"))
    assert 'data-department-field="department"' in asset_form.content.decode()
    assert "更多实物资料（选填）".encode() in asset_form.content


def test_attachment_filter_does_not_reveal_financial_only_files(client, ledger):
    context, _, _, draft, _ = ledger
    upload_asset_attachment(actor=context["finance"], asset=draft, uploaded_file=pdf_upload(),
                            role=AttachmentLink.Role.INVOICE, security_class=AttachmentLink.SecurityClass.A1)
    client.force_login(context["equipment"])
    query = {"q": draft.draft_number, "has_attachments": "no"}
    assert client.get(reverse("assets:asset-list"), query).context["page"].paginator.count == 1
    assert client.get(reverse("assets:asset-list-export"), query).context["dataset"].row_count == 1
    client.force_login(context["finance"])
    assert client.get(reverse("assets:asset-list-export"), query).context["dataset"].row_count == 0


def test_last_inventory_date_only_uses_visible_effective_scans(client, ledger):
    context, first, _, _, qr = ledger
    task = publish_inventory_task(actor=context["finance"], task=_draft(context, "REPAIR-SCAN"))
    scan = _scan(context, task, qr, "repair-effective-scan")
    client.force_login(context["finance"])
    page = client.get(reverse("assets:asset-list"), {"q": first.asset_code})
    assert page.context["page"][0].latest_inventory_at == scan.scanned_at
    # An unassigned warehouse user can see the asset but not this task's scans.
    client.force_login(make_user("repair-unassigned-warehouse", "warehouse"))
    page = client.get(reverse("assets:asset-list"), {"q": first.asset_code})
    assert page.context["page"][0].latest_inventory_at is None


def test_archived_ledger_export_does_not_include_current_assets(client, ledger):
    from apps.assets.lifecycle_services import archive_asset
    from tests.test_sprint7_database import _complete_disposal
    context, first, second, _, _ = ledger
    _complete_disposal(context, first, "REPAIR-ARCHIVE")
    archive_asset(actor=context["finance"], asset=first, reason="修复验收归档", idempotency_key="repair-archive")
    client.force_login(context["finance"])
    result = client.get(reverse("assets:asset-list-export"), {"record_status": "archived"})
    assert result.context["dataset"].row_count == 1
    assert result.context["dataset"].rows[0]["asset_name"] == first.asset_name
    assert second.asset_name not in result.content.decode()


def test_unchecking_all_selected_assets_does_not_reuse_old_hidden_selection(client, ledger):
    context, first, _, _, _ = ledger
    task = _draft(context, "REPAIR-UNCHECK", inventory_type="special", scope_type="selected_assets",
                  scope_department=None, selected_asset_ids=[str(first.pk)])
    client.force_login(context["finance"])
    payload = {"name": task.name, "inventory_type": "special", "scope_type": "selected_assets",
               "planned_start": task.planned_start, "planned_end": task.planned_end,
               "selected_asset_ids": str(first.pk), "selected_asset_ids_ui_present": "1"}
    response = client.post(reverse("inventory:task-edit", args=[task.pk]), payload)
    assert response.status_code == 200
    assert "selected_asset_ids" in response.context["form"].errors
    task.refresh_from_db()
    assert task.scope_definition_json["selected_asset_ids"] == [str(first.pk)]
