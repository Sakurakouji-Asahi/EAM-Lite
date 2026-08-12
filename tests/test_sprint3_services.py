from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection

from apps.assets.models import Asset, AssetCodeHistory, AssetCustomValue
from apps.assets.services import (
    create_asset_draft,
    delete_asset_draft,
    set_requested_coding_scheme,
    submit_asset_for_finance,
    update_asset_draft,
    withdraw_asset_to_draft,
)
from apps.audit.models import AuditLog
from apps.masterdata.models import AssetCodingScheme, IssuedCode, SequenceCounter
from tests.test_sprint3_support import (
    add_photo,
    complete_asset_data,
    complete_initialization,
    grant_scope,
    make_asset,
    make_category,
    make_company,
    make_custom_field,
    make_department,
    make_employee,
    make_location_tree,
    make_structurally_valid_active_scheme,
    make_user,
)


pytestmark = pytest.mark.django_db


def make_context(*, role="equipment"):
    actor = make_user(f"{role}-actor", role)
    company = make_company()
    complete_initialization(company, actor)
    department = make_department(company)
    employee = make_employee(company, department)
    category = make_category(company)
    _site, _area, location = make_location_tree(company)
    return actor, company, department, employee, category, location


def test_real_initialization_gate_blocks_every_asset_service_entry_without_fixture():
    actor = make_user("equipment", "equipment")
    company = make_company()
    department = make_department(company)
    employee = make_employee(company, department)
    category = make_category(company)
    _site, _area, location = make_location_tree(company)
    data = complete_asset_data(category, department, employee, location)

    with pytest.raises(PermissionDenied, match="初始化尚未完成"):
        create_asset_draft(actor=actor, company=company, data=data)

    assert not Asset.objects.exists()
    assert not AuditLog.objects.filter(action="asset_draft_create").exists()


def test_inactive_company_blocks_direct_asset_service_even_if_initialized():
    actor, company, department, employee, category, location = make_context()
    company.is_active = False
    company.save(update_fields=["is_active"])

    with pytest.raises(PermissionDenied):
        create_asset_draft(
            actor=actor,
            company=company,
            data=complete_asset_data(category, department, employee, location),
        )

    assert not Asset.objects.exists()


def test_completed_gate_is_backed_by_all_nine_true_flags_and_persists():
    actor, company, department, employee, category, location = make_context()

    setting = company.initialization_setting
    assert setting.initialization_completed
    assert all(
        getattr(setting, name)
        for name in (
            "company_configured",
            "departments_configured",
            "employees_configured",
            "categories_configured",
            "locations_configured",
            "coding_scheme_configured",
            "finance_rules_configured",
            "permissions_configured",
            "users_configured",
        )
    )
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    assert asset.asset_status == "draft"


def test_create_and_update_draft_are_audited_and_never_issue_code():
    actor, company, department, employee, category, location = make_context()
    text = make_custom_field(company, category, "COLOR", "text")
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
        custom_values={str(text.pk): "red"},
    )

    updated = update_asset_draft(
        actor=actor,
        asset=asset,
        data={"asset_name": "更新后设备", "quantity": 1},
        custom_values={str(text.pk): "blue"},
    )

    assert updated.asset_name == "更新后设备"
    assert updated.custom_values.get().value_text == "blue"
    create_log = AuditLog.objects.get(action="asset_draft_create")
    update_log = AuditLog.objects.get(action="asset_draft_update")
    assert create_log.company == company == update_log.company
    assert create_log.new_data_json["custom_values"] == {"COLOR": "red"}
    assert update_log.old_data_json["custom_values"] == {"COLOR": "red"}
    assert update_log.new_data_json["custom_values"] == {"COLOR": "blue"}
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetCodeHistory.objects.count() == 0


@pytest.mark.parametrize(
    ("forbidden_field", "value"),
    (
        ("original_cost", Decimal("5000.00")),
        ("accounting_treatment", "fixed_asset"),
        ("book_value", Decimal("1.00")),
        ("asset_status", "in_use"),
        ("record_status", "archived"),
        ("asset_code", "FAKE"),
        ("current_issued_code", 1),
        ("requested_coding_scheme", 1),
    ),
)
def test_constructed_forbidden_payload_is_rejected_without_mutation(
    forbidden_field, value
):
    actor, company, department, employee, category, location = make_context()
    data = complete_asset_data(category, department, employee, location)
    data[forbidden_field] = value

    with pytest.raises(PermissionDenied):
        create_asset_draft(actor=actor, company=company, data=data)

    assert Asset.objects.count() == 0
    assert AuditLog.objects.filter(action="asset_draft_create").count() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (("quantity", 2), ("quantity", 0), ("tracking_mode", "batch")),
)
def test_service_rejects_batch_or_non_single_asset_payload(field, value):
    actor, company, department, employee, category, location = make_context()
    data = complete_asset_data(category, department, employee, location)
    data[field] = value

    with pytest.raises(ValidationError):
        create_asset_draft(actor=actor, company=company, data=data)

    assert not Asset.objects.exists()


def test_department_manager_can_create_only_inside_current_authorized_scope():
    manager = make_user("manager", "department_manager")
    company = make_company()
    complete_initialization(company, manager)
    root = make_department(company, "ROOT")
    child = make_department(company, "CHILD", parent=root)
    outside = make_department(company, "OUT")
    child_employee = make_employee(company, child, "E1")
    outside_employee = make_employee(company, outside, "E2")
    category = make_category(company)
    _site, _area, location = make_location_tree(company)
    grant_scope(manager, company, root)

    allowed = make_asset(
        actor=manager,
        company=company,
        category=category,
        department=child,
        employee=child_employee,
        location=location,
    )
    with pytest.raises(PermissionDenied):
        make_asset(
            actor=manager,
            company=company,
            category=category,
            department=outside,
            employee=outside_employee,
            location=location,
        )

    assert Asset.objects.filter(pk=allowed.pk).exists()
    assert Asset.objects.count() == 1


def test_department_manager_cannot_reparent_asset_outside_authorized_scope():
    manager = make_user("manager-move", "department_manager")
    company = make_company()
    complete_initialization(company, manager)
    inside = make_department(company, "IN")
    outside = make_department(company, "OUT")
    inside_employee = make_employee(company, inside, "IN-E")
    outside_employee = make_employee(company, outside, "OUT-E")
    category = make_category(company)
    _site, _area, location = make_location_tree(company)
    grant_scope(manager, company, inside)
    asset = make_asset(
        actor=manager,
        company=company,
        category=category,
        department=inside,
        employee=inside_employee,
        location=location,
    )

    with pytest.raises(PermissionDenied):
        update_asset_draft(
            actor=manager,
            asset=asset,
            data={
                "department": outside,
                "responsible_employee": outside_employee,
            },
        )

    asset.refresh_from_db()
    assert asset.department_id == inside.pk
    assert asset.responsible_employee_id == inside_employee.pk
    assert not AuditLog.objects.filter(action="asset_draft_update").exists()


def test_system_admin_cannot_create_or_edit_p0_p1_but_can_select_current_scheme():
    equipment, company, department, employee, category, location = make_context()
    admin = make_user("admin", "system_admin")
    asset = make_asset(
        actor=equipment,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    scheme = make_structurally_valid_active_scheme(
        company=company,
        actor=admin,
    )

    with pytest.raises(PermissionDenied):
        update_asset_draft(actor=admin, asset=asset, data={"asset_name": "越权"})
    selected = set_requested_coding_scheme(
        actor=admin, asset=asset, coding_scheme=scheme
    )

    assert selected.requested_coding_scheme == scheme
    assert selected.asset_code is None
    assert AuditLog.objects.filter(action="asset_coding_scheme_select").count() == 1
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0


def test_non_admin_and_invalid_or_cross_company_scheme_are_rejected():
    actor, company, department, employee, category, location = make_context()
    admin = make_user("admin", "system_admin")
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    draft = AssetCodingScheme.objects.create(
        company=company,
        name="草稿方案",
        scheme_key="DRAFT",
        version=1,
        status="draft",
        reset_mode="never",
        sequence_start=1,
    )
    other = make_company("C2", active=False)
    foreign = make_structurally_valid_active_scheme(
        company=other,
        actor=admin,
        key="FOREIGN",
    )

    with pytest.raises(PermissionDenied):
        set_requested_coding_scheme(actor=actor, asset=asset, coding_scheme=None)
    for scheme in (draft, foreign):
        with pytest.raises(ValidationError):
            set_requested_coding_scheme(actor=admin, asset=asset, coding_scheme=scheme)
    asset.refresh_from_db()
    assert asset.requested_coding_scheme_id is None
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0


def test_submit_requires_all_physical_fields_leaf_photo_and_required_custom_values():
    actor, company, department, employee, category, location = make_context()
    required = make_custom_field(
        company, category, "REQUIRED", "text", required=True
    )
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )

    with pytest.raises(ValidationError) as missing:
        submit_asset_for_finance(actor=actor, asset=asset)
    assert "attachments" in missing.value.message_dict
    assert "custom_values" in missing.value.message_dict

    add_photo(actor, asset)
    update_asset_draft(
        actor=actor,
        asset=asset,
        data={},
        custom_values={str(required.pk): "已补齐"},
    )
    submitted = submit_asset_for_finance(actor=actor, asset=asset)

    assert submitted.asset_status == "pending_finance"
    assert submitted.asset_code is None
    assert submitted.current_issued_code_id is None
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetCodeHistory.objects.count() == 0


@pytest.mark.parametrize(
    "missing_field",
    ("unit", "department", "responsible_employee", "location"),
)
def test_submit_rejects_each_missing_required_physical_field(missing_field):
    actor, company, department, employee, category, location = make_context()
    data = {
        "department": department,
        "employee": employee,
        "location": location,
    }
    data[{
        "responsible_employee": "employee",
        "department": "department",
        "location": "location",
    }.get(missing_field, missing_field)] = None
    if missing_field == "department":
        # A department-less draft cannot validly retain an employee FK: the
        # PostgreSQL BEFORE trigger enforces employee/department coherence.
        # Submission still proves the department requirement independently.
        data["employee"] = None
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=data["department"],
        employee=data["employee"],
        location=data["location"],
        **({"unit": ""} if missing_field == "unit" else {}),
    )
    add_photo(actor, asset)

    with pytest.raises(ValidationError) as error:
        submit_asset_for_finance(actor=actor, asset=asset)

    assert missing_field in error.value.message_dict
    asset.refresh_from_db()
    assert asset.asset_status == "draft"


def test_submit_rejects_non_leaf_location_cross_department_or_inactive_employee():
    actor, company, department, employee, category, leaf = make_context()
    site = leaf.parent.parent
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=site,
    )
    add_photo(actor, asset)

    with pytest.raises(ValidationError):
        submit_asset_for_finance(actor=actor, asset=asset)

    other_department = make_department(company, "D2")
    other_employee = make_employee(company, other_department, "E2")
    asset.location = leaf
    asset.responsible_employee = other_employee
    with pytest.raises(ValidationError):
        asset.full_clean()
    employee.is_active = False
    employee.save(update_fields=["is_active"])
    asset.responsible_employee = employee
    with pytest.raises(ValidationError):
        asset.full_clean()


def test_repeat_submit_is_idempotent_and_writes_one_audit_row():
    actor, company, department, employee, category, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    add_photo(actor, asset)

    first = submit_asset_for_finance(actor=actor, asset=asset)
    second = submit_asset_for_finance(actor=actor, asset=asset)

    assert first.pk == second.pk
    assert second.asset_status == "pending_finance"
    assert AuditLog.objects.filter(action="asset_submit_finance").count() == 1
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetCodeHistory.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_submit_writes_one_transition_and_no_code():
    if connection.vendor != "postgresql":
        pytest.skip("row-lock concurrency acceptance requires PostgreSQL")
    actor, company, department, employee, category, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    add_photo(actor, asset)
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            local_actor = type(actor).objects.get(pk=actor.pk)
            local_asset = Asset.objects.get(pk=asset.pk)
            barrier.wait(timeout=10)
            result = submit_asset_for_finance(actor=local_actor, asset=local_asset)
            return result.asset_status
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: worker(), range(2)))

    assert results == ["pending_finance", "pending_finance"]
    assert AuditLog.objects.filter(action="asset_submit_finance").count() == 1
    assert AssetCodeHistory.objects.count() == 0
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0


def test_only_original_submitter_or_finance_can_withdraw_and_reason_is_required():
    actor, company, department, employee, category, location = make_context()
    outsider = make_user("outsider", "equipment")
    finance = make_user("finance", "finance")
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    add_photo(actor, asset)
    asset = submit_asset_for_finance(actor=actor, asset=asset)

    with pytest.raises(ValidationError):
        withdraw_asset_to_draft(actor=actor, asset=asset, reason="")
    with pytest.raises(PermissionDenied):
        withdraw_asset_to_draft(actor=outsider, asset=asset, reason="越权")
    returned = withdraw_asset_to_draft(
        actor=finance, asset=asset, reason="资料需更正"
    )

    assert returned.asset_status == "draft"
    assert returned.submitted_by_id is None
    assert returned.submitted_at is None
    log = AuditLog.objects.get(action="asset_withdraw_to_draft")
    assert log.new_data_json["reason"] == "资料需更正"


def test_delete_draft_role_and_reference_boundaries_are_enforced_and_audited():
    equipment, company, department, employee, category, location = make_context()
    warehouse_owner = make_user("warehouse-owner", "warehouse")
    warehouse_other = make_user("warehouse-other", "warehouse")
    asset = make_asset(
        actor=warehouse_owner,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )

    with pytest.raises(PermissionDenied):
        delete_asset_draft(
            actor=warehouse_other, asset=asset, reason="不是本人创建"
        )
    delete_asset_draft(actor=equipment, asset=asset, reason="误建草稿")

    assert not Asset.objects.filter(pk=asset.pk).exists()
    log = AuditLog.objects.get(action="asset_draft_delete")
    assert log.object_id == str(asset.pk)
    assert log.old_data_json["reason"] == "误建草稿"

    referenced = make_asset(
        actor=equipment,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    # A custom value is strict draft-owned data and may cascade with an
    # otherwise unreferenced draft.  An attachment is retained evidence and
    # therefore makes the draft ineligible for physical deletion.
    add_photo(equipment, referenced)
    with pytest.raises(ValidationError, match="业务引用"):
        delete_asset_draft(actor=equipment, asset=referenced, reason="有引用")
    assert Asset.objects.filter(pk=referenced.pk).exists()


def test_pending_asset_cannot_be_physically_deleted_or_jump_to_formal_status():
    actor, company, department, employee, category, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    add_photo(actor, asset)
    asset = submit_asset_for_finance(actor=actor, asset=asset)

    with pytest.raises(ValidationError):
        asset.delete()
    asset.asset_status = "pending_label"
    with pytest.raises(ValidationError):
        asset.full_clean()

    assert Asset.objects.filter(pk=asset.pk).exists()
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetCodeHistory.objects.count() == 0
