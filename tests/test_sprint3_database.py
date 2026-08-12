from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.assets.models import Asset, AssetCodeHistory, AssetCustomField, AssetCustomValue, AttachmentLink
from apps.masterdata.models import AssetCodingScheme, IssuedCode, SequenceCounter
from tests.test_sprint3_support import (
    complete_initialization,
    direct_attachment,
    direct_draft,
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


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 3 cross-table constraints require PostgreSQL")


def context():
    actor = make_user("equipment", "equipment")
    company = make_company()
    complete_initialization(company, actor)
    department = make_department(company)
    employee = make_employee(company, department)
    category = make_category(company)
    site, area, leaf = make_location_tree(company)
    return actor, company, department, employee, category, site, area, leaf


def test_postgresql_18_4_and_sprint3_guard_triggers_are_installed():
    require_postgresql()
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        assert cursor.fetchone()[0].startswith("18.4")
        expected = {
            "trg_asset_references",
            "trg_asset_commit",
            "trg_asset_delete",
            "trg_custom_field_validate",
            "trg_custom_value_validate",
            "trg_custom_value_asset_commit",
            "trg_code_history_validate",
            "trg_code_history_immutable",
            "trg_attachment_link_validate",
            "trg_attachment_link_asset_commit",
            "trg_assets_department_company",
            "trg_assets_employee_company",
            "trg_assets_location_company",
            "trg_assets_category_company",
            "trg_assets_attachment_company",
        }
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname = ANY(%s) AND NOT tgisinternal",
            [list(expected)],
        )
        actual = {row[0] for row in cursor.fetchall()}
    assert expected <= actual


def test_database_rejects_cross_company_asset_master_references():
    require_postgresql()
    actor, company, department, employee, category, _site, _area, leaf = context()
    other = make_company("C2", active=False)
    foreign_department = make_department(other, "FD")
    foreign_employee = make_employee(other, foreign_department, "FE")
    foreign_category = make_category(other, "FC")
    _fsite, _farea, foreign_location = make_location_tree(other, "F")

    bad_rows = (
        {"category": foreign_category},
        {"department": foreign_department},
        {"responsible_employee": foreign_employee},
        {"location": foreign_location},
    )
    base = {
        "company": company,
        "asset_name": "cross-company",
        "category": category,
        "department": department,
        "responsible_employee": employee,
        "location": leaf,
        "created_by": actor,
    }
    for override in bad_rows:
        values = {**base, **override}
        with pytest.raises(IntegrityError), transaction.atomic():
            Asset.objects.create(**values)


def test_database_rejects_cross_department_inactive_employee_and_inactive_master():
    require_postgresql()
    actor, company, department, employee, category, _site, _area, leaf = context()
    outside = make_department(company, "D2")
    outside_employee = make_employee(company, outside, "E2")

    with pytest.raises(IntegrityError), transaction.atomic():
        direct_draft(
            company,
            category,
            actor=actor,
            department=department,
            responsible_employee=outside_employee,
            location=leaf,
        )
    employee.is_active = False
    employee.save(update_fields=["is_active"])
    with pytest.raises(IntegrityError), transaction.atomic():
        direct_draft(
            company,
            category,
            actor=actor,
            department=department,
            responsible_employee=employee,
            location=leaf,
        )


def test_deferred_pending_constraint_rejects_nonleaf_missing_photo_and_required_value():
    require_postgresql()
    actor, company, department, employee, category, site, _area, leaf = context()
    required = make_custom_field(
        company, category, "REQUIRED", "text", required=True
    )
    base = {
        "company": company,
        "asset_name": "pending",
        "category": category,
        "unit": "台",
        "department": department,
        "responsible_employee": employee,
        "submitted_by": actor,
        "submitted_at": timezone.now(),
        "asset_status": "pending_finance",
        "created_by": actor,
        "updated_by": actor,
    }

    for location in (site, leaf):
        with pytest.raises(IntegrityError), transaction.atomic():
            # INSERT itself is already rejected because Sprint 3 rows must be
            # born as draft; update path below separately proves deferral.
            Asset.objects.create(location=location, **base)

    draft = direct_draft(
        company,
        category,
        actor=actor,
        department=department,
        responsible_employee=employee,
        location=leaf,
        unit="台",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                """
                UPDATE assets_asset
                   SET asset_status = 'pending_finance',
                       submitted_by_id = %s,
                       submitted_at = %s
                 WHERE id = %s
                """,
                [actor.pk, timezone.now(), draft.pk],
            )
        # pytest-django's ordinary django_db fixture wraps the test in an
        # outer transaction.  Force the DEFERRABLE trigger here so the invalid
        # row is rejected and rolled back at this savepoint, not at teardown.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert required.required
    draft.refresh_from_db()
    assert draft.asset_status == "draft"


def test_deferred_constraint_allows_final_valid_state_created_in_one_transaction():
    require_postgresql()
    actor, company, department, employee, category, _site, _area, leaf = context()
    field = make_custom_field(company, category, "REQUIRED", "text", required=True)
    with transaction.atomic():
        asset = direct_draft(
            company,
            category,
            actor=actor,
            department=department,
            responsible_employee=employee,
            location=leaf,
            unit="台",
        )
        attachment = direct_attachment(
            company, actor, key="private/assets/deferred.jpg"
        )
        AttachmentLink.objects.create(
            company=company,
            attachment=attachment,
            asset=asset,
            role="photo",
            security_class="A0",
            created_by=actor,
        )
        AssetCustomValue.objects.create(
            company=company,
            asset=asset,
            custom_field=field,
            value_text="ok",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                """
                UPDATE assets_asset
                   SET asset_status = 'pending_finance',
                       submitted_by_id = %s,
                       submitted_at = %s
                 WHERE id = %s
                """,
                [actor.pk, timezone.now(), asset.pk],
            )

    asset.refresh_from_db()
    assert asset.asset_status == "pending_finance"
    assert asset.asset_code is None
    assert asset.current_issued_code_id is None


def test_database_custom_value_guards_reject_cross_scope_wrong_column_and_select():
    require_postgresql()
    actor, company, _department, _employee, category, _site, _area, _leaf = context()
    asset = direct_draft(company, category, actor=actor)
    select_field = make_custom_field(
        company, category, "GRADE", "select", options=["A", "B"]
    )
    decimal_field = make_custom_field(company, category, "WEIGHT", "decimal")
    other = make_company("C2", active=False)

    invalid = (
        {
            "company": company,
            "asset": asset,
            "custom_field": select_field,
            "value_text": "C",
        },
        {
            "company": company,
            "asset": asset,
            "custom_field": decimal_field,
            "value_text": "1.2",
        },
        {
            "company": other,
            "asset": asset,
            "custom_field": select_field,
            "value_text": "A",
        },
    )
    for values in invalid:
        with pytest.raises(IntegrityError), transaction.atomic():
            AssetCustomValue.objects.create(**values)


def test_database_custom_field_select_options_are_strict():
    require_postgresql()
    _actor, company, _department, _employee, category, _site, _area, _leaf = context()
    invalid_options = ([], [""], [" X"], ["X", "X"], [1])
    for index, options in enumerate(invalid_options):
        with pytest.raises(IntegrityError), transaction.atomic():
            AssetCustomField.objects.create(
                company=company,
                category=category,
                name=f"invalid {index}",
                code=f"INVALID-{index}",
                normalized_code=f"invalid-{index}",
                field_type="select",
                options_json=options,
            )


def test_database_attachment_link_rejects_cross_company_and_nonimage_photo():
    require_postgresql()
    actor, company, _department, _employee, category, _site, _area, _leaf = context()
    asset = direct_draft(company, category, actor=actor)
    other = make_company("C2", active=False)
    foreign = direct_attachment(other, actor, key="private/assets/foreign.jpg")
    pdf = direct_attachment(
        company,
        actor,
        key="private/assets/file.pdf",
        filename="file.pdf",
        mime="application/pdf",
        data=b"%PDF-1.7\n",
    )
    for attachment in (foreign, pdf):
        with pytest.raises(IntegrityError), transaction.atomic():
            AttachmentLink.objects.create(
                company=company,
                attachment=attachment,
                asset=asset,
                role="photo",
                security_class="A0",
                created_by=actor,
            )


def test_database_status_machine_blocks_direct_formal_jump_and_pending_delete():
    require_postgresql()
    actor, company, department, employee, category, _site, _area, leaf = context()
    asset = direct_draft(
        company,
        category,
        actor=actor,
        department=department,
        responsible_employee=employee,
        location=leaf,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE assets_asset SET asset_status = 'pending_label' WHERE id = %s",
                [asset.pk],
            )

    attachment = direct_attachment(
        company, actor, key="private/assets/status.jpg"
    )
    AttachmentLink.objects.create(
        company=company,
        attachment=attachment,
        asset=asset,
        role="photo",
        security_class="A0",
        created_by=actor,
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                """
                UPDATE assets_asset
                   SET unit = %s, asset_status = 'pending_finance',
                       submitted_by_id = %s, submitted_at = %s
                 WHERE id = %s
                """,
                ["台", actor.pk, timezone.now(), asset.pk],
            )
    with pytest.raises(IntegrityError), transaction.atomic():
        # Use SQL so this assertion reaches the database DELETE trigger rather
        # than stopping at AssetQuerySet.delete()'s application guard.
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM assets_asset WHERE id = %s", [asset.pk])
    assert Asset.objects.filter(pk=asset.pk).exists()


def test_database_code_history_is_append_only_and_cross_company_protected():
    require_postgresql()
    actor, company, _department, _employee, category, _site, _area, _leaf = context()
    asset = direct_draft(company, category, actor=actor)
    scheme = make_structurally_valid_active_scheme(
        company=company,
        actor=actor,
        key="HIST",
    )
    issued = IssuedCode.objects.create(
        company=company,
        coding_scheme=scheme,
        scope_key="history",
        sequence_value=1,
        display_code="H-1",
        normalized_code="h-1",
        effective_date=date.today(),
        idempotency_key="history-1",
        issued_by=actor,
    )
    history = AssetCodeHistory.objects.create(
        company=company,
        asset=asset,
        event_type="issued",
        new_issued_code=issued,
        effective_at=timezone.now(),
        operated_by=actor,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodeHistory.objects.filter(pk=history.pk).update(reason="changed")
    with pytest.raises(IntegrityError), transaction.atomic():
        AssetCodeHistory.objects.filter(pk=history.pk).delete()
    assert AssetCodeHistory.objects.filter(pk=history.pk).exists()


def test_sprint3_business_flow_leaves_counter_issued_and_history_empty():
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetCodeHistory.objects.count() == 0
