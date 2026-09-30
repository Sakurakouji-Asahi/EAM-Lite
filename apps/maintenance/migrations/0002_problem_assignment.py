from importlib import import_module
from django.db import migrations


SOURCE = import_module('apps.maintenance.migrations.0001_initial').MAINTENANCE_GUARDS_SQL
FUNCTION = SOURCE[SOURCE.index('CREATE OR REPLACE FUNCTION maintenance_validate_problem()'):SOURCE.index('CREATE OR REPLACE FUNCTION maintenance_validate_record_problem_pair()')]
OLD = """    IF TG_OP='UPDATE' AND NOT (
        OLD.status='open' AND NEW.status='closed'
"""
NEW = """    IF TG_OP='UPDATE' AND NOT (
        (OLD.status='open' AND NEW.status='open'
         AND COALESCE(current_setting('eam_lite.controlled_maintenance_problem_assignment',true),'')='on'
         AND ROW(NEW.company_id,NEW.maintenance_record_id,NEW.asset_id,NEW.description,
                 NEW.status,NEW.closed_by_id,NEW.closed_at,NEW.closure_note,NEW.created_at)
             IS NOT DISTINCT FROM
             ROW(OLD.company_id,OLD.maintenance_record_id,OLD.asset_id,OLD.description,
                 OLD.status,OLD.closed_by_id,OLD.closed_at,OLD.closure_note,OLD.created_at))
        OR OLD.status='open' AND NEW.status='closed'
        AND COALESCE(current_setting('eam_lite.controlled_maintenance_problem_mutation',true),'')='on'
"""
assert FUNCTION.count(OLD) == 1
UPDATED = FUNCTION.replace(OLD,NEW,1).replace(
    "    IF NOT controlled THEN",
    "    IF TG_OP='UPDATE' AND COALESCE(current_setting('eam_lite.controlled_maintenance_problem_assignment',true),'')='on' THEN controlled := true; END IF;\n    IF NOT controlled THEN",
    1,
).replace("    RETURN NEW;", "    PERFORM set_config('eam_lite.controlled_maintenance_problem_assignment','off',true);\n    RETURN NEW;",1)


def install(apps,schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(UPDATED)


def uninstall(apps,schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FUNCTION)


class Migration(migrations.Migration):
    dependencies = [('maintenance','0001_initial')]
    operations = [migrations.RunPython(install,uninstall)]
