from django.db import migrations


CREATE_SQL = """
CREATE OR REPLACE FUNCTION eam_guard_auditlog_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit log rows are append-only'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_auditlog_append_only ON audit_auditlog;
CREATE TRIGGER trg_auditlog_append_only
BEFORE UPDATE OR DELETE ON audit_auditlog
FOR EACH ROW
EXECUTE FUNCTION eam_guard_auditlog_append_only();
"""


DROP_SQL = """
DROP TRIGGER IF EXISTS trg_auditlog_append_only ON audit_auditlog;
DROP FUNCTION IF EXISTS eam_guard_auditlog_append_only();
"""


def install_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("audit", "0002_auditlog_company")]

    operations = [
        migrations.RunPython(
            install_postgresql_guard,
            reverse_code=remove_postgresql_guard,
        )
    ]
