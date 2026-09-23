from datetime import date
from decimal import Decimal
import io
import json

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation
from openpyxl import load_workbook

from apps.assets.models import Asset, AssetIdentity
from apps.assets.registration import register_asset
from apps.assets.custody_services import return_custody_asset
from apps.core.middleware import RequestRoleCacheMiddleware
from apps.finance.services import confirm_asset_finance, create_fixed_asset_category
from apps.imports.services import build_template_workbook, get_template_definition, upload_and_validate_import, confirm_import_batch
from apps.inventory.forms import InventoryTaskForm
from apps.maintenance.forms import MaintenancePlanForm
from apps.masterdata.permissions import role_names_for
from apps.masterdata.models import Employee
from apps.reports.supply_queries import build_supply_dashboard
from apps.supplies.forms import SupplyCountTaskForm
from tests.test_sprint3_support import make_user, make_department, make_employee, make_location, grant_scope
from tests.test_sprint5_support import physical_row, sprint5_context, XLSX_MIME
from tests.test_unified_asset_identity import context, registered

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def isolated_import_files(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


def upload(company, rows, version=None):
    definition = get_template_definition("asset_initialization", company=company, version=version)
    workbook = load_workbook(io.BytesIO(build_template_workbook("asset_initialization", company, version=version)))
    for row in rows:
        workbook[definition.sheet_name].append([row.get(header, "") for header in definition.headers])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return SimpleUploadedFile("identity-import.xlsx", output.getvalue(), content_type=XLSX_MIME)


def import_row(context, **changes):
    row = physical_row(context["company"], context["category"], context["department"], context["employee"], context["location"])
    row.update({"首次管理属性": "LV", **changes})
    return row


def validated(context, row, *, actor=None, version=None):
    return upload_and_validate_import(actor=actor or context["equipment"], company=context["company"],
        import_type="asset_initialization", uploaded_file=upload(context["company"], [row], version),
        idempotency_key="improvement-upload")


@pytest.mark.parametrize("treatment", ["fixed_asset", "controlled_non_fixed"])
def test_lv_stays_in_physical_view_before_and_after_independent_finance(context, client, treatment):
    asset = registered(context, management_attribute="LV")
    client.force_login(context["finance"])
    query = {"view": "individual_durable"}
    assert client.get(reverse("assets:asset-list"), query).context["page"].paginator.count == 1
    assert client.get(reverse("assets:asset-list"), {**query, "accounting_treatment": "unconfirmed"}).context["page"].paginator.count == 1
    dashboard = build_supply_dashboard(actor=context["finance"], company=context["company"])
    assert dashboard["individual_durable_count"] == 1
    assert dashboard["controlled_non_fixed_count"] == 0
    data = {"accounting_treatment": treatment, "original_cost": Decimal("1000.00"), "accounting_treatment_reason": "独立认定"}
    if treatment == "fixed_asset":
        fixed = create_fixed_asset_category(actor=context["finance"], company=context["company"],
            data={"code": "ACC", "name": "会计设备", "useful_life_months_default": 12})
        data.update(fixed_asset_category=fixed, capitalization_date=timezone.localdate())
    code = asset.asset_code
    confirm_asset_finance(actor=context["finance"], asset=asset, finance_data=data, profile_data={},
        idempotency_key="improvement-finance", reason="财务独立确认")
    asset.refresh_from_db()
    assert asset.asset_code == code
    assert client.get(reverse("assets:asset-list"), query).context["page"].paginator.count == 1
    assert client.get(reverse("assets:asset-list"), {**query, "accounting_treatment": treatment}).context["page"].paginator.count == 1
    assert client.get(reverse("assets:asset-list"), {**query, "accounting_treatment": "unconfirmed"}).context["page"].paginator.count == 0


def test_new_fa_nonfixed_is_not_reclassified_as_lv(context, client):
    asset = registered(context, management_attribute="FA")
    confirm_asset_finance(actor=context["finance"], asset=asset,
        finance_data={"accounting_treatment": "controlled_non_fixed", "original_cost": Decimal("500.00"), "accounting_treatment_reason": "独立认定"},
        idempotency_key="fa-nonfixed", reason="财务独立确认")
    client.force_login(context["finance"])
    assert client.get(reverse("assets:asset-list"), {"view": "individual_durable"}).context["page"].paginator.count == 0
    assert client.get(reverse("assets:asset-list"), {"accounting_treatment": "controlled_non_fixed"}).context["page"].paginator.count == 1


@pytest.mark.parametrize("attribute", ["FA", "LV", "IA", "LS", "OT"])
def test_current_import_round_trip_preserves_identity_and_registers_without_editing(context, attribute):
    row = import_row(context, **{"首次管理属性": attribute, "取得年份": 2019, "取得年份依据": "验收单明确为 2019 年", "车牌号": "TEST-01", "车架号": "VIN-01", "校准编号": "CAL-01"})
    batch = validated(context, row)
    assert batch.status == "validated", list(batch.rows.values_list("errors_json", flat=True))
    assert batch.template_version == "asset-initialization-v3"
    assert not Asset.objects.exists()
    confirm_import_batch(actor=context["equipment"], batch=batch)
    asset = Asset.objects.get()
    assert (asset.management_attribute, asset.coding_year, asset.vehicle_plate, asset.chassis_number, asset.calibration_number) == (attribute, 2019, "TEST-01", "VIN-01", "CAL-01")
    assert not AssetIdentity.objects.exists()
    register_asset(actor=context["equipment"], asset=asset, idempotency_key="register-import")
    asset.refresh_from_db()
    assert asset.asset_code == f"{attribute}-02-2019-000001-00"
    assert confirm_import_batch(actor=context["equipment"], batch=batch).status == "confirmed"
    assert Asset.objects.count() == 1


@pytest.mark.parametrize("changes,expected", [
    ({"首次管理属性": ""}, "首次管理属性"),
    ({"首次管理属性": "BAD"}, "首次管理属性"),
    ({"取得年份": 2019, "取得年份依据": ""}, "依据"),
    ({"取得年份": 999}, "取得年份"),
    ({"主资产编号": "FA-02-2020-999999-00"}, "主资产编号"),
])
def test_missing_or_invalid_import_identity_is_reported_before_confirmation(context, changes, expected):
    batch = validated(context, import_row(context, **changes))
    assert batch.error_rows == 1 and batch.status != "validated"
    assert expected in json.dumps(list(batch.rows.values_list("errors_json", flat=True)), ensure_ascii=False)
    assert not Asset.objects.exists()


def test_component_import_inherits_parent_identity(context):
    parent = registered(context, "import-parent")
    batch = validated(context, import_row(context, **{"首次管理属性": "", "主资产编号": parent.asset_code}))
    assert batch.status == "validated", list(batch.rows.values_list("errors_json", flat=True))
    confirm_import_batch(actor=context["equipment"], batch=batch)
    child = Asset.objects.exclude(pk=parent.pk).get()
    assert child.component_of_id == parent.pk and child.management_attribute == "FA" and child.coding_year == 2020
    register_asset(actor=context["equipment"], asset=child, idempotency_key="imported-component")
    child.refresh_from_db()
    assert child.asset_code == "FA-02-2020-000001-01"


def test_import_rechecks_parent_state_at_confirmation(context):
    parent = registered(context, "leased-parent", management_attribute="LS")
    batch = validated(context, import_row(context, **{"首次管理属性": "", "主资产编号": parent.asset_code}))
    assert batch.status == "validated"
    return_custody_asset(actor=context["equipment"], asset=parent, returned_on=timezone.localdate(),
        counterparty="测试出租方", contract_reference="TEST", acceptance_evidence="TEST", reason="测试归还", idempotency_key="returned-parent")
    with pytest.raises(ValidationError, match="主资产状态"):
        confirm_import_batch(actor=context["equipment"], batch=batch)
    assert Asset.objects.count() == 1


def test_component_import_does_not_accept_parent_outside_department_scope(context):
    other = make_department(context["company"], "PRIVATE")
    other_employee = make_employee(context["company"], other, "PRIVATE")
    parent = registered(context, "hidden-parent", department=other, responsible_employee=other_employee)
    manager = make_user("scoped-importer", "department_manager")
    grant_scope(manager, context["company"], context["department"])
    batch = validated(context, import_row(context, **{"首次管理属性": "", "主资产编号": parent.asset_code}), actor=manager)
    assert batch.error_rows == 1
    assert "可用范围" in json.dumps(list(batch.rows.values_list("errors_json", flat=True)), ensure_ascii=False)
    assert Asset.objects.count() == 1


def test_v2_files_remain_compatible_with_legacy_physical_draft_import():
    company, actor, category, department, employee, location = sprint5_context(prefix="COMPATV2")
    row = physical_row(company, category, department, employee, location)
    batch = upload_and_validate_import(actor=actor, company=company, import_type="asset_initialization",
        uploaded_file=upload(company, [row], "asset-initialization-v2"), idempotency_key="legacy-v2")
    assert batch.template_version == "asset-initialization-v2" and batch.status == "validated"
    confirm_import_batch(actor=actor, batch=batch)
    assert Asset.objects.get().management_attribute == ""


def test_v2_files_do_not_guess_attributes_from_finance_when_standard_is_active(context):
    batch = validated(context, import_row(context), version="asset-initialization-v2")
    assert batch.template_version == "asset-initialization-v2" and batch.error_rows == 1
    assert "首次管理属性" in json.dumps(list(batch.rows.values_list("errors_json", flat=True)), ensure_ascii=False)
    assert not Asset.objects.exists()


def test_floor_filter_and_export_include_only_authorized_descendants(context, client):
    visible = registered(context, "visible-room")
    other_department = make_department(context["company"], "HIDDEN")
    other_employee = make_employee(context["company"], other_department, "HIDDEN")
    hidden_room = make_location(context["company"], "HIDDEN", parent=context["location"].parent)
    hidden = registered(context, "hidden-room", department=other_department, responsible_employee=other_employee, location=hidden_room)
    manager = make_user("floor-manager", "department_manager")
    grant_scope(manager, context["company"], context["department"])
    client.force_login(manager)
    floor = context["location"].parent
    response = client.get(reverse("assets:asset-list"), {"location": floor.pk})
    assert response.status_code == 200 and response.context["page"].paginator.count == 1
    assert response.context["page"][0].pk == visible.pk
    options = {item.pk for item in response.context["locations"]}
    assert floor.pk in options and context["location"].pk in options and hidden_room.pk not in options
    assert hidden.asset_code not in response.content.decode()
    preview = client.get(reverse("assets:asset-list-export"), {"location": floor.pk})
    assert preview.status_code == 200 and preview.context["dataset"].row_count == 1
    rejected = client.get(reverse("assets:asset-list"), {"location": hidden_room.pk})
    assert rejected.context["filter_errors"] and rejected.context["page"].paginator.count == 0


def test_employee_directory_filters_and_columns_preserve_notes(context, client):
    employee = context["employee"]
    employee.remark = "岗位：生产助理\n原表人员标记：莹润\n保留原始说明"
    employee.save(update_fields=["remark"])
    other = make_employee(context["company"], context["department"], "HOURLY")
    other.remark = "岗位：小时工\n原表人员标记：小时工"
    other.save(update_fields=["remark"])
    before = list(Employee.objects.order_by("pk").values())
    client.force_login(context["admin"])
    response = client.get(reverse("masterdata:employee-list"), {"department": employee.department_id, "position": "生产助理", "source_mark": "莹润"})
    assert response.status_code == 200 and response.context["objects"].count() == 1
    assert response.context["objects"][0].pk == employee.pk
    assert "生产助理" in response.content.decode() and "莹润" in response.content.decode()
    employee.refresh_from_db()
    assert employee.remark == "岗位：生产助理\n原表人员标记：莹润\n保留原始说明"
    assert list(Employee.objects.order_by("pk").values()) == before


def test_employee_filter_preserves_and_explains_unmatched_selection(context, client):
    client.force_login(context["admin"])
    response = client.get(reverse("masterdata:employee-list"), {"position": "旧岗位"})
    assert response.status_code == 200 and response.context["objects"].count() == 0
    assert response.context["filter_errors"]
    assert 'value="旧岗位" selected' in response.content.decode()


def test_employee_directory_does_not_expose_out_of_scope_filter_options(context, client):
    private = make_department(context["company"], "PRIVATE")
    employee = make_employee(context["company"], private, "PRIVATE")
    employee.remark = "岗位：隐藏岗位\n原表人员标记：隐藏标记"
    employee.save(update_fields=["remark"])
    manager = make_user("directory-manager", "department_manager")
    grant_scope(manager, context["company"], context["department"])
    client.force_login(manager)
    response = client.get(reverse("masterdata:employee-list"))
    assert "隐藏岗位" not in response.content.decode() and private.pk not in {d.pk for d in response.context["department_options"]}
    response = client.get(reverse("masterdata:employee-list"), {"department": private.pk})
    assert response.context["filter_errors"] and response.context["objects"].count() == 0


@pytest.mark.parametrize("form_class,field", [(InventoryTaskForm, "planned_start"), (MaintenancePlanForm, "first_due_date"), (SupplyCountTaskForm, "planned_start")])
def test_date_initial_values_and_invalid_form_redisplay_stay_iso(context, form_class, field):
    with translation.override("zh-hans"):
        form = form_class(actor=context["equipment"], company=context["company"], initial={field: date(2026, 9, 12)})
        assert 'value="2026-09-12"' in str(form[field])
        bound = form_class({field: "2026-09-12"}, actor=context["equipment"], company=context["company"])
        assert not bound.is_valid()
        assert 'value="2026-09-12"' in str(bound[field])


def test_read_request_roles_are_loaded_once_and_not_retained_between_requests(context):
    actor = context["equipment"]
    observed = []
    def view(request):
        for _ in range(8):
            observed.append(role_names_for(actor))
        return HttpResponse("ok")
    handler = RequestRoleCacheMiddleware(view)
    for _ in range(2):
        with CaptureQueriesContext(connection) as queries:
            handler(RequestFactory().get("/"))
        assert len(queries) == 1
    assert all("equipment" in roles for roles in observed)
    assert not role_names_for(context["finance"]) == role_names_for(actor)


def test_post_permissions_are_reread_after_roles_change(context):
    actor = context["equipment"]
    def view(request):
        assert "equipment" in role_names_for(actor)
        actor.groups.clear()
        assert "equipment" not in role_names_for(actor)
        return HttpResponse("ok")
    RequestRoleCacheMiddleware(view)(RequestFactory().post("/"))


def test_failed_get_does_not_leave_a_stale_role_cache(context):
    actor = context["equipment"]
    def view(request):
        assert "equipment" in role_names_for(actor)
        raise RuntimeError("render failed")
    with pytest.raises(RuntimeError):
        RequestRoleCacheMiddleware(view)(RequestFactory().get("/"))
    actor.groups.clear()
    assert "equipment" not in role_names_for(actor)
