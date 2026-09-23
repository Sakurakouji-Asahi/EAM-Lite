from importlib import import_module

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.assets.models import Asset, AssetCustodyReturn, AssetMovement
from apps.assets.services import _controlled_update
from apps.audit.models import AuditLog
from tests.test_asset_workflow_improvements import planned, undo, receipt
from tests.test_unified_asset_identity import context

pytestmark = pytest.mark.django_db(transaction=True)


def test_migration_closes_legacy_plan_and_preserves_original_return(context):
    asset, plan=planned(context)
    executor=MigrationExecutor(connection)
    leaves=executor.loader.graph.leaf_nodes()
    target=[("assets","0019_leased_custody_return")]
    executor.migrate(target)
    old_apps=executor.loader.project_state(target).apps
    Returns=old_apps.get_model("assets","AssetCustodyReturn")
    try:
        with transaction.atomic():
            movement=AssetMovement(company=asset.company,asset=asset,movement_type="custody_return",effective_at=timezone.now(),
                from_department=asset.department,to_department=asset.department,from_employee=asset.responsible_employee,to_employee=asset.responsible_employee,
                from_location=asset.location,to_location=asset.location,from_status="in_use",to_status="other_disposed",reason="历史归还",idempotency_key="legacy-return",operated_by=context["equipment"])
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('eam_lite.controlled_asset_movement_insert','on',true)")
            movement.save()
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('eam_lite.controlled_asset_custody_return','on',true)")
            old=Returns.objects.create(company_id=asset.company_id,asset_id=asset.pk,issued_code_id=asset.current_issued_code_id,
                movement_id=movement.pk,returned_on=timezone.localdate(),counterparty="历史接收方",contract_reference="HIST-01",
                acceptance_evidence="历史交接单",reason="历史归还",idempotency_key="legacy-return",request_hash="a"*64,recorded_by_id=context["equipment"].pk)
            _controlled_update(Asset,pk=asset.pk,values={"asset_status":"other_disposed"})
        original=Returns.objects.filter(pk=old.pk).values().get()
        movement_before=AssetMovement.objects.filter(pk=movement.pk).values().get()
    finally:
        MigrationExecutor(connection).migrate(leaves)
    fresh=AssetCustodyReturn.objects.get(pk=old.pk)
    plan.refresh_from_db()
    assert plan.status == "ended" and fresh.maintenance_plan_states == [{"id":str(plan.pk),"status":"active"}]
    assert {key:getattr(fresh,key) for key in original} == original
    assert AssetMovement.objects.filter(pk=movement.pk).values().get() == movement_before
    assert AuditLog.objects.filter(action="migration.custody_return_maintenance_closed",object_id=str(plan.pk)).count() == 1
    undo(context,fresh)
    plan.refresh_from_db()
    assert plan.status == "active"


def test_empty_correction_migrations_round_trip():
    executor=MigrationExecutor(connection)
    leaves=executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("assets","0019_leased_custody_return")])
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert not MigrationExecutor(connection).migration_plan(leaves)


def test_downgrade_refuses_to_drop_reversal_evidence(context):
    from tests.test_unified_asset_identity import registered
    asset=registered(context,management_attribute="LS")
    undo(context,receipt(context,asset))
    migration=import_module("apps.assets.migrations.0021_custody_correction_guards")
    state=MigrationExecutor(connection).loader.project_state().apps
    with pytest.raises(RuntimeError,match="不能降级"):
        with connection.schema_editor() as schema_editor:
            migration.uninstall(state,schema_editor)
