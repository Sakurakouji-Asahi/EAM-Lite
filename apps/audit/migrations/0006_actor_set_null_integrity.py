from django.conf import settings
from django.db import migrations


CREATE_SQL = r"""
CREATE OR REPLACE FUNCTION audit_sprint11_validate_actor_set_null()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_actor_id bigint;
    new_actor_id bigint;
BEGIN
    IF TG_TABLE_NAME = 'audit_auditlog' THEN
        old_actor_id := NULLIF(to_jsonb(OLD)->>'user_id', '')::bigint;
        new_actor_id := NULLIF(to_jsonb(NEW)->>'user_id', '')::bigint;
    ELSIF TG_TABLE_NAME = 'reports_exportlog' THEN
        old_actor_id := NULLIF(to_jsonb(OLD)->>'requested_by_id', '')::bigint;
        new_actor_id := NULLIF(to_jsonb(NEW)->>'requested_by_id', '')::bigint;
    ELSIF TG_TABLE_NAME = 'assets_assetexternalreference' THEN
        old_actor_id := NULLIF(to_jsonb(OLD)->>'created_by_id', '')::bigint;
        new_actor_id := NULLIF(to_jsonb(NEW)->>'created_by_id', '')::bigint;
    ELSE
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='unsupported actor guard table';
    END IF;

    IF old_actor_id IS NOT NULL AND new_actor_id IS NULL
       AND EXISTS (SELECT 1 FROM accounts_user WHERE id = old_actor_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='actor can only be cleared by user deletion';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_actor_set_null_integrity ON audit_auditlog;
CREATE CONSTRAINT TRIGGER trg_audit_actor_set_null_integrity
AFTER UPDATE OF user_id ON audit_auditlog
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION audit_sprint11_validate_actor_set_null();

DROP TRIGGER IF EXISTS trg_reports_actor_set_null_integrity ON reports_exportlog;
CREATE CONSTRAINT TRIGGER trg_reports_actor_set_null_integrity
AFTER UPDATE OF requested_by_id ON reports_exportlog
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION audit_sprint11_validate_actor_set_null();

DROP TRIGGER IF EXISTS trg_external_reference_actor_set_null_integrity
    ON assets_assetexternalreference;
CREATE CONSTRAINT TRIGGER trg_external_reference_actor_set_null_integrity
AFTER UPDATE OF created_by_id ON assets_assetexternalreference
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION audit_sprint11_validate_actor_set_null();
"""

DROP_SQL = r"""
DROP TRIGGER IF EXISTS trg_external_reference_actor_set_null_integrity
    ON assets_assetexternalreference;
DROP TRIGGER IF EXISTS trg_reports_actor_set_null_integrity ON reports_exportlog;
DROP TRIGGER IF EXISTS trg_audit_actor_set_null_integrity ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_sprint11_validate_actor_set_null();
"""


def install_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0005_sprint11_query_indexes"),
        ("reports", "0001_initial"),
        ("assets", "0013_assetexternalreference"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            install_postgresql_guard,
            reverse_code=remove_postgresql_guard,
        )
    ]
