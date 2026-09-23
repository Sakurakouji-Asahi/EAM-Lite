"""Append-only reversals, active-edge guards and traceable legacy plan closure."""
from importlib import import_module
from django.db import migrations


def replace_once(sql, old, new):
    if sql.count(old) != 1:
        raise RuntimeError("Unexpected predecessor for custody correction guard")
    return sql.replace(old, new, 1)


def functions():
    previous = import_module("apps.assets.migrations.0019_leased_custody_return")
    references, movement = previous.current_functions()
    references = replace_once(references, "returned.movement_id IS NOT NULL", "returned.movement_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM assets_assetcustodyreturnreversal r WHERE r.custody_return_id=returned.id)")
    marker = "OR (OLD.asset_status='pending_label' AND NEW.asset_status IN ('in_use','idle'))"
    references = replace_once(references, marker, marker + """
            OR (OLD.asset_status='other_disposed' AND NEW.asset_status IN ('pending_label','in_use','idle','under_repair')
                AND EXISTS (SELECT 1 FROM assets_assetcustodyreturnreversal r
                    JOIN assets_assetcustodyreturn c ON c.id=r.custody_return_id
                    JOIN assets_assetmovement m ON m.id=r.movement_id
                    WHERE c.asset_id=NEW.id AND m.to_status=NEW.asset_status
                      AND m.id=(SELECT id FROM assets_assetmovement WHERE asset_id=NEW.id ORDER BY created_at DESC,id DESC LIMIT 1)))
    """)
    movement = replace_once(movement, "'disposal_complete','disposal_reversal','custody_return'",
                            "'disposal_complete','disposal_reversal','custody_return','custody_return_reversal'")
    movement = replace_once(movement, '    RETURN NEW;\nEND;\n$$;', """    IF NEW.movement_type='custody_return_reversal' AND NOT (
        NEW.from_status='other_disposed' AND NEW.to_status IN ('pending_label','in_use','idle','under_repair')
        AND ROW(NEW.from_department_id,NEW.from_employee_id,NEW.from_location_id)
            IS NOT DISTINCT FROM ROW(NEW.to_department_id,NEW.to_employee_id,NEW.to_location_id)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid custody reversal movement'; END IF;
    RETURN NEW;
END;
$$;""")
    trace = import_module("apps.assets.migrations.0018_asset_origin_and_composition").TRACE_SQL.split("CREATE TRIGGER")[0]
    trace = replace_once(trace, "WHERE edge.company_id=NEW.company_id", "WHERE edge.company_id=NEW.company_id AND NOT EXISTS (SELECT 1 FROM assets_assetoriginreversal r WHERE r.origin_id=edge.id)")
    trace = replace_once(trace, "        IF EXISTS (WITH RECURSIVE reach(id) AS (", """        IF EXISTS (SELECT 1 FROM assets_assetoriginlink edge WHERE edge.company_id=NEW.company_id
            AND edge.source_asset_id=NEW.source_asset_id AND edge.target_asset_id=NEW.target_asset_id
            AND edge.relation_type=NEW.relation_type AND NOT EXISTS (SELECT 1 FROM assets_assetoriginreversal r WHERE r.origin_id=edge.id)) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='active origin relation already exists'; END IF;
        IF EXISTS (WITH RECURSIVE reach(id) AS (""")
    return_guard = previous.RETURN_SQL.split("CREATE TRIGGER")[0]
    return_guard = replace_once(return_guard, "    IF NEW.composition_revision_id IS NOT NULL THEN", """    PERFORM 1 FROM masterdata_company WHERE id=NEW.company_id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM assets_assetcustodyreturn c WHERE c.asset_id=NEW.asset_id
        AND NOT EXISTS (SELECT 1 FROM assets_assetcustodyreturnreversal r WHERE r.custody_return_id=c.id)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='asset already has an active custody return'; END IF;
    IF jsonb_typeof(NEW.maintenance_plan_states) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid maintenance closure snapshot'; END IF;
    IF NEW.composition_revision_id IS NOT NULL THEN""")
    return references, movement, trace, return_guard


REVERSAL_SQL = r'''
CREATE OR REPLACE FUNCTION assets_guard_trace_reversal()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c assets_assetcustodyreturn%ROWTYPE; a assets_asset%ROWTYPE; m assets_assetmovement%ROWTYPE;
        original assets_assetmovement%ROWTYPE; ref_company bigint;
BEGIN
    IF TG_OP='UPDATE' AND OLD.recorded_by_id IS NOT NULL AND NEW.recorded_by_id IS NULL
       AND (to_jsonb(NEW)-'recorded_by_id')=(to_jsonb(OLD)-'recorded_by_id') THEN RETURN NEW; END IF;
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='trace reversal is immutable history'; END IF;
    IF COALESCE(current_setting('eam_lite.controlled_asset_trace_reversal',true),'')<>'on'
       OR NEW.recorded_by_id IS NULL OR btrim(NEW.reason)='' OR btrim(NEW.idempotency_key)=''
       OR NEW.request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='trace reversal requires controlled attributable evidence'; END IF;
    PERFORM set_config('eam_lite.controlled_asset_trace_reversal','off',true);
    PERFORM 1 FROM masterdata_company WHERE id=NEW.company_id FOR UPDATE;
    IF TG_TABLE_NAME='assets_assetoriginreversal' THEN
        SELECT company_id INTO ref_company FROM assets_assetoriginlink WHERE id=NEW.origin_id;
        IF ref_company IS DISTINCT FROM NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='origin reversal company mismatch'; END IF;
    ELSE
        SELECT * INTO c FROM assets_assetcustodyreturn WHERE id=NEW.custody_return_id;
        IF NOT FOUND OR c.company_id<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody reversal company mismatch'; END IF;
        SELECT * INTO a FROM assets_asset WHERE id=c.asset_id;
        IF a.record_status<>'active' THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody reversal requires a visible asset'; END IF;
        IF c.movement_id IS NULL THEN
            IF NEW.movement_id IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='evidence reversal cannot restore financial disposal'; END IF;
        ELSE
            SELECT * INTO original FROM assets_assetmovement WHERE id=c.movement_id;
            SELECT * INTO m FROM assets_assetmovement WHERE id=NEW.movement_id;
            IF NOT FOUND OR a.asset_status<>'other_disposed' OR m.asset_id<>a.id OR m.company_id<>NEW.company_id
               OR m.movement_type<>'custody_return_reversal' OR m.from_status<>'other_disposed' OR m.to_status<>original.from_status
               OR ROW(m.from_department_id,m.from_employee_id,m.from_location_id)
                  IS DISTINCT FROM ROW(original.to_department_id,original.to_employee_id,original.to_location_id) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid custody return reversal'; END IF;
        END IF;
    END IF;
    RETURN NEW;
END; $$;
CREATE TRIGGER trg_origin_reversal_history BEFORE INSERT OR UPDATE OR DELETE ON assets_assetoriginreversal
FOR EACH ROW EXECUTE FUNCTION assets_guard_trace_reversal();
CREATE TRIGGER trg_custody_reversal_history BEFORE INSERT OR UPDATE OR DELETE ON assets_assetcustodyreturnreversal
FOR EACH ROW EXECUTE FUNCTION assets_guard_trace_reversal();

CREATE OR REPLACE FUNCTION assets_validate_custody_return_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c assets_assetcustodyreturn%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME='assets_assetmovement' THEN
        IF NEW.movement_type<>'custody_return' THEN RETURN NULL; END IF;
        SELECT * INTO c FROM assets_assetcustodyreturn WHERE movement_id=NEW.id AND asset_id=NEW.asset_id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody return movement needs its durable result'; END IF;
    ELSE
        IF TG_OP='UPDATE' THEN RETURN NULL; END IF;
        SELECT * INTO c FROM assets_assetcustodyreturn WHERE id=NEW.id;
    END IF;
    IF EXISTS (SELECT 1 FROM assets_assetcustodyreturnreversal WHERE custody_return_id=c.id) THEN RETURN NULL; END IF;
    IF NOT EXISTS (SELECT 1 FROM assets_asset WHERE id=c.asset_id AND asset_status='other_disposed')
       OR EXISTS (SELECT 1 FROM maintenance_maintenanceplan WHERE asset_id=c.asset_id AND status IN ('active','suspended')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody return must close physical management and maintenance'; END IF;
    RETURN NULL;
END; $$;

CREATE OR REPLACE FUNCTION assets_validate_custody_reversal_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r assets_assetcustodyreturnreversal%ROWTYPE; c assets_assetcustodyreturn%ROWTYPE; m assets_assetmovement%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME='assets_assetmovement' THEN
        IF NEW.movement_type<>'custody_return_reversal' THEN RETURN NULL; END IF;
        SELECT * INTO r FROM assets_assetcustodyreturnreversal WHERE movement_id=NEW.id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody reversal movement needs its durable result'; END IF;
    ELSE
        IF TG_OP='UPDATE' THEN RETURN NULL; END IF;
        r:=NEW;
    END IF;
    IF r.movement_id IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO c FROM assets_assetcustodyreturn WHERE id=r.custody_return_id;
    SELECT * INTO m FROM assets_assetmovement WHERE id=r.movement_id;
    IF NOT EXISTS (SELECT 1 FROM assets_asset WHERE id=c.asset_id AND asset_status=m.to_status)
       AND NOT EXISTS (SELECT 1 FROM assets_assetcustodyreturn fresh WHERE fresh.asset_id=c.asset_id AND fresh.id<>c.id
           AND NOT EXISTS (SELECT 1 FROM assets_assetcustodyreturnreversal v WHERE v.custody_return_id=fresh.id)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody reversal must restore physical state'; END IF;
    RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER trg_custody_reversal_commit AFTER INSERT OR UPDATE ON assets_assetcustodyreturnreversal
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assets_validate_custody_reversal_commit();
CREATE CONSTRAINT TRIGGER trg_custody_reversal_movement_commit AFTER INSERT ON assets_assetmovement
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assets_validate_custody_reversal_commit();
'''


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        for sql in (*functions(), REVERSAL_SQL):
            schema_editor.execute(sql, params=None)


def close_legacy_plans(apps, schema_editor):
    """Populate only the newly added snapshot; original custody facts are intact."""
    Returns = apps.get_model("assets", "AssetCustodyReturn")
    Plans = apps.get_model("maintenance", "MaintenancePlan")
    Audit = apps.get_model("audit", "AuditLog")
    alias = schema_editor.connection.alias
    pg = schema_editor.connection.vendor == "postgresql"
    strict_guard = functions()[-1] if pg else ""
    if pg:
        temporary = replace_once(strict_guard, "    IF TG_OP<>'INSERT' THEN", """    IF TG_OP='UPDATE' AND COALESCE(current_setting('eam_lite.migration_custody_plan_snapshot',true),'')='on'
           AND OLD.maintenance_plan_states='[]'::jsonb
           AND (to_jsonb(NEW)-'maintenance_plan_states')=(to_jsonb(OLD)-'maintenance_plan_states') THEN RETURN NEW; END IF;
    IF TG_OP<>'INSERT' THEN""")
        schema_editor.execute(temporary, params=None)
        schema_editor.execute("SELECT set_config('eam_lite.migration_custody_plan_snapshot','on',true)")
    for receipt in Returns.objects.using(alias).filter(movement__isnull=False, maintenance_plan_states=[]).iterator():
        plans = list(Plans.objects.using(alias).filter(asset_id=receipt.asset_id, status__in=("active", "suspended")))
        if not plans:
            continue
        snapshot = [{"id": str(plan.pk), "status": plan.status} for plan in plans]
        Returns.objects.using(alias).filter(pk=receipt.pk).update(maintenance_plan_states=snapshot)
        for plan in plans:
            if pg:
                schema_editor.execute("SELECT set_config('eam_lite.controlled_maintenance_plan_mutation','on',true)")
            Plans.objects.using(alias).filter(pk=plan.pk).update(status="ended", ended_reason="other", ended_at=receipt.recorded_at)
            Audit.objects.using(alias).create(company_id=receipt.company_id, action="migration.custody_return_maintenance_closed",
                object_type="MaintenancePlan", object_id=str(plan.pk), old_data_json={"status":plan.status},
                new_data_json={"status":"ended", "custody_return":str(receipt.pk), "migration":"assets.0021"})
    if pg:
        schema_editor.execute("SELECT set_config('eam_lite.migration_custody_plan_snapshot','off',true)")
        schema_editor.execute(strict_guard, params=None)


def uninstall(apps, schema_editor):
    if (apps.get_model("assets", "AssetOriginReversal").objects.exists()
            or apps.get_model("assets", "AssetCustodyReturnReversal").objects.exists()
            or apps.get_model("assets", "AssetCustodyReturn").objects.exclude(maintenance_plan_states=[]).exists()):
        raise RuntimeError("已有撤销或保养联动记录，不能降级丢弃历史；请使用升级前备份恢复。")
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute('''
DROP TRIGGER IF EXISTS trg_origin_reversal_history ON assets_assetoriginreversal;
DROP TRIGGER IF EXISTS trg_custody_reversal_history ON assets_assetcustodyreturnreversal;
DROP TRIGGER IF EXISTS trg_custody_reversal_commit ON assets_assetcustodyreturnreversal;
DROP TRIGGER IF EXISTS trg_custody_reversal_movement_commit ON assets_assetmovement;
DROP FUNCTION IF EXISTS assets_guard_trace_reversal();
DROP FUNCTION IF EXISTS assets_validate_custody_reversal_commit();
''', params=None)
        previous = import_module("apps.assets.migrations.0019_leased_custody_return")
        commit = previous.RETURN_SQL.split("CREATE OR REPLACE FUNCTION assets_validate_custody_return_commit()")[1].split("CREATE CONSTRAINT TRIGGER")[0]
        for sql in (*previous.current_functions(), previous.RETURN_SQL.split("CREATE TRIGGER")[0],
                    "CREATE OR REPLACE FUNCTION assets_validate_custody_return_commit()"+commit,
                    import_module("apps.assets.migrations.0018_asset_origin_and_composition").TRACE_SQL.split("CREATE TRIGGER")[0]):
            schema_editor.execute(sql, params=None)


class Migration(migrations.Migration):
    dependencies = [("assets", "0020_custody_and_origin_corrections"), ("maintenance", "0001_initial"), ("audit", "0002_auditlog_company")]
    operations = [migrations.RunPython(install, uninstall), migrations.RunPython(close_legacy_plans, migrations.RunPython.noop)]
