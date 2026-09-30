from django.db import migrations


CREATE_GUARD = """
CREATE FUNCTION supplies_guard_referenced_item_unit_u11()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.unit IS DISTINCT FROM OLD.unit AND (
        EXISTS (SELECT 1 FROM supplies_supplydocumentline WHERE item_id = OLD.id)
        OR EXISTS (SELECT 1 FROM supplies_supplystockbalance WHERE item_id = OLD.id)
        OR EXISTS (SELECT 1 FROM supplies_supplystockledger WHERE item_id = OLD.id)
        OR EXISTS (SELECT 1 FROM supplies_supplycustody WHERE item_id = OLD.id)
        OR EXISTS (SELECT 1 FROM supplies_supplycustodymovement WHERE item_id = OLD.id)
        OR EXISTS (SELECT 1 FROM supplies_supplycountline WHERE item_id = OLD.id)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'referenced supply item unit is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_supply_item_unit_u11
BEFORE UPDATE OF unit ON supplies_supplyitem
FOR EACH ROW EXECUTE FUNCTION supplies_guard_referenced_item_unit_u11();
"""

DROP_GUARD = """
DROP TRIGGER IF EXISTS trg_supply_item_unit_u11 ON supplies_supplyitem;
DROP FUNCTION IF EXISTS supplies_guard_referenced_item_unit_u11();
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(CREATE_GUARD)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(DROP_GUARD)


class Migration(migrations.Migration):
    dependencies = [("supplies", "0010_inventory_accounting_invariants")]
    operations = [migrations.RunPython(install, uninstall)]
