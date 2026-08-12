from django.db import migrations, models


CREATE_POSTGRESQL_GUARDS = r"""
CREATE OR REPLACE FUNCTION masterdata_protect_import_row_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    controlled_confirmation boolean;
    controlled_cleanup boolean;
    batch_status varchar;
BEGIN
    IF OLD.validation_status = 'created' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'created import row is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT status
          INTO batch_status
          FROM masterdata_importbatch
         WHERE id = OLD.batch_id;
        controlled_cleanup := COALESCE(
            current_setting('eam_lite.controlled_import_cleanup', true), ''
        ) = 'on';
        IF batch_status = 'validated' AND NOT controlled_cleanup THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'validated import row evidence cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.validation_status IN ('valid', 'invalid') THEN
        controlled_confirmation := COALESCE(
            current_setting('eam_lite.controlled_import_confirmation', true), ''
        ) = 'on';
        IF NOT (
            controlled_confirmation
            AND OLD.validation_status = 'valid'
            AND NEW.validation_status = 'created'
            AND NEW.raw_data_json IS NOT DISTINCT FROM OLD.raw_data_json
            AND NEW.normalized_data_json IS NOT DISTINCT FROM OLD.normalized_data_json
            AND NEW.errors_json IS NOT DISTINCT FROM OLD.errors_json
            AND NEW.warnings_json IS NOT DISTINCT FROM OLD.warnings_json
            AND NEW.batch_id IS NOT DISTINCT FROM OLD.batch_id
            AND NEW.row_number IS NOT DISTINCT FROM OLD.row_number
            AND NEW.created_object_type <> ''
            AND NEW.created_object_id <> ''
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'validated import row evidence is immutable';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_import_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    batch_status varchar;
BEGIN
    SELECT status INTO batch_status
      FROM masterdata_importbatch
     WHERE id = NEW.batch_id;

    IF batch_status IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = 'import row batch does not exist';
    END IF;
    IF NEW.validation_status = 'invalid'
       AND (jsonb_typeof(NEW.errors_json) <> 'array'
            OR jsonb_array_length(NEW.errors_json) = 0) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'invalid import row must have structured errors';
    END IF;
    IF jsonb_typeof(NEW.errors_json) <> 'array'
       OR jsonb_typeof(NEW.warnings_json) <> 'array' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'import errors and warnings must be arrays';
    END IF;
    IF NEW.validation_status = 'created' AND batch_status <> 'confirmed' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'created import row requires confirmed batch';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_confirmed_import_rows()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_batch_id bigint;
    target_batch_status varchar;
    target_total_rows integer;
    actual_row_count integer;
BEGIN
    IF TG_TABLE_NAME = 'masterdata_importbatch' THEN
        target_batch_id := NEW.id;
    ELSIF TG_TABLE_NAME = 'masterdata_importrow' THEN
        target_batch_id := NEW.batch_id;
    ELSE
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'unsupported import consistency trigger table';
    END IF;

    SELECT status, total_rows
      INTO target_batch_status, target_total_rows
      FROM masterdata_importbatch
     WHERE id = target_batch_id;

    IF target_batch_status = 'confirmed' THEN
        SELECT count(*)
          INTO actual_row_count
          FROM masterdata_importrow
         WHERE batch_id = target_batch_id;
        IF target_total_rows IS NULL
           OR actual_row_count <> target_total_rows
           OR EXISTS (
               SELECT 1
                 FROM masterdata_importrow
                WHERE batch_id = target_batch_id
                  AND validation_status <> 'created'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'confirmed import batch requires its complete created row set';
        END IF;
    END IF;

    IF TG_TABLE_NAME = 'masterdata_importrow' THEN
        IF NEW.validation_status = 'created'
           AND target_batch_status <> 'confirmed' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'created import row requires confirmed batch';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_import_batch_confirmed_rows
AFTER INSERT OR UPDATE OF status
ON masterdata_importbatch
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_confirmed_import_rows();

CREATE CONSTRAINT TRIGGER trg_import_row_confirmed_batch
AFTER INSERT OR UPDATE OF batch_id, validation_status
ON masterdata_importrow
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_confirmed_import_rows();
"""


DROP_POSTGRESQL_GUARDS = r"""
DROP TRIGGER IF EXISTS trg_import_row_confirmed_batch ON masterdata_importrow;
DROP TRIGGER IF EXISTS trg_import_batch_confirmed_rows ON masterdata_importbatch;
DROP FUNCTION IF EXISTS masterdata_validate_confirmed_import_rows();

CREATE OR REPLACE FUNCTION masterdata_protect_import_row_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.validation_status = 'created' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'created import row is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_import_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    batch_status varchar;
BEGIN
    IF NEW.validation_status = 'invalid'
       AND (jsonb_typeof(NEW.errors_json) <> 'array'
            OR jsonb_array_length(NEW.errors_json) = 0) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'invalid import row must have structured errors';
    END IF;
    IF jsonb_typeof(NEW.errors_json) <> 'array'
       OR jsonb_typeof(NEW.warnings_json) <> 'array' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'import errors and warnings must be arrays';
    END IF;
    IF NEW.validation_status = 'created' THEN
        SELECT status INTO batch_status
          FROM masterdata_importbatch
         WHERE id = NEW.batch_id;
        IF batch_status <> 'confirmed' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'created import row requires confirmed batch';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(CREATE_POSTGRESQL_GUARDS)


def uninstall_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(DROP_POSTGRESQL_GUARDS)


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0008_sprint4_counter_guards")]

    operations = [
        migrations.AlterField(
            model_name="importbatch",
            name="import_type",
            field=models.CharField(
                choices=[
                    ("department", "部门"),
                    ("employee", "人员"),
                    ("asset_initialization", "资产初始化"),
                ],
                max_length=32,
                verbose_name="导入类型",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="importbatch",
            name="ck_import_batch_type_valid",
        ),
        migrations.AddConstraint(
            model_name="importbatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    import_type__in=(
                        "department",
                        "employee",
                        "asset_initialization",
                    )
                ),
                name="ck_import_batch_type_valid",
            ),
        ),
        migrations.RunPython(
            code=install_postgresql_guards,
            reverse_code=uninstall_postgresql_guards,
        ),
    ]
