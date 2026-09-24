"""Narrow exceptions for unused registrations, recorded before the same commit.

Existing triggers are extended, not disabled. Removing the extension restores
their exact previous bodies; post-undo business rows are never recreated.
"""
import re
from django.db import migrations


HELPERS = r"""
CREATE FUNCTION audit_operation_undo_plan() RETURNS jsonb LANGUAGE sql STABLE AS $$
 SELECT u.plan_json FROM audit_operationundo u
 JOIN audit_auditlog original ON original.id=u.original_log_id
 JOIN audit_auditlog reversal ON reversal.id=u.reversal_log_id
 WHERE u.id::text=NULLIF(current_setting('eam_lite.operation_undo',true),'')
   AND u.created_at >= transaction_timestamp()
   AND original.company_id=reversal.company_id
   AND ((original.action='asset_register' AND reversal.action='asset_registration_reverse')
     OR (original.action='import_confirm' AND reversal.action='import_reverse'))
$$;
CREATE FUNCTION audit_operation_undo_deletes(t text, k text) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT COALESCE((audit_operation_undo_plan()->'delete'->t) ? k,false)
$$;
CREATE FUNCTION audit_guard_operation_undo() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='operation undo evidence is append only';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM audit_auditlog a JOIN audit_auditlog b ON a.company_id=b.company_id
   WHERE a.id=NEW.original_log_id AND b.id=NEW.reversal_log_id
     AND ((a.action='asset_register' AND a.object_type='Asset' AND b.action IN ('asset_registration_reverse','import_reverse'))
       OR (a.action='import_confirm' AND a.object_type='ImportBatch' AND b.action='import_reverse'))
     AND a.created_at<=b.created_at) THEN
   RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='invalid operation undo evidence';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER trg_operation_undo_immutable BEFORE INSERT OR UPDATE OR DELETE ON audit_operationundo
 FOR EACH ROW EXECUTE FUNCTION audit_guard_operation_undo();
"""

DELETE_BRANCH = """
    IF TG_OP='DELETE' AND audit_operation_undo_deletes(TG_TABLE_NAME,OLD.id::text) THEN
        RETURN OLD;
    END IF;
"""
BRANCHES = {name: DELETE_BRANCH for name in (
    'assets_guard_registration', 'assets_guard_unified_identity',
    'assets_reject_code_history_change', 'assets_guard_qr_delete',
    'masterdata_reject_issued_code_delete',
)}
BRANCHES['masterdata_guard_sequence_counter_history'] = """
    IF TG_OP='UPDATE' AND (to_jsonb(NEW)-'current_value'-'updated_at')=(to_jsonb(OLD)-'current_value'-'updated_at')
       AND EXISTS (SELECT 1 FROM jsonb_array_elements(audit_operation_undo_plan()->'counters') x
          WHERE x->>'id'=OLD.id::text AND (x->>'old')::bigint=OLD.current_value
            AND (x->>'new')::bigint=NEW.current_value AND NEW.current_value<OLD.current_value)
       AND NOT EXISTS (SELECT 1 FROM masterdata_issuedcode WHERE company_id=OLD.company_id
           AND scope_key=OLD.scope_key AND sequence_value>NEW.current_value) THEN
        RETURN NEW;
    END IF;
"""
BRANCHES['masterdata_protect_import_batch_immutable'] = """
    IF TG_OP='UPDATE' AND OLD.status='confirmed' AND NEW.status='reversed'
       AND (to_jsonb(NEW)-'status')=(to_jsonb(OLD)-'status')
       AND audit_operation_undo_plan()->>'batch_id'=OLD.id::text THEN RETURN NEW; END IF;
    IF OLD.status='reversed' THEN
       RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='reversed import evidence is immutable';
    END IF;
"""
BRANCHES['masterdata_protect_confirmed_import_attachment'] = """
    IF EXISTS (SELECT 1 FROM masterdata_importbatch WHERE file_attachment_id=OLD.id AND status='reversed') THEN
       RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='reversed import attachment is immutable';
    END IF;
"""
OLD_TRANSITION = "OR (OLD.asset_status='pending_label' AND NEW.asset_status IN ('in_use','idle'))"
NEW_TRANSITION = OLD_TRANSITION + """
            OR (OLD.asset_status='pending_label' AND NEW.asset_status='draft'
                AND COALESCE((audit_operation_undo_plan()->'draft_assets') ? OLD.id::text,false))"""


def _definition(editor, name):
    with editor.connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", [name + '()'])
        return cursor.fetchone()[0]


def _marker(branch):
    return '\n    -- BEGIN controlled_operation_undo\n' + branch + '    -- END controlled_operation_undo\n'


def install(apps, editor):
    if editor.connection.vendor != 'postgresql':
        return
    editor.execute(HELPERS, params=None)
    for name, branch in BRANCHES.items():
        sql = _definition(editor, name)
        if 'controlled_operation_undo' in sql:
            raise RuntimeError('Undo guards already installed: ' + name)
        sql, count = re.subn(r'\bBEGIN\b', lambda _: 'BEGIN' + _marker(branch), sql, count=1)
        if count != 1:
            raise RuntimeError('Unexpected trigger body: ' + name)
        editor.execute(sql, params=None)
    sql = _definition(editor, 'assets_validate_asset_references')
    if sql.count(OLD_TRANSITION) != 1:
        raise RuntimeError('Unexpected asset transitions')
    editor.execute(sql.replace(OLD_TRANSITION, NEW_TRANSITION), params=None)


def uninstall(apps, editor):
    if editor.connection.vendor != 'postgresql':
        return
    for name, branch in BRANCHES.items():
        sql = _definition(editor, name)
        marker = _marker(branch)
        if sql.count(marker) != 1:
            raise RuntimeError('Unexpected undo trigger: ' + name)
        editor.execute(sql.replace(marker, ''), params=None)
    sql = _definition(editor, 'assets_validate_asset_references')
    if sql.count(NEW_TRANSITION) != 1:
        raise RuntimeError('Unexpected asset undo transition')
    editor.execute(sql.replace(NEW_TRANSITION, OLD_TRANSITION), params=None)
    editor.execute('DROP TRIGGER trg_operation_undo_immutable ON audit_operationundo; '
                   'DROP FUNCTION audit_guard_operation_undo(); '
                   'DROP FUNCTION audit_operation_undo_deletes(text,text); '
                   'DROP FUNCTION audit_operation_undo_plan();', params=None)


class Migration(migrations.Migration):
    dependencies = [
        ('audit', '0007_operationundo'),
        ('assets', '0022_asset_equipment_number'),
        ('masterdata', '0016_remove_importbatch_ck_import_batch_status_fields_and_more'),
    ]
    operations = [migrations.RunPython(install, uninstall)]
