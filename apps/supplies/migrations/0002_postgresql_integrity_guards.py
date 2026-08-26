from django.db import migrations


CREATE_POSTGRESQL_GUARDS = r"""
CREATE OR REPLACE FUNCTION supplies_reject_company_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'supply master-data company is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_category_tree()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    parent_company bigint;
    cycle_found boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('supplies:category:company:' || NEW.company_id::text, 0)
    );
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.parent_id = NEW.id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'supply category cannot be its own parent';
    END IF;
    SELECT company_id
      INTO parent_company
      FROM supplies_supplycategory
     WHERE id = NEW.parent_id;
    IF parent_company IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = 'supply category parent does not exist';
    END IF;
    IF parent_company <> NEW.company_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'supply category parent belongs to another company';
    END IF;
    WITH RECURSIVE ancestors AS (
        SELECT id, parent_id
          FROM supplies_supplycategory
         WHERE id = NEW.parent_id
        UNION ALL
        SELECT parent.id, parent.parent_id
          FROM supplies_supplycategory parent
          JOIN ancestors child ON parent.id = child.parent_id
    )
    SELECT EXISTS(SELECT 1 FROM ancestors WHERE id = NEW.id)
      INTO cycle_found;
    IF cycle_found THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'supply category tree cannot contain a cycle';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_warehouse_references()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    reference_company bigint;
    reference_active boolean;
    employee_status varchar;
    employee_department_active boolean;
BEGIN
    IF NEW.location_id IS NOT NULL THEN
        SELECT company_id, is_active
          INTO reference_company, reference_active
          FROM masterdata_location
         WHERE id = NEW.location_id;
        IF reference_company IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'warehouse location does not exist';
        END IF;
        IF reference_company <> NEW.company_id OR NOT reference_active THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'warehouse location must be active and in the same company';
        END IF;
    END IF;
    IF NEW.manager_employee_id IS NOT NULL THEN
        SELECT employee.company_id,
               employee.is_active,
               employee.employment_status,
               department.is_active
          INTO reference_company,
               reference_active,
               employee_status,
               employee_department_active
          FROM masterdata_employee employee
          JOIN masterdata_department department
            ON department.id = employee.department_id
         WHERE employee.id = NEW.manager_employee_id;
        IF reference_company IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'warehouse manager does not exist';
        END IF;
        IF reference_company <> NEW.company_id
           OR NOT reference_active
           OR employee_status <> 'active'
           OR NOT employee_department_active THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'warehouse manager must be an active employee in the same company';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_item_references()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    reference_company bigint;
    reference_active boolean;
BEGIN
    SELECT company_id, is_active
      INTO reference_company, reference_active
      FROM supplies_supplycategory
     WHERE id = NEW.category_id;
    IF reference_company IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'supply item category does not exist';
    END IF;
    IF reference_company <> NEW.company_id OR NOT reference_active THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'supply item category must be active and in the same company';
    END IF;
    IF NEW.default_warehouse_id IS NOT NULL THEN
        SELECT company_id, is_active
          INTO reference_company, reference_active
          FROM supplies_supplywarehouse
         WHERE id = NEW.default_warehouse_id;
        IF reference_company IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'supply item default warehouse does not exist';
        END IF;
        IF reference_company <> NEW.company_id OR NOT reference_active THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'supply item default warehouse must be active and in the same company';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_manager_employee_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    manager_department_active boolean;
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM supplies_supplywarehouse
         WHERE manager_employee_id = NEW.id
    ) THEN
        RETURN NEW;
    END IF;
    SELECT is_active
      INTO manager_department_active
      FROM masterdata_department
     WHERE id = NEW.department_id;
    IF NEW.employment_status <> 'active'
       OR NOT NEW.is_active
       OR NOT COALESCE(manager_department_active, false) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'clear supply warehouse manager assignments before disabling employee';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_manager_department_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT NEW.is_active
       AND EXISTS (
           SELECT 1
             FROM supplies_supplywarehouse warehouse
             JOIN masterdata_employee employee
               ON employee.id = warehouse.manager_employee_id
            WHERE employee.department_id = NEW.id
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'clear supply warehouse manager assignments before disabling department';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_supplies_category_company_immutable
BEFORE UPDATE OF company_id
ON supplies_supplycategory
FOR EACH ROW EXECUTE FUNCTION supplies_reject_company_change();

CREATE TRIGGER trg_supplies_warehouse_company_immutable
BEFORE UPDATE OF company_id
ON supplies_supplywarehouse
FOR EACH ROW EXECUTE FUNCTION supplies_reject_company_change();

CREATE TRIGGER trg_supplies_item_company_immutable
BEFORE UPDATE OF company_id
ON supplies_supplyitem
FOR EACH ROW EXECUTE FUNCTION supplies_reject_company_change();

CREATE TRIGGER trg_supplies_category_tree
BEFORE INSERT OR UPDATE OF parent_id, company_id
ON supplies_supplycategory
FOR EACH ROW EXECUTE FUNCTION supplies_validate_category_tree();

CREATE TRIGGER trg_supplies_warehouse_references
BEFORE INSERT OR UPDATE OF location_id, manager_employee_id, company_id
ON supplies_supplywarehouse
FOR EACH ROW EXECUTE FUNCTION supplies_validate_warehouse_references();

CREATE TRIGGER trg_supplies_item_references
BEFORE INSERT OR UPDATE OF category_id, default_warehouse_id, company_id
ON supplies_supplyitem
FOR EACH ROW EXECUTE FUNCTION supplies_validate_item_references();

CREATE CONSTRAINT TRIGGER trg_supplies_manager_employee_validity
AFTER UPDATE OF employment_status, is_active, department_id
ON masterdata_employee
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION supplies_guard_manager_employee_change();

CREATE CONSTRAINT TRIGGER trg_supplies_manager_department_validity
AFTER UPDATE OF is_active
ON masterdata_department
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION supplies_guard_manager_department_change();
"""


DROP_POSTGRESQL_GUARDS = r"""
DROP TRIGGER IF EXISTS trg_supplies_manager_department_validity ON masterdata_department;
DROP TRIGGER IF EXISTS trg_supplies_manager_employee_validity ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_supplies_item_references ON supplies_supplyitem;
DROP TRIGGER IF EXISTS trg_supplies_warehouse_references ON supplies_supplywarehouse;
DROP TRIGGER IF EXISTS trg_supplies_category_tree ON supplies_supplycategory;
DROP TRIGGER IF EXISTS trg_supplies_item_company_immutable ON supplies_supplyitem;
DROP TRIGGER IF EXISTS trg_supplies_warehouse_company_immutable ON supplies_supplywarehouse;
DROP TRIGGER IF EXISTS trg_supplies_category_company_immutable ON supplies_supplycategory;
DROP FUNCTION IF EXISTS supplies_validate_item_references();
DROP FUNCTION IF EXISTS supplies_validate_warehouse_references();
DROP FUNCTION IF EXISTS supplies_validate_category_tree();
DROP FUNCTION IF EXISTS supplies_reject_company_change();
DROP FUNCTION IF EXISTS supplies_guard_manager_department_change();
DROP FUNCTION IF EXISTS supplies_guard_manager_employee_change();
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(CREATE_POSTGRESQL_GUARDS)


def uninstall_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(DROP_POSTGRESQL_GUARDS)


class Migration(migrations.Migration):
    dependencies = [
        ("supplies", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            code=install_postgresql_guards,
            reverse_code=uninstall_postgresql_guards,
        )
    ]
