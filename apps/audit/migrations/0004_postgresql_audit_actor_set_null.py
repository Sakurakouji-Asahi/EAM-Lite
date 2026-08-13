from django.db import migrations


CREATE_SQL = r"""
CREATE OR REPLACE FUNCTION eam_guard_auditlog_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.user_id IS NOT NULL
       AND NEW.user_id IS NULL
       AND ROW(
           NEW.company_id, NEW.action, NEW.object_type, NEW.object_id,
           NEW.old_data_json, NEW.new_data_json, NEW.ip_address,
           NEW.user_agent, NEW.correlation_id, NEW.created_at
       ) IS NOT DISTINCT FROM ROW(
           OLD.company_id, OLD.action, OLD.object_type, OLD.object_id,
           OLD.old_data_json, OLD.new_data_json, OLD.ip_address,
           OLD.user_agent, OLD.correlation_id, OLD.created_at
       ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'audit log rows are append-only'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;
"""


def install_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def restore_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        from importlib import import_module

        previous = import_module(
            "apps.audit.migrations.0003_postgresql_auditlog_append_only"
        )
        schema_editor.execute(previous.CREATE_SQL)


class Migration(migrations.Migration):
    dependencies = [("audit", "0003_postgresql_auditlog_append_only")]

    operations = [
        migrations.RunPython(
            install_postgresql_guard,
            reverse_code=restore_postgresql_guard,
        )
    ]
