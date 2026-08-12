from django.db import migrations


CREATE_SQL = """
CREATE OR REPLACE FUNCTION masterdata_guard_finite_decimal_setting()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    numeric_value numeric;
BEGIN
    IF NEW.key <> 'fixed_asset_warning_amount' THEN
        RETURN NEW;
    END IF;
    BEGIN
        numeric_value := NEW.value::numeric;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixed asset warning amount must be decimal';
    END;
    IF numeric_value::text IN ('NaN', 'Infinity', '-Infinity') OR numeric_value < 0 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixed asset warning amount must be finite and non-negative';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_systemsetting_finite_decimal ON masterdata_systemsetting;
CREATE TRIGGER trg_systemsetting_finite_decimal
BEFORE INSERT OR UPDATE OF key, value ON masterdata_systemsetting
FOR EACH ROW
EXECUTE FUNCTION masterdata_guard_finite_decimal_setting();
"""


DROP_SQL = """
DROP TRIGGER IF EXISTS trg_systemsetting_finite_decimal ON masterdata_systemsetting;
DROP FUNCTION IF EXISTS masterdata_guard_finite_decimal_setting();
"""


def install_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0002_postgresql_integrity_triggers")]

    operations = [
        migrations.RunPython(
            install_postgresql_guard,
            reverse_code=remove_postgresql_guard,
        )
    ]
