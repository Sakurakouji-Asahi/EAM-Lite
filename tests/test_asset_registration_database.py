from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal

import pytest
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from apps.assets.models import Asset, AssetRegistration, AssetQrIdentity
from apps.assets.registration import create_registered_asset
from apps.finance.models import FinanceFormalizationRequest
from apps.masterdata.models import IssuedCode
from tests.test_asset_registration_finance_separation import context, physical_data, registered
from tests.test_sprint4_acceptance import _pending_asset, _confirm_nonfixed


pytestmark = pytest.mark.django_db(transaction=True)


def test_postgresql_registration_is_immutable_and_cannot_be_removed(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL trigger acceptance")
    asset = registered(context)
    record = asset.registration
    for sql, params in (
        ("UPDATE assets_assetregistration SET request_hash=%s WHERE id=%s", ["f" * 64, record.pk]),
        ("DELETE FROM assets_assetregistration WHERE id=%s", [record.pk]),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
    record.refresh_from_db()
    assert record.request_hash != "f" * 64


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_physical_registration_has_unique_codes_and_exact_replay(context, same_key):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL locking and concurrency acceptance")
    barrier = Barrier(2)
    data = physical_data(context)

    def create(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            asset = create_registered_asset(
                actor=context["equipment"], company=context["company"], data=data,
                idempotency_key="concurrent" if same_key else f"concurrent-{index}",
            )
            return asset.pk, asset.asset_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    expected = 1 if same_key else 2
    assert len(set(results)) == expected
    assert Asset.objects.count() == AssetRegistration.objects.count() == IssuedCode.objects.count() == expected


def test_upgrade_backfills_legacy_registration_without_changing_asset_finance_or_qr(context):
    asset = _pending_asset(context, "legacy-upgrade")
    _confirm_nonfixed(context, asset, cost=Decimal("1200.00"), key="legacy-upgrade")
    asset.refresh_from_db()
    request = FinanceFormalizationRequest.objects.get(asset=asset)
    before = (asset.pk, asset.asset_code, asset.current_issued_code_id, request.result_finance_id)
    qr = AssetQrIdentity.objects.get(asset=asset, status="active")
    token = qr.public_token
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("assets", "0014_production_location_leaf_guard")])
        executor = MigrationExecutor(connection)
        executor.migrate([("assets", "0015_physical_registration")])
        historical = executor.loader.project_state([("assets", "0015_physical_registration")]).apps
        old_asset = historical.get_model("assets", "Asset").objects.get(pk=asset.pk)
        record = historical.get_model("assets", "AssetRegistration").objects.get(asset_id=asset.pk)
        assert record.source == "legacy_finance"
        assert record.result_issued_code_id == request.result_issued_code_id
        assert record.registered_at == request.completed_at
        finance = historical.get_model("finance", "AssetFinance").objects.get(asset_id=asset.pk)
        assert (old_asset.pk, old_asset.asset_code, old_asset.current_issued_code_id, finance.pk) == before
        assert historical.get_model("assets", "AssetQrIdentity").objects.get(pk=qr.pk).public_token == token
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_reverse_refuses_to_discard_new_physical_registration_history(context):
    asset = registered(context)
    code = asset.asset_code
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="独立实物建档历史"):
            executor.migrate([("assets", "0014_production_location_leaf_guard")])
    finally:
        MigrationExecutor(connection).migrate(latest)
    assert MigrationRecorder.Migration.objects.filter(app="assets", name="0015_physical_registration").exists()
    asset.refresh_from_db()
    assert asset.asset_code == code and AssetRegistration.objects.filter(asset=asset).exists()
