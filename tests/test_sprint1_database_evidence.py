import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.masterdata.models import (
    AssetCategory,
    Attachment,
    Company,
    Department,
    Employee,
    ImportBatch,
    ImportRow,
    Location,
    SystemSetting,
)


pytestmark = pytest.mark.django_db


def test_acceptance_database_is_postgresql_with_integrity_triggers():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 1 database acceptance requires PostgreSQL")
    assert connection.vendor == "postgresql"
    assert connection.settings_dict["ENGINE"] == "django.db.backends.postgresql"
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        assert cursor.fetchone()[0].startswith("18.6")
        cursor.execute(
            "SELECT count(*) FROM pg_trigger "
            "WHERE tgname LIKE 'trg_%' AND NOT tgisinternal"
        )
        assert cursor.fetchone()[0] >= 16


def test_audit_log_rejects_raw_sql_update_and_delete():
    if connection.vendor != "postgresql":
        pytest.skip("audit append-only database guard requires PostgreSQL")
    company = Company.objects.create(
        code="AUDIT-GUARD",
        normalized_code="audit-guard",
        name="审计保护公司",
        short_name="审计保护",
    )
    audit = AuditLog.objects.create(
        company=company,
        action="masterdata.create",
        object_type="Company",
        object_id=str(company.pk),
        new_data_json={"code": company.code},
    )

    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE audit_auditlog SET action = %s WHERE id = %s",
            ["tampered", audit.pk],
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM audit_auditlog WHERE id = %s", [audit.pk])

    audit.refresh_from_db()
    assert audit.action == "masterdata.create"


def test_audit_log_allows_only_foreign_key_actor_set_null():
    if connection.vendor != "postgresql":
        pytest.skip("audit actor SET_NULL database guard requires PostgreSQL")
    company = Company.objects.create(
        code="AUDIT-ACTOR",
        normalized_code="audit-actor",
        name="审计操作人公司",
        short_name="操作人公司",
    )
    user = get_user_model().objects.create_user(
        username="audit-actor",
        password="Test-Password-2026!",
        display_name="待删除操作人",
    )
    audit = AuditLog.objects.create(
        company=company,
        user=user,
        action="security.example",
        object_type="Example",
        object_id="actor-set-null",
        new_data_json={"safe": True},
    )

    user.delete()

    audit.refresh_from_db()
    assert audit.user_id is None
    assert audit.action == "security.example"
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE audit_auditlog SET user_id = NULL, action = %s WHERE id = %s",
            ["tampered", audit.pk],
        )


def test_confirmed_import_evidence_rejects_queryset_and_raw_sql_mutation():
    if connection.vendor != "postgresql":
        pytest.skip("confirmed import evidence guards require PostgreSQL")
    company = Company.objects.create(
        code="EVIDENCE",
        normalized_code="evidence",
        name="证据公司",
        short_name="证据",
    )
    user = get_user_model().objects.create_user(
        username="evidence-user",
        password="Test-Password-2026!",
        display_name="证据用户",
    )
    digest = "a" * 64
    attachment = Attachment.objects.create(
        company=company,
        storage_key="imports/evidence.xlsx",
        original_filename="evidence.xlsx",
        safe_filename="evidence.xlsx",
        file_size=128,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256=digest,
        malware_scan_status="clean",
        is_available=True,
    )
    batch = ImportBatch.objects.create(
        company=company,
        import_type="department",
        template_version="1.0",
        file_attachment=attachment,
        file_sha256=digest,
        status="confirmed",
        total_rows=1,
        valid_rows=1,
        error_rows=0,
        warning_rows=0,
        request_hash="b" * 64,
        idempotency_key="confirmed-evidence",
        uploaded_by=user,
        validated_at=timezone.now(),
        confirmed_by=user,
        confirmed_at=timezone.now(),
    )
    row = ImportRow.objects.create(
        batch=batch,
        row_number=1,
        raw_data_json={"code": "D1"},
        normalized_data_json={"normalized_code": "d1"},
        validation_status="created",
        created_object_type="Department",
        created_object_id="1",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ImportBatch.objects.filter(pk=batch.pk).update(status="validated")
    with pytest.raises(IntegrityError), transaction.atomic():
        ImportRow.objects.filter(pk=row.pk).update(
            validation_status="valid", created_object_type="", created_object_id=""
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Attachment.objects.filter(pk=attachment.pk).update(
            storage_key="imports/replaced.xlsx"
        )

    for table, object_id in (
        ("masterdata_importrow", row.pk),
        ("masterdata_importbatch", batch.pk),
        ("masterdata_attachment", attachment.pk),
    ):
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f'DELETE FROM "{table}" WHERE id = %s', [object_id])


def test_database_rejects_values_outside_fixed_enums():
    if connection.vendor != "postgresql":
        pytest.skip("fixed enum database checks require PostgreSQL")
    company = Company.objects.create(
        code="ENUM", normalized_code="enum", name="枚举公司", short_name="枚举"
    )
    department = Department.objects.create(
        company=company, code="D1", normalized_code="d1", name="部门"
    )
    employee = Employee.objects.create(
        company=company,
        department=department,
        employee_no="E1",
        normalized_employee_no="e1",
        name="员工",
    )
    location = Location.objects.create(
        company=company,
        code="L1",
        normalized_code="l1",
        name="位置",
        location_type="site",
    )
    category = AssetCategory.objects.create(
        company=company,
        code="A1",
        normalized_code="a1",
        name="分类",
        category_type="equipment",
    )
    attachment = Attachment.objects.create(
        company=company,
        storage_key="imports/enum.xlsx",
        original_filename="enum.xlsx",
        safe_filename="enum.xlsx",
        file_size=1,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256="c" * 64,
        malware_scan_status="pending",
    )
    setting = SystemSetting.objects.create(
        company=company,
        key="attachment_max_size_bytes",
        value="1024",
        value_type="integer",
        description="附件上限",
    )

    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE masterdata_employee SET employment_status = %s WHERE id = %s",
            ["unknown", employee.pk],
        )

    invalid_updates = (
        (Location, location.pk, {"location_type": "unknown"}),
        (AssetCategory, category.pk, {"category_type": "unknown"}),
        (Attachment, attachment.pk, {"malware_scan_status": "unknown"}),
        (SystemSetting, setting.pk, {"value_type": "unknown"}),
    )
    for model, object_id, values in invalid_updates:
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.filter(pk=object_id).update(**values)


def test_database_rejects_non_finite_and_negative_decimal_setting():
    if connection.vendor != "postgresql":
        pytest.skip("decimal setting database guard requires PostgreSQL")
    company = Company.objects.create(
        code="DECIMAL-GUARD",
        normalized_code="decimal-guard",
        name="数值保护公司",
        short_name="数值保护",
    )
    setting = SystemSetting.objects.create(
        company=company,
        key="fixed_asset_warning_amount",
        value="5000.00",
        value_type="decimal",
        description="固定资产认定提示金额",
    )

    for invalid_value in ("NaN", "Infinity", "-Infinity", "-0.01"):
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE masterdata_systemsetting SET value = %s WHERE id = %s",
                [invalid_value, setting.pk],
            )

    setting.refresh_from_db()
    assert setting.value == "5000.00"


def test_reverse_company_reference_guards_reject_existing_links():
    if connection.vendor != "postgresql":
        pytest.skip("reverse company reference guards require PostgreSQL")
    c1 = Company.objects.create(
        code="RC1", normalized_code="rc1", name="原公司", short_name="原"
    )
    c2 = Company.objects.create(
        code="RC2",
        normalized_code="rc2",
        name="目标公司",
        short_name="目标",
        is_active=False,
    )
    d1 = Department.objects.create(
        company=c1, code="D1", normalized_code="d1", name="原部门"
    )
    Department.objects.create(
        company=c1, code="D1C", normalized_code="d1c", name="下级部门", parent=d1
    )
    d2 = Department.objects.create(
        company=c2, code="D2", normalized_code="d2", name="目标部门"
    )
    location = Location.objects.create(
        company=c1,
        code="L1",
        normalized_code="l1",
        name="父位置",
        location_type="site",
    )
    Location.objects.create(
        company=c1,
        code="L2",
        normalized_code="l2",
        name="子位置",
        location_type="position",
        parent=location,
    )
    category = AssetCategory.objects.create(
        company=c1,
        code="A1",
        normalized_code="a1",
        name="父分类",
        category_type="equipment",
    )
    AssetCategory.objects.create(
        company=c1,
        code="A2",
        normalized_code="a2",
        name="子分类",
        category_type="tool",
        parent=category,
    )
    manager = Employee.objects.create(
        company=c1,
        department=d1,
        employee_no="M1",
        normalized_employee_no="m1",
        name="经理",
    )
    managed = Department.objects.create(
        company=c1,
        code="MNG",
        normalized_code="mng",
        name="被管理部门",
        manager_employee=manager,
    )
    attachment = Attachment.objects.create(
        company=c1,
        storage_key="imports/reverse.xlsx",
        original_filename="reverse.xlsx",
        safe_filename="reverse.xlsx",
        file_size=1,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256="d" * 64,
        malware_scan_status="pending",
    )
    user = get_user_model().objects.create_user(
        username="reverse-company-user",
        password="Test-Password-2026!",
        display_name="反向公司用户",
    )
    ImportBatch.objects.create(
        company=c1,
        import_type="department",
        template_version="department-v1",
        file_attachment=attachment,
        file_sha256=attachment.sha256,
        request_hash="e" * 64,
        idempotency_key="reverse-company",
        uploaded_by=user,
    )

    guarded_updates = (
        (Department, d1.pk, {"company": c2}),
        (Location, location.pk, {"company": c2}),
        (AssetCategory, category.pk, {"company": c2}),
        (Employee, manager.pk, {"company": c2, "department": d2}),
        (Attachment, attachment.pk, {"company": c2}),
    )
    for model, object_id, values in guarded_updates:
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.filter(pk=object_id).update(**values)

    managed.refresh_from_db()
    assert managed.manager_employee_id == manager.pk
