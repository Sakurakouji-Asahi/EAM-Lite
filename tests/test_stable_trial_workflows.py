"""Stable trial business checks; all assets and accounts here are constructed."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.urls import reverse

from apps.assets.draft_assignment import (
    AUDIT_ACTION, confirm_draft_assignment, preview_draft_assignment,
)
from apps.assets.models import Asset, AssetRegistration
from apps.assets.registration import register_asset
from apps.assets.services import create_asset_draft, update_asset_draft
from apps.audit.models import AuditLog
from apps.finance.models import AssetFinance
from apps.finance.readiness import missing_finance_base_fields
from apps.masterdata.models import IssuedCode
from apps.reports.queries import build_report_dataset
from tests.test_asset_workflow_improvements import business_time
from tests.test_sprint3_support import (
    grant_scope, make_company, make_department, make_employee, make_location, make_user,
)
from tests.test_unified_asset_identity import context, physical_data, registered

pytestmark = pytest.mark.django_db(transaction=True)


def draft(ctx, name="待补资料", **overrides):
    return create_asset_draft(actor=ctx["equipment"], company=ctx["company"],
        data=physical_data(ctx, asset_name=name, **overrides))


def preview(ctx, assets, *, data=None, actor=None):
    return preview_draft_assignment(actor=actor or ctx["equipment"], company=ctx["company"],
        asset_ids=[item.pk for item in assets], data=data or {"responsible_employee": ctx["employee"]},
        reason="按交接清单核对草稿责任资料")


def confirm(ctx, prepared, *, actor=None):
    return confirm_draft_assignment(actor=actor or ctx["equipment"], company=ctx["company"], token=prepared["token"])


def test_registration_date_list_and_export_distinguish_unregistered_drafts(client):
    with business_time(10):
        ctx = context.__wrapped__()
        asset = draft(ctx, "早建草稿晚建档")
    with business_time(17):
        asset = register_asset(actor=ctx["equipment"], asset=asset, idempotency_key="trial-date")
        unregistered = draft(ctx, "当天草稿尚未建档")
        client.force_login(ctx["equipment"])
        url = reverse("assets:asset-list")
        response = client.get(url, {"initialized_from": "2026-09-17", "initialized_to": "2026-09-17"})
        assert response.status_code == 200
        assert [row.pk for row in response.context["page"]] == [asset.pk]
        exported = build_report_dataset(actor=ctx["equipment"], company=ctx["company"], report_key="asset_ledger",
            filters={"asset_list_filters": response.context["filters"]})
        assert len(exported.rows) == 1 and exported.rows[0]["asset_code"] == asset.asset_code
        earlier = client.get(url, {"initialized_from": "2026-09-10", "initialized_to": "2026-09-10"})
        assert earlier.context["page"].paginator.count == 0
        created = client.get(url, {"created_from": "2026-09-17", "created_to": "2026-09-17"})
        assert [row.pk for row in created.context["page"]] == [unregistered.pk]
        assert not AssetRegistration.objects.filter(asset=unregistered).exists()
        client.force_login(ctx["finance"])
        pending = client.get(reverse("finance:pending-list"), {"q": asset.asset_code})
        assert [row.pk for row in pending.context["assets"]] == [asset.pk]
        assert "2026-09-17 12:00" in pending.content.decode()


@pytest.mark.parametrize("query", [
    {"created_from": "2026-09-20", "created_to": "2026-09-10"},
    {"initialized_from": "not-a-date"},
])
def test_invalid_date_filters_are_visible_and_never_return_all_rows(context, client, query):
    registered(context)
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:asset-list"), query)
    assert response.status_code == 200
    assert response.context["filter_errors"] and response.context["page"].paginator.count == 0


def test_finance_pending_search_filters_and_zero_cost_hint(context, client):
    first = registered(context, "find-a", asset_name="试用挤出机")
    second = registered(context, "find-b", asset_name="试用检测仪")
    AssetFinance.objects.create(company=context["company"], asset=second,
        accounting_treatment="controlled_non_fixed", original_cost=Decimal("0.00"))
    client.force_login(context["finance"])
    url = reverse("finance:pending-list")
    found = client.get(url, {"q": "挤出机", "department": context["department"].pk})
    assert [row.pk for row in found.context["assets"]] == [first.pk]
    assert found.context["assets"][0].finance_missing_fields == ["会计认定", "原值"]
    saved = client.get(url, {"data_status": "saved"})
    assert [row.pk for row in saved.context["assets"]] == [second.pk]
    assert saved.context["assets"][0].finance_missing_fields == []
    untouched = client.get(url, {"data_status": "not_entered"})
    assert [row.pk for row in untouched.context["assets"]] == [first.pk]
    assert str(context["department"].pk) in found.context["pagination_query"]
    invalid = client.get(url, {"data_status": "invented"})
    assert invalid.context["filter_form"].errors and invalid.context["page_obj"].paginator.count == 0


def test_finance_pending_rejects_other_company_department_and_nonfinance_user(context, client):
    other = make_company("TRIAL-OTHER", active=False)
    department = make_department(other)
    registered(context)
    client.force_login(context["finance"])
    response = client.get(reverse("finance:pending-list"), {"department": department.pk})
    assert response.context["filter_form"].errors and not response.context["assets"]
    client.force_login(context["equipment"])
    assert client.get(reverse("finance:pending-list")).status_code == 403


def test_missing_finance_hints_do_not_require_fixed_fields_for_nonfixed():
    finance = SimpleNamespace(accounting_treatment="controlled_non_fixed", original_cost=Decimal("0.00"))
    assert missing_finance_base_fields(SimpleNamespace(finance=finance)) == []
    finance = SimpleNamespace(accounting_treatment="fixed_asset", original_cost=Decimal("12500.00"),
        fixed_asset_category_id=None, capitalization_date=None)
    assert missing_finance_base_fields(SimpleNamespace(finance=finance, commissioning_date=None)) == [
        "固定资产会计类别", "资本化日期", "达到可使用状态日期",
    ]


def test_bulk_assignment_preview_no_write_then_confirm_replay_audited(context):
    first, second = draft(context, "一号", responsible_employee=None), draft(context, "二号", responsible_employee=None)
    before_audit = AuditLog.objects.count()
    prepared = preview(context, [first, second])
    assert prepared["error_count"] == 0 and prepared["token"]
    assert AuditLog.objects.count() == before_audit
    assert Asset.objects.filter(responsible_employee__isnull=True).count() == 2
    result = confirm(context, prepared)
    assert result["count"] == 2 and not result["replayed"]
    after_audit = AuditLog.objects.count()
    assert confirm(context, prepared)["replayed"]
    assert AuditLog.objects.count() == after_audit
    assert Asset.objects.filter(asset_status="draft", responsible_employee=context["employee"]).count() == 2
    assert AuditLog.objects.filter(action=AUDIT_ACTION).count() == 1
    assert AuditLog.objects.get(action=AUDIT_ACTION).new_data_json["reason"] == "按交接清单核对草稿责任资料"
    assert not IssuedCode.objects.exists() and not AssetFinance.objects.exists()


def test_changed_draft_stops_entire_assignment_without_overwriting(context):
    first, second = draft(context, "一号", responsible_employee=None), draft(context, "二号", responsible_employee=None)
    prepared = preview(context, [first, second])
    update_asset_draft(actor=context["equipment"], asset=second, data={"asset_name": "同事已修正名称"})
    audit_count = AuditLog.objects.count()
    with pytest.raises(ValidationError, match="发生变化"):
        confirm(context, prepared)
    assert Asset.objects.filter(responsible_employee__isnull=True).count() == 2
    second.refresh_from_db()
    assert second.asset_name == "同事已修正名称"
    assert AuditLog.objects.count() == audit_count


def test_late_save_failure_rolls_back_earlier_asset_and_audit(context):
    from apps.assets import draft_assignment

    first, second = draft(context, "一号", responsible_employee=None), draft(context, "二号", responsible_employee=None)
    prepared = preview(context, [first, second])
    original = draft_assignment.update_asset_draft
    count = 0
    def fail_second(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ValidationError("第二件资料已不可用")
        return original(**kwargs)
    before = AuditLog.objects.count()
    with patch.object(draft_assignment, "update_asset_draft", side_effect=fail_second):
        with pytest.raises(ValidationError, match="第二件"):
            confirm(context, prepared)
    assert Asset.objects.filter(responsible_employee__isnull=True).count() == 2
    assert AuditLog.objects.count() == before


def test_assignment_department_employee_mismatch_not_silently_cleared(context):
    asset = draft(context)
    other = make_department(context["company"], "D-OTHER")
    prepared = preview(context, [asset], data={"department": other})
    assert prepared["error_count"] == 1 and not prepared["token"]
    assert "责任人" in prepared["rows"][0]["error"]
    asset.refresh_from_db()
    assert asset.department_id == context["department"].pk and asset.responsible_employee_id == context["employee"].pk


def test_assignment_rechecks_scope_and_target_department(context):
    asset = draft(context)
    manager = make_user("trial-manager", "department_manager")
    grant_scope(manager, context["company"], context["department"])
    other = make_department(context["company"], "D-OTHER")
    employee = make_employee(context["company"], other, "E-OTHER")
    with pytest.raises(PermissionDenied, match="目标部门"):
        preview(context, [asset], actor=manager, data={"department": other, "responsible_employee": employee})
    outsider = make_user("trial-outsider", "employee")
    with pytest.raises(PermissionDenied):
        preview(context, [asset], actor=outsider)
    prepared = preview(context, [asset])
    with pytest.raises(ValidationError, match="预览"):
        confirm(context, prepared, actor=manager)
    with pytest.raises(ValidationError, match="预览"):
        confirm_draft_assignment(actor=context["equipment"], company=context["company"], token=prepared["token"]+"x")


def test_registered_asset_is_rejected_by_bulk_assignment(context):
    asset = registered(context)
    with pytest.raises(PermissionDenied):
        preview(context, [asset])
    asset.refresh_from_db()
    assert asset.asset_code and asset.asset_status == "pending_label"


def test_bulk_and_single_draft_options_respect_combined_admin_manager_scope(context):
    from apps.assets.bulk_forms import BulkDraftAssignmentForm
    from apps.assets.forms import AssetDraftForm

    actor = make_user("trial-scoped-admin", "system_admin", "department_manager")
    grant_scope(actor, context["company"], context["department"])
    other = make_department(context["company"], "D-OUTSIDE")
    for form in (BulkDraftAssignmentForm(actor=actor, company=context["company"]),
                 AssetDraftForm(actor=actor, company=context["company"])):
        assert set(form.fields["department"].queryset.values_list("pk", flat=True)) == {context["department"].pk}
        assert other.pk not in form.fields["department"].queryset.values_list("pk", flat=True)


def test_registration_time_display_does_not_expand_hr_summary(context, client):
    asset = registered(context)
    client.force_login(make_user("trial-hr", "hr"))
    response = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert response.status_code == 200 and "正式建档时间" not in response.content.decode()
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert response.status_code == 200 and "正式建档时间" in response.content.decode()


def test_bulk_assignment_browser_http_then_registration(context, client):
    asset = draft(context, responsible_employee=None)
    client.force_login(context["equipment"])
    url = reverse("assets:bulk-registration")
    response = client.post(url, {"action": "assignment_preview", "assets": [str(asset.pk)],
        "responsible_employee": context["employee"].pk, "reason": "按实物清单补齐责任人"})
    assert response.status_code == 200 and response.context["assignment_preview"]["token"]
    saved = client.post(url, {"action": "assignment_confirm", "token": response.context["assignment_preview"]["token"]})
    assert saved.status_code == 200 and saved.context["assignment_result"]["count"] == 1
    checked = client.post(url, {"action": "preview", "assets": [str(asset.pk)]})
    assert checked.status_code == 200 and checked.context["preview"]["ready_count"] == 1
    result = client.post(url, {"action": "confirm", "token": checked.context["preview"]["token"]})
    assert result.context["result"]["success_count"] == 1
    assert result.context["cleared_asset_ids"] == [str(asset.pk)]


def test_concurrent_assignment_replay_has_one_receipt(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL locking required")
    asset = draft(context, responsible_employee=None)
    prepared = preview(context, [asset])
    actor_id, company = context["equipment"].pk, context["company"]
    def worker():
        close_old_connections()
        try:
            actor = get_user_model().objects.get(pk=actor_id)
            return confirm_draft_assignment(actor=actor, company=company, token=prepared["token"])
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: worker(), range(2)))
    assert sorted(row["replayed"] for row in results) == [False, True]
    assert AuditLog.objects.filter(action=AUDIT_ACTION).count() == 1
    assert AuditLog.objects.filter(action="asset_draft_update", object_id=str(asset.pk)).count() == 1
