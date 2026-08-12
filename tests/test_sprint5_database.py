from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction

from apps.assets.models import Asset
from apps.imports.services import upload_and_validate_import
from apps.masterdata.models import ImportBatch, ImportRow
from tests.test_sprint5_support import (
    asset_workbook_upload,
    physical_row,
    sprint5_context,
)


pytestmark = pytest.mark.django_db(transaction=True)


def test_sprint5_database_constraints_reject_unapproved_import_and_initialization_values(
    settings, tmp_path
):
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 5 database constraints require PostgreSQL")
    settings.MEDIA_ROOT = tmp_path / "media"
    company, actor, category, department, employee, location = sprint5_context(
        prefix="S5DB"
    )
    batch = upload_and_validate_import(
        actor=actor,
        company=company,
        import_type="asset_initialization",
        uploaded_file=asset_workbook_upload(
            company,
            [physical_row(company, category, department, employee, location)],
        ),
        idempotency_key="sprint5-database-constraints",
    )
    assert batch.status == "validated"

    with pytest.raises(IntegrityError), transaction.atomic():
        ImportBatch.objects.filter(pk=batch.pk).update(import_type="unapproved")

    asset = Asset.objects.create(
        company=company,
        asset_name="数据库约束资产",
        category=category,
        quantity=1,
        department=department,
        responsible_employee=employee,
        location=location,
        initialized_by=actor,
        created_by=actor,
        updated_by=actor,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Asset.objects.filter(pk=asset.pk).update(initialization_source="unapproved")

    row = batch.rows.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        ImportRow.objects.filter(pk=row.pk).update(
            normalized_data_json={"tampered": True}
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM masterdata_importrow WHERE id = %s", [row.pk]
            )
