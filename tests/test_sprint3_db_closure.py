from __future__ import annotations

from django.db import IntegrityError, connection, transaction
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
import pytest

from apps.assets.models import Asset, AttachmentLink
from apps.assets.services import (
    delete_asset_draft,
    set_requested_coding_scheme,
    submit_asset_for_finance,
    update_asset_draft,
    upload_asset_attachment,
    void_asset_attachment,
    withdraw_asset_to_draft,
)
from apps.audit.models import AuditLog
from apps.masterdata.models import Attachment
from tests.test_sprint3_support import (
    direct_attachment,
    direct_draft,
    complete_initialization,
    complete_asset_data,
    jpeg_upload,
    make_category,
    make_company,
    make_custom_field,
    make_department,
    make_employee,
    make_location,
    make_location_tree,
    make_user,
)


pytestmark = pytest.mark.django_db


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 3 database closure uses PostgreSQL triggers")


def pending_asset_context():
    actor = make_user("db-closure", "equipment")
    company = make_company("DBC")
    department = make_department(company, "DBD")
    employee = make_employee(company, department, "DBE")
    category = make_category(company, "DBCAT")
    _site, _area, leaf = make_location_tree(company, "DBL")
    asset = direct_draft(
        company,
        category,
        actor=actor,
        asset_name="待财务数据库保护资产",
        unit="台",
        department=department,
        responsible_employee=employee,
        location=leaf,
    )
    attachment = direct_attachment(
        company, actor, key="private/assets/db-closure.jpg"
    )
    link = AttachmentLink.objects.create(
        company=company,
        asset=asset,
        attachment=attachment,
        role=AttachmentLink.Role.PHOTO,
        security_class=AttachmentLink.SecurityClass.A0,
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
                   SET asset_status = 'pending_finance',
                       submitted_by_id = %s,
                       submitted_at = %s
                 WHERE id = %s
                """,
                [actor.pk, timezone.now(), asset.pk],
            )
    asset.refresh_from_db()
    return actor, company, department, employee, category, leaf, asset, attachment, link


def test_postgresql_controlled_mutation_marker_is_required_for_asset_and_link():
    require_postgresql()
    actor, _company, _department, _employee, _category, _leaf, asset, _attachment, link = pending_asset_context()

    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE assets_asset SET asset_status = 'draft', submitted_at = NULL, submitted_by_id = NULL WHERE id = %s",
                [asset.pk],
            )
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE assets_attachmentlink SET status = 'voided', void_reason = '无标记', voided_at = %s, voided_by_id = %s WHERE id = %s",
                [timezone.now(), actor.pk, link.pk],
            )

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                "UPDATE assets_asset SET asset_status = 'draft', submitted_at = NULL, submitted_by_id = NULL WHERE id = %s",
                [asset.pk],
            )
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                "UPDATE assets_attachmentlink SET status = 'voided', void_reason = '受控作废', voided_at = %s, voided_by_id = %s WHERE id = %s",
                [timezone.now(), actor.pk, link.pk],
            )
    asset.refresh_from_db()
    link.refresh_from_db()
    assert asset.asset_status == Asset.AssetStatus.DRAFT
    assert link.status == AttachmentLink.Status.VOIDED


def test_postgresql_requested_scheme_change_requires_controlled_marker():
    require_postgresql()
    actor, company, _department, _employee, _category, _leaf, asset, _attachment, _link = pending_asset_context()
    from tests.test_sprint3_support import make_structurally_valid_active_scheme

    scheme = make_structurally_valid_active_scheme(
        company=company, actor=actor, key="DBREQ"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE assets_asset SET requested_coding_scheme_id = %s WHERE id = %s",
                [scheme.pk, asset.pk],
            )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                "UPDATE assets_asset SET requested_coding_scheme_id = %s WHERE id = %s",
                [scheme.pk, asset.pk],
            )
    asset.refresh_from_db()
    assert asset.requested_coding_scheme_id == scheme.pk


@pytest.mark.parametrize(
    "mutation",
    [
        "category_inactive",
        "department_inactive",
        "employee_inactive",
        "employee_department",
        "location_inactive",
        "location_child",
        "custom_field",
        "attachment_unavailable",
        "attachment_scan",
        "attachment_mime",
    ],
)
def test_pending_finance_reverse_integrity_guards(mutation):
    require_postgresql()
    actor, company, department, employee, category, leaf, _asset, attachment, _link = pending_asset_context()
    custom_field = None
    if mutation == "custom_field":
        custom_field = make_custom_field(company, category, "DBCF", "text")

    with pytest.raises(IntegrityError), transaction.atomic():
        if mutation == "category_inactive":
            category.is_active = False
            category.save(update_fields=["is_active"])
        elif mutation == "department_inactive":
            department.is_active = False
            department.save(update_fields=["is_active"])
        elif mutation == "employee_inactive":
            employee.is_active = False
            employee.save(update_fields=["is_active"])
        elif mutation == "employee_department":
            other = make_department(company, "DBD2")
            employee.department = other
            employee.save(update_fields=["department"])
        elif mutation == "location_inactive":
            leaf.is_active = False
            leaf.save(update_fields=["is_active"])
        elif mutation == "location_child":
            make_location(company, "DBL4", parent=leaf)
        elif mutation == "custom_field":
            assert custom_field is not None
            custom_field.required = True
            custom_field.save(update_fields=["required"])
        elif mutation == "attachment_unavailable":
            attachment.is_available = False
            attachment.save(update_fields=["is_available"])
        elif mutation == "attachment_scan":
            attachment.malware_scan_status = Attachment.MalwareScanStatus.REJECTED
            attachment.save(update_fields=["malware_scan_status"])
        elif mutation == "attachment_mime":
            attachment.mime_type = "application/pdf"
            attachment.save(update_fields=["mime_type"])


def test_set_null_actors_preserves_submitted_and_voided_history():
    require_postgresql()
    actor, _company, _department, _employee, _category, _leaf, asset, _attachment, link = pending_asset_context()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                "UPDATE assets_asset SET asset_status = 'draft', submitted_at = NULL, submitted_by_id = NULL WHERE id = %s",
                [asset.pk],
            )
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                "UPDATE assets_attachmentlink SET status = 'voided', void_reason = '历史原因', voided_at = %s, voided_by_id = %s WHERE id = %s",
                [timezone.now(), actor.pk, link.pk],
            )
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
            cursor.execute(
                "UPDATE assets_asset SET asset_status = 'pending_finance', submitted_at = %s, submitted_by_id = %s WHERE id = %s",
                [timezone.now(), actor.pk, asset.pk],
            )
        # The pending asset now has no active photo. Add a replacement in the
        # same transaction before deferred validation runs.
        replacement = direct_attachment(
            asset.company, actor, key="private/assets/db-closure-replacement.jpg"
        )
        AttachmentLink.objects.create(
            company=asset.company,
            asset=asset,
            attachment=replacement,
            role=AttachmentLink.Role.PHOTO,
            security_class=AttachmentLink.SecurityClass.A0,
            created_by=actor,
        )

    actor.delete()
    asset.refresh_from_db()
    link.refresh_from_db()
    assert asset.asset_status == Asset.AssetStatus.PENDING_FINANCE
    assert asset.submitted_at is not None
    assert asset.submitted_by_id is None
    assert link.status == AttachmentLink.Status.VOIDED
    assert link.void_reason == "历史原因"
    assert link.voided_at is not None
    assert link.voided_by_id is None


def test_asset_delete_requires_service_and_attachment_link_is_never_deleted():
    require_postgresql()
    actor = make_user("db-delete", "equipment")
    company = make_company("DBDEL")
    category = make_category(company, "DBDELCAT")
    asset = direct_draft(company, category, actor=actor)
    attachment = direct_attachment(
        company, actor, key="private/assets/db-delete.jpg"
    )
    AttachmentLink.objects.create(
        company=company,
        asset=asset,
        attachment=attachment,
        role=AttachmentLink.Role.PHOTO,
        security_class=AttachmentLink.SecurityClass.A0,
        created_by=actor,
    )

    with pytest.raises(ValidationError, match="受控 Service"):
        asset.delete()
    with pytest.raises(ValidationError, match="受控 Service"):
        Asset.objects.filter(pk=asset.pk).delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM assets_asset WHERE id = %s", [asset.pk])
    assert Asset.objects.filter(pk=asset.pk).exists()

    link = asset.attachment_links.get()
    with pytest.raises(ValidationError, match="不得物理删除"):
        link.delete()
    with pytest.raises(ValidationError, match="不得物理删除"):
        AttachmentLink.objects.filter(pk=link.pk).delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM assets_attachmentlink WHERE id = %s", [link.pk]
            )
    assert AttachmentLink.objects.filter(pk=link.pk).exists()


def test_delete_asset_draft_service_audits_then_consumes_one_delete_capability():
    require_postgresql()
    actor = make_user("db-service-delete", "equipment")
    company = make_company("DBSDEL")
    complete_initialization(company, actor)
    category = make_category(company, "DBSDELCAT")
    asset = direct_draft(company, category, actor=actor)
    asset_id = asset.pk

    delete_asset_draft(actor=actor, asset=asset, reason="测试受控删除")

    assert not Asset.objects.filter(pk=asset_id).exists()
    audit = AuditLog.objects.get(
        action="asset_draft_delete", object_id=str(asset_id)
    )
    assert audit.old_data_json["reason"] == "测试受控删除"

    second = direct_draft(company, category, actor=actor)
    with pytest.raises(IntegrityError), transaction.atomic():
        # The trigger consumed the Service's marker; it cannot authorize a
        # second physical deletion in the surrounding test transaction.
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM assets_asset WHERE id = %s", [second.pk])
    assert Asset.objects.filter(pk=second.pk).exists()


def test_services_reject_forged_cross_company_instances_before_mutation(tmp_path):
    actor = make_user("db-forged", "equipment", "system_admin")
    current = make_company("DBCUR")
    complete_initialization(current, actor)
    department = make_department(current, "DBCURD")
    employee = make_employee(current, department, "DBCURE")
    category = make_category(current, "DBCURC")
    _site, _area, leaf = make_location_tree(current, "DBCURL")
    foreign = make_company("DBFOR", active=False)
    foreign_category = make_category(foreign, "DBFORC")
    foreign_department = make_department(foreign, "DBFORD")
    foreign_employee = make_employee(foreign, foreign_department, "DBFORE")
    _fsite, _farea, foreign_leaf = make_location_tree(foreign, "DBFORL")
    foreign_asset = direct_draft(
        foreign,
        foreign_category,
        actor=actor,
        department=foreign_department,
        responsible_employee=foreign_employee,
        location=foreign_leaf,
        unit="台",
    )
    # The caller forges the current company onto a real foreign primary key.
    forged = Asset(pk=foreign_asset.pk, company=current, category=category)
    data = complete_asset_data(category, department, employee, leaf)
    service_calls = (
        lambda: update_asset_draft(actor=actor, asset=forged, data=data),
        lambda: set_requested_coding_scheme(actor=actor, asset=forged, coding_scheme=None),
        lambda: submit_asset_for_finance(actor=actor, asset=forged),
        lambda: withdraw_asset_to_draft(actor=actor, asset=forged, reason="伪造"),
        lambda: delete_asset_draft(actor=actor, asset=forged, reason="伪造"),
    )
    for call in service_calls:
        with pytest.raises(PermissionDenied):
            call()
    with pytest.raises(PermissionDenied):
        upload_asset_attachment(
            actor=actor,
            asset=forged,
            uploaded_file=jpeg_upload(),
            role=AttachmentLink.Role.PHOTO,
            security_class=AttachmentLink.SecurityClass.A0,
        )

    attachment = direct_attachment(
        foreign, actor, key="private/assets/db-forged.jpg"
    )
    link = AttachmentLink.objects.create(
        company=foreign,
        asset=foreign_asset,
        attachment=attachment,
        role=AttachmentLink.Role.OTHER,
        security_class=AttachmentLink.SecurityClass.A1,
        created_by=actor,
    )
    forged_link = AttachmentLink(pk=link.pk, company=current, asset=forged)
    with pytest.raises(PermissionDenied):
        void_asset_attachment(actor=actor, link=forged_link, reason="伪造")
    foreign_asset.refresh_from_db()
    link.refresh_from_db()
    assert foreign_asset.asset_status == Asset.AssetStatus.DRAFT
    assert link.status == AttachmentLink.Status.ACTIVE


def test_attachment_link_identity_security_and_availability_are_database_guarded():
    require_postgresql()
    actor = make_user("db-link-immutable", "finance")
    company = make_company("DBLIMM")
    category = make_category(company, "DBLIMMC")
    asset = direct_draft(company, category, actor=actor)
    attachment = direct_attachment(
        company, actor, key="private/assets/db-link-immutable.pdf",
        filename="immutable.pdf", mime="application/pdf", data=b"%PDF-1.7\n",
    )
    link = AttachmentLink.objects.create(
        company=company,
        asset=asset,
        attachment=attachment,
        role=AttachmentLink.Role.OTHER,
        security_class=AttachmentLink.SecurityClass.A1,
        created_by=actor,
    )

    link.security_class = AttachmentLink.SecurityClass.A0
    with pytest.raises(ValidationError, match="不可修改"):
        link.save(update_fields=["security_class"])
    with pytest.raises(ValidationError, match="不可修改"):
        AttachmentLink.objects.filter(pk=link.pk).update(security_class="A0")
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE assets_attachmentlink SET security_class = 'A0' WHERE id = %s",
                [link.pk],
            )

    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE masterdata_attachment SET malware_scan_status = 'pending', is_available = TRUE WHERE id = %s",
                [attachment.pk],
            )


def test_fresh_postgresql_guards_allow_legal_submit_and_withdraw():
    require_postgresql()
    actor = make_user("db-legal-state", "equipment")
    company = make_company("DBLEGAL")
    complete_initialization(company, actor)
    department = make_department(company, "DBLEGALD")
    employee = make_employee(company, department, "DBLEGALE")
    category = make_category(company, "DBLEGALC")
    _site, _area, leaf = make_location_tree(company, "DBLEGALL")
    asset = direct_draft(
        company,
        category,
        actor=actor,
        asset_name="合法触发器状态测试",
        unit="台",
        department=department,
        responsible_employee=employee,
        location=leaf,
    )
    attachment = direct_attachment(
        company, actor, key="private/assets/db-legal-state.jpg"
    )
    AttachmentLink.objects.create(
        company=company,
        asset=asset,
        attachment=attachment,
        role=AttachmentLink.Role.PHOTO,
        security_class=AttachmentLink.SecurityClass.A0,
        created_by=actor,
    )

    pending = submit_asset_for_finance(actor=actor, asset=asset)
    draft = withdraw_asset_to_draft(
        actor=actor, asset=pending, reason="合法回退触发器验证"
    )

    assert draft.asset_status == Asset.AssetStatus.DRAFT
    assert AuditLog.objects.filter(action="asset_submit_finance").exists()
    assert AuditLog.objects.filter(action="asset_withdraw_to_draft").exists()
