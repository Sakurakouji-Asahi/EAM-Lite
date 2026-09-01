from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import archive_asset
from apps.audit.models import AuditLog
from apps.offboarding.services import initiate_clearance
from apps.maintenance.services import create_maintenance_plan
from apps.reports.services import generate_report_export
from tests.test_sprint10_support import formal_asset, offboarding_context
from tests.test_sprint3_support import (
    complete_initialization,
    grant_scope,
    make_company,
    make_department,
    make_user,
)
from tests.test_sprint7_database import _complete_disposal
from tests.test_sprint7_support import active_fixed_asset_context


pytestmark = pytest.mark.django_db(transaction=True)


def _assert_no_store(response):
    assert "no-store" in response.headers.get("Cache-Control", "")


def _access_context(prefix):
    company = make_company(prefix)
    users = {
        role: make_user(f"{prefix.lower()}-{role}", role)
        for role in ("system_admin", "finance", "hr", "management", "employee")
    }
    users["finance_admin"] = make_user(
        f"{prefix.lower()}-finance-admin", "system_admin", "finance"
    )
    complete_initialization(company, users["system_admin"])
    return company, users


def _completed_export(*, company, actor, report_key, suffix):
    export_log = generate_report_export(
        actor=actor,
        company=company,
        report_key=report_key,
        filters={},
        idempotency_key=f"{suffix}-{uuid.uuid4().hex}",
    )
    with default_storage.open(export_log.output_attachment.storage_key, "rb") as stream:
        content = stream.read()
    return export_log, content


def test_navigation_and_dashboard_are_role_safe_and_no_store(client):
    _company, users = _access_context("S11HTTPNAV")

    client.force_login(users["finance"])
    finance = client.get(reverse("home"))
    assert finance.status_code == 200
    _assert_no_store(finance)
    assert "financial" in finance.context["dashboard"]
    assert "CNY" in finance.content.decode()
    assert reverse("audit:log-list") in finance.content.decode()
    assert reverse("reports:tplus-export") in finance.content.decode()

    client.force_login(users["system_admin"])
    system_admin = client.get(reverse("home"))
    assert system_admin.status_code == 200
    _assert_no_store(system_admin)
    assert "physical" in system_admin.context["dashboard"]
    assert "financial" not in system_admin.context["dashboard"]
    assert "CNY" not in system_admin.content.decode()

    client.force_login(users["hr"])
    hr = client.get(reverse("home"))
    assert hr.status_code == 200
    _assert_no_store(hr)
    assert set(hr.context["dashboard"]) == {"data_snapshot_at", "pending"}
    assert set(hr.context["dashboard"]["pending"]) == {"offboarding_unresolved"}
    assert "CNY" not in hr.content.decode()
    assert hr.context["masterdata_nav"]["system_setting"] is False
    assert hr.context["audit_nav"]["can_view"] is True
    assert reverse("audit:log-list") in hr.content.decode()
    assert reverse("reports:tplus-export") not in hr.content.decode()

    client.force_login(users["employee"])
    employee = client.get(reverse("home"))
    assert employee.status_code == 200
    _assert_no_store(employee)
    assert "physical" in employee.context["dashboard"]
    assert "financial" not in employee.context["dashboard"]
    assert "CNY" not in employee.content.decode()
    assert reverse("audit:log-list") not in employee.content.decode()


def test_dashboard_physical_drilldowns_match_card_and_chart_scopes(client):
    context = offboarding_context("S11DASHDRILL")
    attached, _attached_qr = formal_asset(context, "S11DASHDRILL-A")
    pending_label, _pending_qr = formal_asset(
        context, "S11DASHDRILL-P", activate=False
    )
    overdue_asset, _overdue_qr = formal_asset(context, "S11DASHDRILL-O")
    for asset in (attached, pending_label, overdue_asset):
        asset.is_maintenance_required = True
        asset.save(update_fields={"is_maintenance_required"})
    today = timezone.localdate()
    create_maintenance_plan(
        actor=context["equipment"], company=context["company"], asset=attached,
        name="即将保养", cycle_value=1, cycle_unit="month",
        responsible_employee=context["employee"], advance_notice_days=3,
        standard_content="检查", first_due_date=today + timedelta(days=1),
    )
    create_maintenance_plan(
        actor=context["equipment"], company=context["company"], asset=overdue_asset,
        name="逾期保养", cycle_value=1, cycle_unit="month",
        responsible_employee=context["employee"], advance_notice_days=3,
        standard_content="检查", first_due_date=today - timedelta(days=1),
    )

    client.force_login(context["equipment"])
    home = client.get(reverse("home"))
    dashboard = home.context["dashboard"]
    assert dashboard["physical"]["asset_total"] == 3
    assert dashboard["pending"]["pending_label"] == 1
    assert dashboard["pending"]["maintenance_upcoming"] == 1
    assert dashboard["pending"]["maintenance_overdue"] == 1
    home_html = home.content.decode()
    for expected in (
        "asset_scope=managed", "label_scope=not_attached",
        "maintenance_due_scope=upcoming", "maintenance_due_scope=overdue",
        f"department={context['department'].pk}",
        f"category={context['category'].pk}",
    ):
        assert expected in home_html

    cases = (
        ({"report_type": "asset_ledger", "asset_scope": "managed",
          "as_of_date": today, "include_disposed": "false"}, 3),
        ({"report_type": "asset_ledger", "asset_scope": "managed",
          "label_scope": "not_attached", "as_of_date": today,
          "include_disposed": "false"}, 1),
        ({"report_type": "maintenance_due", "maintenance_due_scope": "upcoming",
          "as_of_date": today}, 1),
        ({"report_type": "maintenance_due", "maintenance_due_scope": "overdue",
          "as_of_date": today}, 1),
        ({"report_type": "department_assets", "asset_scope": "managed",
          "department": context["department"].pk, "as_of_date": today,
          "include_disposed": "false"}, 3),
        ({"report_type": "asset_ledger", "asset_scope": "managed",
          "category": context["category"].pk, "as_of_date": today,
          "include_disposed": "false"}, 3),
    )
    for query, expected_count in cases:
        response = client.get(reverse("reports:report-center"), query)
        assert response.status_code == 200
        assert response.context["dataset"].row_count == expected_count

    invalid = client.get(
        reverse("reports:report-center"),
        {"report_type": "asset_ledger", "asset_scope": "all_company"},
    )
    assert invalid.status_code == 400
    wrong_report = client.get(
        reverse("reports:report-center"),
        {"report_type": "asset_ledger", "maintenance_due_scope": "overdue"},
    )
    assert wrong_report.status_code == 400


def test_dashboard_disposed_drilldown_includes_active_and_archived_records(client):
    context = offboarding_context("S11DASHDISPOSED")
    active_disposed, _ = formal_asset(context, "S11DASHDISPOSED-A")
    archived_disposed, _ = formal_asset(context, "S11DASHDISPOSED-R")
    _complete_disposal(context, active_disposed, "S11DASHDISPOSED-A")
    _complete_disposal(context, archived_disposed, "S11DASHDISPOSED-R")
    archived_disposed.refresh_from_db()
    archive_asset(
        actor=context["finance"], asset=archived_disposed,
        reason="Dashboard 归档报废钻取验收",
        idempotency_key="S11DASHDISPOSED-archive",
    )

    client.force_login(context["equipment"])
    home = client.get(reverse("home"))
    assert home.context["dashboard"]["physical"]["disposed"] == 2
    assert "asset_status=disposed" in home.content.decode()
    assert "include_disposed=true" in home.content.decode()

    drilldown = client.get(
        reverse("reports:report-center"),
        {
            "report_type": "asset_ledger",
            "asset_status": "disposed",
            "as_of_date": timezone.localdate(),
            "include_disposed": "true",
        },
    )
    assert drilldown.status_code == 200
    assert drilldown.context["dataset"].row_count == 2


def test_unknown_parameters_page_size_and_tplus_permission_fail_closed(client):
    _company, users = _access_context("S11HTTPINPUT")

    client.force_login(users["finance"])
    responses = (
        client.get(reverse("reports:report-center"), {"sql": "1"}),
        client.post(
            reverse("reports:report-export"),
            {"report_type": "asset_ledger", "sql": "1"},
        ),
        client.get(reverse("reports:tplus-export"), {"sql": "1"}),
        client.get(reverse("audit:log-list"), {"sql": "1"}),
        client.get(reverse("audit:log-list"), {"page_size": "101"}),
    )
    for response in responses:
        assert response.status_code == 400
        _assert_no_store(response)
    allowed_tplus = client.get(reverse("reports:tplus-export"))
    assert allowed_tplus.status_code == 200
    _assert_no_store(allowed_tplus)

    client.force_login(users["hr"])
    denied_tplus = client.get(reverse("reports:tplus-export"), {"sql": "1"})
    assert denied_tplus.status_code == 403
    _assert_no_store(denied_tplus)

    client.force_login(users["employee"])
    denied_audit = client.get(reverse("audit:log-list"), {"sql": "1"})
    assert denied_audit.status_code == 403
    _assert_no_store(denied_audit)


@pytest.mark.parametrize(
    ("role", "fixed_category_visible"),
    (
        ("finance", True),
        ("system_admin", False),
        ("equipment", False),
        ("warehouse", False),
        ("department_manager", False),
        ("employee", False),
    ),
)
def test_report_form_hides_accounting_category_from_nonfinance(
    client, role, fixed_category_visible
):
    _company, users = _access_context(f"S11HTTPF1{role[:3].upper()}")
    actor = users.get(role) or make_user(f"s11-http-f1-{role}", role)
    client.force_login(actor)

    response = client.get(reverse("reports:report-center"))

    assert response.status_code == 200
    assert (
        "fixed_asset_category" in response.context["form"].fields
    ) is fixed_category_visible
    assert (
        'name="fixed_asset_category"' in response.content.decode()
    ) is fixed_category_visible


def test_report_and_tplus_masterdata_filters_are_named_selects(client):
    context, asset, _qr, _profile, _policy = active_fixed_asset_context(
        "S11HTTPSELECT"
    )
    client.force_login(context["finance"])

    report = client.get(reverse("reports:report-center"))
    assert report.status_code == 200
    form = report.context["form"]
    for field_name in (
        "department",
        "category",
        "fixed_asset_category",
        "responsible_employee",
    ):
        assert form.fields[field_name].widget.input_type == "select"
    report_html = report.content.decode()
    for label in (
        context["department"].name,
        context["category"].name,
        context["employee"].name,
        asset.finance.fixed_asset_category.name,
    ):
        assert label in report_html

    filtered = client.get(
        reverse("reports:report-center"),
        {
            "report_type": "fixed_asset_detail",
            "department": context["department"].pk,
            "category": context["category"].pk,
            "fixed_asset_category": asset.finance.fixed_asset_category_id,
            "responsible_employee": context["employee"].pk,
        },
    )
    assert filtered.status_code == 200
    assert filtered.context["dataset"].row_count == 1
    display_filters = dict(filtered.context["display_filters"])
    assert display_filters["部门"] == str(context["department"])
    assert display_filters["实物分类"] == str(context["category"])
    assert display_filters["固定资产类别"] == str(
        asset.finance.fixed_asset_category
    )
    assert display_filters["责任人"] == str(context["employee"])

    tplus = client.get(reverse("reports:tplus-export"))
    assert tplus.status_code == 200
    for field_name in ("department", "category", "fixed_asset_category"):
        assert tplus.context["form"].fields[field_name].widget.input_type == "select"
    assert context["department"].name in tplus.content.decode()


def test_audit_http_scope_exact_registry_and_recursive_redaction(client):
    company, users = _access_context("S11HTTPAUDIT")
    equipment = make_user("s11-http-audit-equipment", "equipment")
    finance_log = AuditLog.objects.create(
        company=company,
        user=equipment,
        action="finance.changed",
        object_type="AssetFinance",
        object_id="FINANCE-WHITELIST",
        old_data_json={},
        new_data_json={
            "original_cost": "987654.32",
            "safe_note": "FINANCE-VISIBLE",
            "nested": {
                "secret_key": "SECRET-VALUE",
                "file_contents": "FILE-VALUE",
                "items": [
                    {"api_token": "TOKEN-VALUE"},
                    {
                        "security_class": "A1",
                        "original_cost": "A1-AMOUNT",
                    },
                ],
            },
        },
    )
    hr_log = AuditLog.objects.create(
        company=company,
        user=equipment,
        action="employee.changed",
        object_type="Employee",
        object_id="HR-WHITELIST",
        old_data_json={},
        new_data_json={"name": "HR-VISIBLE", "cookie": "COOKIE-VALUE"},
    )
    profile_log = AuditLog.objects.create(
        company=company,
        user=equipment,
        action="profile.changed",
        object_type="AssetDepreciationProfile",
        object_id="PROFILE-WHITELIST",
        old_data_json={},
        new_data_json={"original_cost": "123.45"},
    )
    AuditLog.objects.create(
        company=company,
        user=users["finance"],
        action="asset.changed",
        object_type="Asset",
        object_id="FINANCE-OWN",
        old_data_json={},
        new_data_json={"safe_note": "OWN-VISIBLE"},
    )

    client.force_login(users["finance"])
    finance = client.get(reverse("audit:log-list"))
    finance_html = finance.content.decode()
    assert finance.status_code == 200
    _assert_no_store(finance)
    assert finance_log.object_id in finance_html
    assert profile_log.object_id in finance_html
    assert "FINANCE-OWN" in finance_html
    assert hr_log.object_id not in finance_html
    assert "987654.32" in finance_html
    assert "财务资料变更" in finance_html
    assert "资产财务资料" in finance_html
    assert "原值" in finance_html
    for secret in ("SECRET-VALUE", "FILE-VALUE", "TOKEN-VALUE", "A1-AMOUNT"):
        assert secret not in finance_html

    exact_profile = client.get(
        reverse("audit:log-list"), {"object_type": "AssetDepreciationProfile"}
    )
    assert exact_profile.status_code == 200
    assert exact_profile.context["page_obj"].paginator.count == 1
    wrong_action = client.get(
        reverse("audit:log-list"), {"action": "finance.changed.extra"}
    )
    assert wrong_action.status_code == 200
    assert wrong_action.context["page_obj"].paginator.count == 0
    unknown_type = client.get(
        reverse("audit:log-list"), {"object_type": "AssetFinanceAnything"}
    )
    assert unknown_type.status_code == 400
    _assert_no_store(unknown_type)

    client.force_login(users["system_admin"])
    system_admin = client.get(reverse("audit:log-list"))
    admin_html = system_admin.content.decode()
    assert system_admin.status_code == 200
    _assert_no_store(system_admin)
    assert finance_log.object_id in admin_html
    assert hr_log.object_id in admin_html
    assert "987654.32" not in admin_html
    assert "FINANCE-VISIBLE" not in admin_html

    client.force_login(users["finance_admin"])
    combined = client.get(reverse("audit:log-list"))
    combined_html = combined.content.decode()
    assert combined.status_code == 200
    _assert_no_store(combined)
    assert "987654.32" in combined_html
    assert "FINANCE-VISIBLE" in combined_html
    for secret in ("SECRET-VALUE", "FILE-VALUE", "TOKEN-VALUE", "A1-AMOUNT"):
        assert secret not in combined_html

    client.force_login(users["hr"])
    hr = client.get(reverse("audit:log-list"))
    hr_html = hr.content.decode()
    assert hr.status_code == 200
    _assert_no_store(hr)
    assert hr_log.object_id in hr_html
    assert "HR-VISIBLE" in hr_html
    assert "COOKIE-VALUE" not in hr_html
    assert finance_log.object_id not in hr_html


def test_export_detail_and_download_recheck_department_scope(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    company, users = _access_context("S11HTTPEXPORT")
    department = make_department(company, "S11HTTPEXPORT-D")
    manager = make_user("s11-http-export-manager", "department_manager")
    other_manager = make_user("s11-http-export-other", "department_manager")
    scope = grant_scope(
        manager,
        company,
        department,
        descendants=False,
        assigned_by=users["system_admin"],
    )
    grant_scope(
        other_manager,
        company,
        department,
        descendants=False,
        assigned_by=users["system_admin"],
    )
    export_log, expected = _completed_export(
        company=company,
        actor=manager,
        report_key="asset_ledger",
        suffix="department-scope",
    )
    assert export_log.filters_json["_authorized_department_ids"] == [department.pk]
    detail_url = reverse("reports:export-detail", args=[export_log.pk])
    download_url = reverse("reports:export-download", args=[export_log.pk])

    client.force_login(other_manager)
    guessed_detail = client.get(detail_url)
    guessed_download = client.get(download_url)
    assert guessed_detail.status_code == 403
    assert guessed_download.status_code == 403
    _assert_no_store(guessed_detail)
    _assert_no_store(guessed_download)

    client.force_login(manager)
    detail = client.get(detail_url)
    assert detail.status_code == 200
    _assert_no_store(detail)
    assert "_authorized_department_ids" not in detail.content.decode()
    download = client.get(download_url)
    assert download.status_code == 200
    _assert_no_store(download)
    assert b"".join(download.streaming_content) == expected

    scope.is_active = False
    scope.revoked_at = timezone.now()
    scope.revoked_by = users["system_admin"]
    scope.save(update_fields=("is_active", "revoked_at", "revoked_by"))
    revoked_detail = client.get(detail_url)
    revoked_download = client.get(download_url)
    assert revoked_detail.status_code == 403
    assert revoked_download.status_code == 403
    _assert_no_store(revoked_detail)
    _assert_no_store(revoked_download)


def test_warehouse_export_excludes_unrelated_pending_clearance_item(
    client, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    context = offboarding_context("S11HTTPWH")
    asset, _qr = formal_asset(context, "S11HTTPWH-A")
    clearance = initiate_clearance(
        actor=context["hr"],
        employee=context["employee"],
        idempotency_key="S11HTTPWH-init",
    )
    item = clearance.items.get(asset=asset)
    export_log, expected = _completed_export(
        company=context["company"],
        actor=context["warehouse"],
        report_key="offboarding_unresolved",
        suffix="warehouse-scope",
    )
    assert item.resolution == "pending"
    assert export_log.filters_json["_authorized_clearance_item_ids"] == []
    assert export_log.row_count == 0
    detail_url = reverse("reports:export-detail", args=[export_log.pk])
    download_url = reverse("reports:export-download", args=[export_log.pk])

    client.force_login(context["warehouse"])
    assert client.get(detail_url).status_code == 200
    initial_download = client.get(download_url)
    assert initial_download.status_code == 200
    _assert_no_store(initial_download)
    assert b"".join(initial_download.streaming_content) == expected
