from django.db import migrations


CREATE_FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION masterdata_advisory_statement_lock()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(TG_ARGV[0], 0));
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_tree_node()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_company bigint;
    parent_level integer;
    cycle_found boolean;
    level_column text := TG_ARGV[0];
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.company_id IS DISTINCT FROM NEW.company_id THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:tree:' || TG_TABLE_NAME || ':company:' || LEAST(OLD.company_id, NEW.company_id)::text, 0
        ));
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:tree:' || TG_TABLE_NAME || ':company:' || GREATEST(OLD.company_id, NEW.company_id)::text, 0
        ));
    ELSE
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:tree:' || TG_TABLE_NAME || ':company:' || NEW.company_id::text, 0
        ));
    END IF;
    IF NEW.parent_id IS NULL THEN
        IF level_column <> '' THEN
            NEW := jsonb_populate_record(NEW, jsonb_build_object(level_column, 1));
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id IS NOT NULL AND NEW.parent_id = NEW.id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'tree node cannot be its own parent';
    END IF;

    EXECUTE format('SELECT company_id, %s FROM %I WHERE id = $1',
                   CASE WHEN level_column = '' THEN 'NULL::integer' ELSE quote_ident(level_column) END,
                   TG_TABLE_NAME)
       INTO parent_company, parent_level
       USING NEW.parent_id;

    IF parent_company IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'parent tree node does not exist';
    END IF;
    IF parent_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'parent tree node belongs to another company';
    END IF;

    IF NEW.id IS NOT NULL THEN
        EXECUTE format(
            'WITH RECURSIVE ancestors AS ('
            ' SELECT id, parent_id FROM %I WHERE id = $1'
            ' UNION ALL'
            ' SELECT p.id, p.parent_id FROM %I p JOIN ancestors a ON p.id = a.parent_id'
            ') SELECT EXISTS (SELECT 1 FROM ancestors WHERE id = $2)',
            TG_TABLE_NAME, TG_TABLE_NAME
        ) INTO cycle_found USING NEW.parent_id, NEW.id;
        IF cycle_found THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'tree relationship cannot contain a cycle';
        END IF;
    END IF;

    IF level_column <> '' THEN
        NEW := jsonb_populate_record(
            NEW,
            jsonb_build_object(level_column, parent_level + 1)
        );
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_refresh_descendant_levels()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    level_column text := TG_ARGV[0];
BEGIN
    IF NEW.parent_id IS NOT DISTINCT FROM OLD.parent_id THEN
        RETURN NEW;
    END IF;
    EXECUTE format(
        'WITH RECURSIVE descendants AS ('
        ' SELECT child.id, $2::integer + 1 AS expected_level'
        ' FROM %I child WHERE child.parent_id = $1'
        ' UNION ALL'
        ' SELECT child.id, parent.expected_level + 1'
        ' FROM %I child JOIN descendants parent ON child.parent_id = parent.id'
        ') UPDATE %I target SET %I = descendants.expected_level'
        ' FROM descendants WHERE target.id = descendants.id',
        TG_TABLE_NAME, TG_TABLE_NAME, TG_TABLE_NAME, level_column
    ) USING NEW.id, (to_jsonb(NEW) ->> level_column)::integer;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_employee()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    department_company bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.company_id IS DISTINCT FROM NEW.company_id THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:manager:company:' || LEAST(OLD.company_id, NEW.company_id)::text, 0
        ));
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:manager:company:' || GREATEST(OLD.company_id, NEW.company_id)::text, 0
        ));
    ELSE
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:manager:company:' || NEW.company_id::text, 0
        ));
    END IF;
    IF NEW.user_id IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            hashtextextended('masterdata:user:' || NEW.user_id::text, 0)
        );
        IF EXISTS (
            SELECT 1 FROM masterdata_userdepartmentscope
             WHERE user_id = NEW.user_id AND company_id <> NEW.company_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'employee user scope belongs to another company';
        END IF;
    END IF;
    SELECT company_id INTO department_company
      FROM masterdata_department WHERE id = NEW.department_id;
    IF department_company IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'employee department does not exist';
    END IF;
    IF department_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'employee department belongs to another company';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_department_manager()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    manager_company bigint;
    manager_status varchar;
    manager_active boolean;
    manager_department_active boolean;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.company_id IS DISTINCT FROM NEW.company_id THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:manager:company:' || LEAST(OLD.company_id, NEW.company_id)::text, 0
        ));
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:manager:company:' || GREATEST(OLD.company_id, NEW.company_id)::text, 0
        ));
    ELSE
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'masterdata:manager:company:' || NEW.company_id::text, 0
        ));
    END IF;
    IF NEW.manager_employee_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT e.company_id, e.employment_status, e.is_active, d.is_active
      INTO manager_company, manager_status, manager_active, manager_department_active
      FROM masterdata_employee e
      JOIN masterdata_department d ON d.id = e.department_id
     WHERE e.id = NEW.manager_employee_id;
    IF manager_company IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'department manager does not exist';
    END IF;
    IF manager_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'department manager belongs to another company';
    END IF;
    IF manager_status <> 'active' OR NOT manager_active OR NOT manager_department_active THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'department manager must be an active employee in an active department';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_guard_manager_employee_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (NEW.employment_status <> 'active' OR NOT NEW.is_active)
       AND EXISTS (
           SELECT 1 FROM masterdata_department
            WHERE manager_employee_id = NEW.id
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'clear department manager assignments before disabling employee';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM masterdata_department managed
          JOIN masterdata_department own_department ON own_department.id = NEW.department_id
         WHERE managed.manager_employee_id = NEW.id
           AND NOT own_department.is_active
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'department manager must belong to an active department';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_guard_manager_department_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT NEW.is_active
       AND EXISTS (
           SELECT 1
             FROM masterdata_employee employee
             JOIN masterdata_department managed ON managed.manager_employee_id = employee.id
            WHERE employee.department_id = NEW.id
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'clear manager assignments before disabling manager home department';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_user_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    department_company bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('masterdata:user:' || NEW.user_id::text, 0)
    );
    SELECT company_id INTO department_company
      FROM masterdata_department WHERE id = NEW.department_id;
    IF department_company IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'scope department does not exist';
    END IF;
    IF department_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'scope department belongs to another company';
    END IF;
    IF EXISTS (
        SELECT 1 FROM masterdata_employee
         WHERE user_id = NEW.user_id AND company_id <> NEW.company_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'scope user employee belongs to another company';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_import_batch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    attachment_company bigint;
    attachment_sha varchar;
BEGIN
    SELECT company_id, sha256 INTO attachment_company, attachment_sha
      FROM masterdata_attachment WHERE id = NEW.file_attachment_id;
    IF attachment_company IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'import attachment does not exist';
    END IF;
    IF attachment_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'import attachment belongs to another company';
    END IF;
    IF attachment_sha <> NEW.file_sha256 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'import attachment digest mismatch';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_protect_import_batch_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'confirmed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'confirmed import batch is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF NEW.template_version IS DISTINCT FROM OLD.template_version
       OR NEW.file_attachment_id IS DISTINCT FROM OLD.file_attachment_id
       OR NEW.file_sha256 IS DISTINCT FROM OLD.file_sha256
       OR NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.import_type IS DISTINCT FROM OLD.import_type
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.request_hash IS DISTINCT FROM OLD.request_hash THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'import evidence fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_protect_import_row_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.validation_status = 'created' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'created import row is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_protect_confirmed_import_attachment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM masterdata_importbatch
         WHERE file_attachment_id = OLD.id AND status = 'confirmed'
    ) THEN
        IF TG_OP = 'DELETE' OR to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'confirmed import attachment evidence is immutable';
        END IF;
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
       AND (jsonb_typeof(NEW.errors_json) <> 'array' OR jsonb_array_length(NEW.errors_json) = 0) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid import row must have structured errors';
    END IF;
    IF jsonb_typeof(NEW.errors_json) <> 'array' OR jsonb_typeof(NEW.warnings_json) <> 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'import errors and warnings must be arrays';
    END IF;
    IF NEW.validation_status = 'created' THEN
        SELECT status INTO batch_status FROM masterdata_importbatch WHERE id = NEW.batch_id;
        IF batch_status <> 'confirmed' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'created import row requires confirmed batch';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_system_setting_value()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    json_value jsonb;
    numeric_value numeric;
    integer_value bigint;
BEGIN
    IF NEW.key = 'attachment_allowed_extensions' THEN
        BEGIN
            json_value := NEW.value::jsonb;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment extension setting must be valid JSON';
        END;
        IF jsonb_typeof(json_value) <> 'array' OR jsonb_array_length(json_value) < 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment extension setting must be a non-empty JSON array';
        END IF;
        IF EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(json_value) extension
             WHERE extension NOT IN ('jpg','jpeg','png','webp','pdf','xlsx','docx')
                OR extension <> lower(extension)
        ) OR (SELECT count(*) FROM jsonb_array_elements_text(json_value))
             <> (SELECT count(DISTINCT extension) FROM jsonb_array_elements_text(json_value) extension) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment extension setting contains unsafe or duplicate values';
        END IF;
    ELSIF NEW.key = 'attachment_max_size_bytes' THEN
        BEGIN
            integer_value := NEW.value::bigint;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment size setting must be an integer';
        END;
        IF integer_value < 1 OR integer_value > 20971520 OR NEW.value <> integer_value::text THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment size setting is out of range or not canonical';
        END IF;
    ELSIF NEW.key = 'fixed_asset_warning_amount' THEN
        BEGIN
            numeric_value := NEW.value::numeric;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixed asset warning amount must be decimal';
        END;
        IF numeric_value < 0 THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixed asset warning amount must not be negative';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_guard_company_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.company_id IS NOT DISTINCT FROM OLD.company_id THEN
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'masterdata_department' THEN
        IF EXISTS (SELECT 1 FROM masterdata_department WHERE parent_id = OLD.id AND company_id <> NEW.company_id)
           OR EXISTS (SELECT 1 FROM masterdata_employee WHERE department_id = OLD.id AND company_id <> NEW.company_id)
           OR EXISTS (SELECT 1 FROM masterdata_userdepartmentscope WHERE department_id = OLD.id AND company_id <> NEW.company_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'department company change would break existing references';
        END IF;
    ELSIF TG_TABLE_NAME = 'masterdata_location' THEN
        IF EXISTS (SELECT 1 FROM masterdata_location WHERE parent_id = OLD.id AND company_id <> NEW.company_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'location company change would break existing references';
        END IF;
    ELSIF TG_TABLE_NAME = 'masterdata_assetcategory' THEN
        IF EXISTS (SELECT 1 FROM masterdata_assetcategory WHERE parent_id = OLD.id AND company_id <> NEW.company_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset category company change would break existing references';
        END IF;
    ELSIF TG_TABLE_NAME = 'masterdata_employee' THEN
        IF EXISTS (SELECT 1 FROM masterdata_department WHERE manager_employee_id = OLD.id AND company_id <> NEW.company_id)
           OR (NEW.user_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM masterdata_userdepartmentscope
                 WHERE user_id = NEW.user_id AND company_id <> NEW.company_id
           )) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'employee company change would break existing references';
        END IF;
    ELSIF TG_TABLE_NAME = 'masterdata_attachment' THEN
        IF EXISTS (SELECT 1 FROM masterdata_importbatch WHERE file_attachment_id = OLD.id AND company_id <> NEW.company_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment company change would break existing references';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_00_department_manager_write_lock
BEFORE INSERT OR UPDATE ON masterdata_department
FOR EACH STATEMENT EXECUTE FUNCTION masterdata_advisory_statement_lock('masterdata:manager:write');

CREATE TRIGGER trg_01_department_tree_write_lock
BEFORE INSERT OR UPDATE ON masterdata_department
FOR EACH STATEMENT EXECUTE FUNCTION masterdata_advisory_statement_lock('masterdata:tree:masterdata_department:write');

CREATE TRIGGER trg_00_employee_manager_write_lock
BEFORE INSERT OR UPDATE ON masterdata_employee
FOR EACH STATEMENT EXECUTE FUNCTION masterdata_advisory_statement_lock('masterdata:manager:write');

CREATE TRIGGER trg_00_location_tree_write_lock
BEFORE INSERT OR UPDATE ON masterdata_location
FOR EACH STATEMENT EXECUTE FUNCTION masterdata_advisory_statement_lock('masterdata:tree:masterdata_location:write');

CREATE TRIGGER trg_00_asset_category_tree_write_lock
BEFORE INSERT OR UPDATE ON masterdata_assetcategory
FOR EACH STATEMENT EXECUTE FUNCTION masterdata_advisory_statement_lock('masterdata:tree:masterdata_assetcategory:write');

CREATE TRIGGER trg_department_tree
BEFORE INSERT OR UPDATE OF parent_id, company_id
ON masterdata_department
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_tree_node('');

CREATE TRIGGER trg_location_tree
BEFORE INSERT OR UPDATE OF parent_id, company_id, level
ON masterdata_location
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_tree_node('level');

CREATE TRIGGER trg_asset_category_tree
BEFORE INSERT OR UPDATE OF parent_id, company_id, category_level
ON masterdata_assetcategory
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_tree_node('category_level');

CREATE TRIGGER trg_location_tree_descendants
AFTER UPDATE OF parent_id
ON masterdata_location
FOR EACH ROW EXECUTE FUNCTION masterdata_refresh_descendant_levels('level');

CREATE TRIGGER trg_asset_category_tree_descendants
AFTER UPDATE OF parent_id
ON masterdata_assetcategory
FOR EACH ROW EXECUTE FUNCTION masterdata_refresh_descendant_levels('category_level');

CREATE TRIGGER trg_employee_company
BEFORE INSERT OR UPDATE OF department_id, company_id, user_id
ON masterdata_employee
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_employee();

CREATE TRIGGER trg_department_manager
BEFORE INSERT OR UPDATE OF manager_employee_id, company_id
ON masterdata_department
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_department_manager();

CREATE CONSTRAINT TRIGGER trg_employee_manager_validity
AFTER UPDATE OF employment_status, is_active, department_id
ON masterdata_employee
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_manager_employee_change();

CREATE CONSTRAINT TRIGGER trg_department_manager_home_active
AFTER UPDATE OF is_active
ON masterdata_department
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_manager_department_change();

CREATE TRIGGER trg_user_scope_company
BEFORE INSERT OR UPDATE OF department_id, company_id, user_id
ON masterdata_userdepartmentscope
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_user_scope();

CREATE TRIGGER trg_import_batch_attachment
BEFORE INSERT OR UPDATE OF file_attachment_id, file_sha256, company_id
ON masterdata_importbatch
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_import_batch();

CREATE TRIGGER trg_import_batch_immutable
BEFORE UPDATE OR DELETE
ON masterdata_importbatch
FOR EACH ROW EXECUTE FUNCTION masterdata_protect_import_batch_immutable();

CREATE TRIGGER trg_import_row_immutable
BEFORE UPDATE OR DELETE
ON masterdata_importrow
FOR EACH ROW EXECUTE FUNCTION masterdata_protect_import_row_immutable();

CREATE TRIGGER trg_confirmed_import_attachment_immutable
BEFORE UPDATE OR DELETE
ON masterdata_attachment
FOR EACH ROW EXECUTE FUNCTION masterdata_protect_confirmed_import_attachment();

CREATE TRIGGER trg_import_row_state
BEFORE INSERT OR UPDATE OF validation_status, errors_json, warnings_json, batch_id
ON masterdata_importrow
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_import_row();

CREATE TRIGGER trg_system_setting_value
BEFORE INSERT OR UPDATE OF key, value, value_type
ON masterdata_systemsetting
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_system_setting_value();

CREATE TRIGGER trg_department_company_references
BEFORE UPDATE OF company_id ON masterdata_department
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_company_change();

CREATE TRIGGER trg_location_company_references
BEFORE UPDATE OF company_id ON masterdata_location
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_company_change();

CREATE TRIGGER trg_asset_category_company_references
BEFORE UPDATE OF company_id ON masterdata_assetcategory
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_company_change();

CREATE TRIGGER trg_employee_company_references
BEFORE UPDATE OF company_id ON masterdata_employee
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_company_change();

CREATE TRIGGER trg_attachment_company_references
BEFORE UPDATE OF company_id ON masterdata_attachment
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_company_change();
"""


DROP_FUNCTIONS = r"""
DROP TRIGGER IF EXISTS trg_00_asset_category_tree_write_lock ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_00_location_tree_write_lock ON masterdata_location;
DROP TRIGGER IF EXISTS trg_00_employee_manager_write_lock ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_01_department_tree_write_lock ON masterdata_department;
DROP TRIGGER IF EXISTS trg_00_department_manager_write_lock ON masterdata_department;
DROP TRIGGER IF EXISTS trg_import_row_state ON masterdata_importrow;
DROP TRIGGER IF EXISTS trg_attachment_company_references ON masterdata_attachment;
DROP TRIGGER IF EXISTS trg_employee_company_references ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_asset_category_company_references ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_location_company_references ON masterdata_location;
DROP TRIGGER IF EXISTS trg_department_company_references ON masterdata_department;
DROP TRIGGER IF EXISTS trg_import_row_immutable ON masterdata_importrow;
DROP TRIGGER IF EXISTS trg_confirmed_import_attachment_immutable ON masterdata_attachment;
DROP TRIGGER IF EXISTS trg_system_setting_value ON masterdata_systemsetting;
DROP TRIGGER IF EXISTS trg_import_batch_immutable ON masterdata_importbatch;
DROP TRIGGER IF EXISTS trg_import_batch_attachment ON masterdata_importbatch;
DROP TRIGGER IF EXISTS trg_user_scope_company ON masterdata_userdepartmentscope;
DROP TRIGGER IF EXISTS trg_department_manager ON masterdata_department;
DROP TRIGGER IF EXISTS trg_department_manager_home_active ON masterdata_department;
DROP TRIGGER IF EXISTS trg_employee_manager_validity ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_employee_company ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_asset_category_tree ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_asset_category_tree_descendants ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_location_tree ON masterdata_location;
DROP TRIGGER IF EXISTS trg_location_tree_descendants ON masterdata_location;
DROP TRIGGER IF EXISTS trg_department_tree ON masterdata_department;
DROP FUNCTION IF EXISTS masterdata_validate_import_row();
DROP FUNCTION IF EXISTS masterdata_guard_company_change();
DROP FUNCTION IF EXISTS masterdata_protect_import_row_immutable();
DROP FUNCTION IF EXISTS masterdata_protect_confirmed_import_attachment();
DROP FUNCTION IF EXISTS masterdata_validate_system_setting_value();
DROP FUNCTION IF EXISTS masterdata_protect_import_batch_immutable();
DROP FUNCTION IF EXISTS masterdata_validate_import_batch();
DROP FUNCTION IF EXISTS masterdata_validate_user_scope();
DROP FUNCTION IF EXISTS masterdata_validate_department_manager();
DROP FUNCTION IF EXISTS masterdata_guard_manager_department_change();
DROP FUNCTION IF EXISTS masterdata_guard_manager_employee_change();
DROP FUNCTION IF EXISTS masterdata_validate_employee();
DROP FUNCTION IF EXISTS masterdata_validate_tree_node();
DROP FUNCTION IF EXISTS masterdata_refresh_descendant_levels();
DROP FUNCTION IF EXISTS masterdata_advisory_statement_lock();
"""


def install_integrity_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(CREATE_FUNCTIONS)


def uninstall_integrity_triggers(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(DROP_FUNCTIONS)


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0001_initial")]

    operations = [
        migrations.RunPython(
            code=install_integrity_triggers,
            reverse_code=uninstall_integrity_triggers,
        )
    ]
