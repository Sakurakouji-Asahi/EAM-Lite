from __future__ import annotations

import hashlib

import pytest
from django.db import IntegrityError, connection, transaction

from apps.assets.models import AssetExternalReference
from apps.audit.models import AuditLog
from apps.reports.models import ExportLog
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def actor_rows():
    context, asset, _qr = active_asset_context("CORRACTOR")
    actor = make_user("correction-actor")
    audit_log = AuditLog.objects.create(
        company=context["company"],
        user=actor,
        action="correction.actor",
        object_type="Asset",
        object_id=str(asset.pk),
        old_data_json={},
        new_data_json={},
    )
    export_log = ExportLog.objects.create(
        company=context["company"],
        export_type=ExportLog.ExportType.ASSET_LEDGER,
        filters_json={},
        request_hash=hashlib.sha256(b"correction-actor").hexdigest(),
        idempotency_key="correction-actor",
        requested_by=actor,
    )
    external_reference = AssetExternalReference.objects.create(
        company=context["company"],
        asset=asset,
        external_system=AssetExternalReference.ExternalSystem.TPLUS,
        reference_type=AssetExternalReference.ReferenceType.ASSET_CARD_CODE,
        reference_value="CORR-ACTOR-CARD",
        created_by=actor,
    )
    return actor, (audit_log, export_log, external_reference)


def test_postgresql_rejects_each_independent_raw_actor_clear(actor_rows):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred actor guard only")
    actor, rows = actor_rows
    probes = (
        ("audit_auditlog", "user_id", rows[0]),
        ("reports_exportlog", "requested_by_id", rows[1]),
        ("assets_assetexternalreference", "created_by_id", rows[2]),
    )
    for table, column, row in probes:
        with pytest.raises(IntegrityError, match="actor can only be cleared"):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE {table} SET {column} = NULL WHERE id = %s",
                        [row.pk],
                    )
        row.refresh_from_db()
        assert getattr(row, column) == actor.pk


def test_postgresql_real_user_delete_allows_three_set_null_updates(actor_rows):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred actor guard only")
    actor, rows = actor_rows
    actor_pk = actor.pk

    with transaction.atomic():
        actor.delete()

    assert not type(actor)._base_manager.filter(pk=actor_pk).exists()
    for row, field in zip(rows, ("user_id", "requested_by_id", "created_by_id")):
        row.refresh_from_db()
        assert getattr(row, field) is None
