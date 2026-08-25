from django.db import migrations


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION operations_guard_backup_set()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'backup metadata is append-only' USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending'
           OR NEW.storage_key <> ''
           OR NEW.package_sha256 <> ''
           OR NEW.package_size IS NOT NULL
           OR NEW.data_snapshot_at IS NOT NULL
           OR NEW.finished_at IS NOT NULL
           OR NEW.expires_at IS NOT NULL
           OR NEW.expired_at IS NOT NULL
           OR NEW.error_summary <> '' THEN
            RAISE EXCEPTION 'new backup metadata must start pending'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.requested_by_id IS NOT NULL
       AND NEW.requested_by_id IS NULL
       AND (to_jsonb(NEW) - 'requested_by_id') =
           (to_jsonb(OLD) - 'requested_by_id') THEN
        RETURN NEW;
    END IF;

    IF OLD.company_id IS DISTINCT FROM NEW.company_id
       OR OLD.backup_set_id IS DISTINCT FROM NEW.backup_set_id
       OR OLD.kind IS DISTINCT FROM NEW.kind
       OR OLD.request_hash IS DISTINCT FROM NEW.request_hash
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
       OR OLD.requested_by_id IS DISTINCT FROM NEW.requested_by_id
       OR OLD.started_at IS DISTINCT FROM NEW.started_at
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'backup identity fields are immutable'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.status = 'pending' AND NEW.status = 'completed' THEN
        IF NEW.storage_key !~ '^backups/[0-9A-Fa-f-]{36}/[0-9A-Fa-f-]{36}[.]eambak$'
           OR NEW.package_sha256 !~ '^[0-9a-f]{64}$'
           OR NEW.package_size IS NULL OR NEW.package_size < 0
           OR NEW.manifest_json = '{}'::jsonb
           OR NEW.data_snapshot_at IS NULL
           OR NEW.finished_at IS NULL
           OR NEW.expires_at IS NULL
           OR NEW.expired_at IS NOT NULL
           OR NEW.error_summary <> '' THEN
            RAISE EXCEPTION 'completed backup fields are incomplete'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'pending' AND NEW.status = 'failed' THEN
        IF NEW.storage_key <> ''
           OR NEW.package_sha256 <> ''
           OR NEW.package_size IS NOT NULL
           OR NEW.data_snapshot_at IS NOT NULL
           OR NEW.finished_at IS NULL
           OR NEW.expires_at IS NOT NULL
           OR NEW.expired_at IS NOT NULL
           OR btrim(NEW.error_summary) = '' THEN
            RAISE EXCEPTION 'failed backup fields are incomplete'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'completed' AND NEW.status = 'expired' THEN
        IF NEW.storage_key <> ''
           OR NEW.package_sha256 IS DISTINCT FROM OLD.package_sha256
           OR NEW.package_size IS DISTINCT FROM OLD.package_size
           OR NEW.manifest_json IS DISTINCT FROM OLD.manifest_json
           OR NEW.data_snapshot_at IS DISTINCT FROM OLD.data_snapshot_at
           OR NEW.finished_at IS DISTINCT FROM OLD.finished_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.expired_at IS NULL
           OR NEW.error_summary <> '' THEN
            RAISE EXCEPTION 'expired backup must retain completed evidence'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'illegal backup metadata transition'
        USING ERRCODE = '23514';
END;
$$;

DROP TRIGGER IF EXISTS operations_backup_set_guard
ON operations_backupset;
CREATE TRIGGER operations_backup_set_guard
BEFORE INSERT OR UPDATE OR DELETE ON operations_backupset
FOR EACH ROW EXECUTE FUNCTION operations_guard_backup_set();

CREATE OR REPLACE FUNCTION operations_guard_backup_download_grant()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    backup_company bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'backup download grants are append-only'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'INSERT' THEN
        SELECT company_id INTO backup_company
        FROM operations_backupset
        WHERE id = NEW.backup_set_id;
        IF backup_company IS NULL OR backup_company <> NEW.company_id
           OR NEW.status <> 'issued'
           OR NEW.user_id IS NULL
           OR NEW.expires_at <= NEW.issued_at
           OR NEW.started_at IS NOT NULL
           OR NEW.finished_at IS NOT NULL
           OR NEW.failure_reason <> '' THEN
            RAISE EXCEPTION 'invalid backup download grant'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.user_id IS NOT NULL
       AND NEW.user_id IS NULL
       AND (to_jsonb(NEW) - 'user_id') = (to_jsonb(OLD) - 'user_id') THEN
        RETURN NEW;
    END IF;

    IF OLD.company_id IS DISTINCT FROM NEW.company_id
       OR OLD.backup_set_id IS DISTINCT FROM NEW.backup_set_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
       OR OLD.issued_at IS DISTINCT FROM NEW.issued_at
       OR OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
        RAISE EXCEPTION 'backup download grant identity is immutable'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.status = 'issued' AND NEW.status = 'started'
       AND NEW.started_at IS NOT NULL
       AND NEW.finished_at IS NULL
       AND NEW.failure_reason = '' THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'started' AND NEW.status = 'completed'
       AND NEW.started_at IS DISTINCT FROM OLD.started_at
       AND NEW.finished_at IS NOT NULL
       AND NEW.failure_reason = '' THEN
        RAISE EXCEPTION 'download start time is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status = 'started' AND NEW.status = 'completed'
       AND NEW.started_at IS NOT DISTINCT FROM OLD.started_at
       AND NEW.finished_at IS NOT NULL
       AND NEW.failure_reason = '' THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'started' AND NEW.status = 'failed'
       AND NEW.started_at IS NOT DISTINCT FROM OLD.started_at
       AND NEW.finished_at IS NOT NULL
       AND btrim(NEW.failure_reason) <> '' THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'illegal backup download grant transition'
        USING ERRCODE = '23514';
END;
$$;

DROP TRIGGER IF EXISTS operations_backup_download_grant_guard
ON operations_backupdownloadgrant;
CREATE TRIGGER operations_backup_download_grant_guard
BEFORE INSERT OR UPDATE OR DELETE ON operations_backupdownloadgrant
FOR EACH ROW EXECUTE FUNCTION operations_guard_backup_download_grant();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS operations_backup_download_grant_guard
ON operations_backupdownloadgrant;
DROP FUNCTION IF EXISTS operations_guard_backup_download_grant();
DROP TRIGGER IF EXISTS operations_backup_set_guard
ON operations_backupset;
DROP FUNCTION IF EXISTS operations_guard_backup_set();
"""


def install_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD_SQL)


def remove_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [("operations", "0001_initial")]

    operations = [migrations.RunPython(install_guards, remove_guards)]
