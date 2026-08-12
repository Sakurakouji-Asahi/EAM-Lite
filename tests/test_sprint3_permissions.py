from __future__ import annotations

import pytest

from apps.assets.permissions import (
    can_create_asset_draft,
    can_delete_asset_draft,
    can_edit_asset_draft,
    can_set_requested_coding_scheme,
    can_view_asset,
    can_view_asset_p1,
    can_view_attachment,
    can_view_financial_fields,
    can_write_financial_fields,
    scoped_assets,
)
from tests.test_sprint3_support import (
    complete_initialization,
    direct_attachment,
    direct_draft,
    grant_scope,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
)
from apps.assets.models import AttachmentLink


pytestmark = pytest.mark.django_db


def build_scope_context():
    company = make_company()
    seed = make_user("seed", "system_admin")
    complete_initialization(company, seed)
    inside = make_department(company, "IN")
    outside = make_department(company, "OUT")
    category = make_category(company)
    _site, _area, location = make_location_tree(company)
    return company, inside, outside, category, location


@pytest.mark.parametrize(
    "role",
    ("system_admin", "finance", "equipment", "warehouse", "hr", "management"),
)
def test_global_roles_query_is_company_scoped(role):
    company, inside, _outside, category, _location = build_scope_context()
    actor = make_user(f"{role}-viewer", role)
    local = direct_draft(company, category, department=inside)
    other = make_company("C2", active=False)
    other_category = make_category(other, "EQ2")
    direct_draft(other, other_category)

    assert list(scoped_assets(actor, company)) == [local]
    assert can_view_asset(actor, local)


def test_department_manager_descendant_scope_and_outside_record_denial():
    company, root, outside, category, _location = build_scope_context()
    child = make_department(company, "CHILD", parent=root)
    manager = make_user("manager", "department_manager")
    grant_scope(manager, company, root)
    inside_asset = direct_draft(company, category, department=child)
    outside_asset = direct_draft(company, category, department=outside)

    assert set(scoped_assets(manager, company)) == {inside_asset}
    assert can_view_asset(manager, inside_asset)
    assert not can_view_asset(manager, outside_asset)
    assert can_edit_asset_draft(manager, inside_asset)
    assert not can_edit_asset_draft(manager, outside_asset)


def test_department_scope_without_role_grants_no_asset_access_or_action():
    company, inside, _outside, category, _location = build_scope_context()
    user = make_user("scope-only", "employee")
    grant_scope(user, company, inside)
    asset = direct_draft(company, category, department=inside)

    assert not can_view_asset(user, asset)
    assert not can_create_asset_draft(user, company, inside)
    assert not can_edit_asset_draft(user, asset)


def test_employee_sees_only_assets_for_linked_employee_identity():
    company, inside, _outside, category, _location = build_scope_context()
    user = make_user("employee-user", "employee")
    linked = make_employee(company, inside, "E1", user=user)
    other_employee = make_employee(company, inside, "E2")
    own = direct_draft(company, category, department=inside, responsible_employee=linked)
    other = direct_draft(
        company, category, department=inside, responsible_employee=other_employee
    )

    assert set(scoped_assets(user, company)) == {own}
    assert can_view_asset(user, own)
    assert not can_view_asset(user, other)
    assert not can_edit_asset_draft(user, own)


def test_p0_p1_f1_matrix_and_system_admin_business_boundary():
    company, inside, _outside, category, _location = build_scope_context()
    asset = direct_draft(company, category, department=inside)
    admin = make_user("admin", "system_admin")
    finance = make_user("finance", "finance")
    equipment = make_user("equipment", "equipment")
    hr = make_user("hr", "hr")
    management = make_user("management", "management")

    assert can_view_asset(admin, asset) and can_view_asset_p1(admin, asset)
    assert not can_edit_asset_draft(admin, asset)
    assert can_set_requested_coding_scheme(admin, asset)
    assert can_view_asset(hr, asset)
    assert not can_view_asset_p1(hr, asset)
    assert not can_view_financial_fields(admin)
    assert can_view_financial_fields(finance)
    assert can_write_financial_fields(finance)
    assert not can_view_financial_fields(equipment)
    assert can_view_financial_fields(management)
    assert not can_write_financial_fields(management)


def test_draft_create_edit_and_delete_matrix():
    company, inside, _outside, category, _location = build_scope_context()
    finance = make_user("finance", "finance")
    equipment = make_user("equipment", "equipment")
    warehouse = make_user("warehouse", "warehouse")
    admin = make_user("admin", "system_admin")
    manager = make_user("manager", "department_manager")
    grant_scope(manager, company, inside)
    manager_asset = direct_draft(
        company, category, actor=manager, department=inside
    )
    warehouse_asset = direct_draft(
        company, category, actor=warehouse, department=inside
    )

    for actor in (finance, equipment, warehouse):
        assert can_create_asset_draft(actor, company, inside)
        assert can_edit_asset_draft(actor, manager_asset)
    assert can_create_asset_draft(manager, company, inside)
    assert not can_create_asset_draft(admin, company, inside)
    assert not can_edit_asset_draft(admin, manager_asset)
    assert can_delete_asset_draft(finance, manager_asset)
    assert can_delete_asset_draft(equipment, manager_asset)
    assert can_delete_asset_draft(manager, manager_asset)
    assert not can_delete_asset_draft(manager, warehouse_asset)
    assert can_delete_asset_draft(warehouse, warehouse_asset)
    assert not can_delete_asset_draft(warehouse, manager_asset)


def test_a0_a1_attachment_matrix_never_leaks_financial_metadata():
    company, inside, _outside, category, _location = build_scope_context()
    creator = make_user("creator", "equipment")
    finance = make_user("finance", "finance")
    management = make_user("management", "management")
    equipment = make_user("equipment", "equipment")
    admin = make_user("admin", "system_admin")
    hr = make_user("hr", "hr")
    asset = direct_draft(company, category, actor=creator, department=inside)
    normal = direct_attachment(
        company, creator, key="private/assets/a0.jpg", filename="a0.jpg"
    )
    financial = direct_attachment(
        company,
        finance,
        key="private/assets/a1.pdf",
        filename="secret-invoice.pdf",
        mime="application/pdf",
        data=b"%PDF-1.7\n",
    )
    a0 = AttachmentLink.objects.create(
        company=company,
        attachment=normal,
        asset=asset,
        role="photo",
        security_class="A0",
        created_by=creator,
    )
    a1 = AttachmentLink.objects.create(
        company=company,
        attachment=financial,
        asset=asset,
        role="invoice",
        security_class="A1",
        created_by=finance,
    )

    for viewer in (finance, management):
        assert can_view_attachment(viewer, a0)
        assert can_view_attachment(viewer, a1)
    for viewer in (equipment, admin):
        assert can_view_attachment(viewer, a0)
        assert not can_view_attachment(viewer, a1)
    assert not can_view_attachment(hr, a0)
    assert not can_view_attachment(hr, a1)


def test_cross_department_attachment_access_inherits_asset_scope():
    company, inside, outside, category, _location = build_scope_context()
    manager = make_user("manager", "department_manager")
    equipment = make_user("equipment", "equipment")
    grant_scope(manager, company, inside)
    asset = direct_draft(company, category, actor=equipment, department=outside)
    attachment = direct_attachment(
        company, equipment, key="private/assets/outside.jpg"
    )
    link = AttachmentLink.objects.create(
        company=company,
        attachment=attachment,
        asset=asset,
        role="photo",
        security_class="A0",
        created_by=equipment,
    )

    assert not can_view_asset(manager, asset)
    assert not can_view_attachment(manager, link)
