from django.db import migrations


CREATE_SQL = r"""
CREATE OR REPLACE FUNCTION masterdata_guard_sequence_counter_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    controlled_increment boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'sequence counter history cannot be deleted';
    END IF;
    IF ROW(NEW.company_id, NEW.coding_scheme_id, NEW.scope_key)
       IS DISTINCT FROM
       ROW(OLD.company_id, OLD.coding_scheme_id, OLD.scope_key) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'sequence counter identity is immutable';
    END IF;
    IF NEW.current_value < OLD.current_value THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'sequence counter cannot decrease';
    END IF;
    IF NEW.current_value = OLD.current_value THEN
        RETURN NEW;
    END IF;
    controlled_increment := COALESCE(
        current_setting('eam_lite.controlled_sequence_counter_increment', true), ''
    ) = 'on';
    IF NOT controlled_increment THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'sequence counter increments must use the controlled issuance service';
    END IF;
    PERFORM set_config(
        'eam_lite.controlled_sequence_counter_increment', 'off', true
    );
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_issued_code()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    scheme_company_id bigint;
    scheme_status varchar;
    scheme_from date;
    scheme_to date;
    scheme_sequence_start bigint;
    shanghai_today date;
    issued_business_date date;
    actor_cleared boolean;
BEGIN
    SELECT company_id, status, effective_from, effective_to, sequence_start
      INTO scheme_company_id, scheme_status, scheme_from, scheme_to,
           scheme_sequence_start
      FROM masterdata_assetcodingscheme
     WHERE id = NEW.coding_scheme_id;
    shanghai_today := (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date;
    issued_business_date := (NEW.issued_at AT TIME ZONE 'Asia/Shanghai')::date;
    IF scheme_company_id IS NULL OR scheme_company_id <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'issued code scheme must be in the same company';
    END IF;
    IF scheme_status <> 'active'
       OR NEW.effective_date < scheme_from
       OR (scheme_to IS NOT NULL AND NEW.effective_date > scheme_to) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'issued code requires an active scheme covering its effective date';
    END IF;
    IF NEW.sequence_value < scheme_sequence_start THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'issued code sequence is below the scheme start';
    END IF;
    IF TG_OP = 'INSERT' AND NEW.status <> 'active' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'new issued code must start active';
    END IF;
    IF NEW.effective_date > shanghai_today THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'issued code effective date cannot be in the future';
    END IF;
    IF NEW.effective_date < issued_business_date AND btrim(NEW.effective_date_reason) = '' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'historical issued code requires a reason';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        actor_cleared := OLD.issued_by_id IS NOT NULL AND NEW.issued_by_id IS NULL;
        IF (NEW.company_id, NEW.coding_scheme_id, NEW.scope_key,
            NEW.sequence_value, NEW.display_code, NEW.normalized_code,
            NEW.effective_date, NEW.effective_date_reason, NEW.idempotency_key,
            NEW.issued_at)
           IS DISTINCT FROM
           (OLD.company_id, OLD.coding_scheme_id, OLD.scope_key,
            OLD.sequence_value, OLD.display_code, OLD.normalized_code,
            OLD.effective_date, OLD.effective_date_reason, OLD.idempotency_key,
            OLD.issued_at)
           OR (NEW.issued_by_id IS DISTINCT FROM OLD.issued_by_id AND NOT actor_cleared) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'issued code identity is immutable';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            IF OLD.status <> 'active' OR NEW.status NOT IN ('replaced', 'voided') THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid issued code status transition';
            END IF;
        ELSIF ROW(NEW.replaced_or_voided_reason, NEW.replaced_or_voided_at)
              IS DISTINCT FROM
              ROW(OLD.replaced_or_voided_reason, OLD.replaced_or_voided_at) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'issued code status evidence is immutable';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sequence_counter_history_guard
    ON masterdata_sequencecounter;
CREATE TRIGGER trg_sequence_counter_history_guard
BEFORE UPDATE OR DELETE ON masterdata_sequencecounter
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_sequence_counter_history();
"""


DROP_SQL = r"""
DROP TRIGGER IF EXISTS trg_sequence_counter_history_guard
    ON masterdata_sequencecounter;
DROP FUNCTION IF EXISTS masterdata_guard_sequence_counter_history();
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)
        from importlib import import_module

        previous = import_module(
            "apps.masterdata.migrations.0005_sprint2_postgresql_coding_guards"
        )
        schema_editor.execute(previous.CREATE_SQL)


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0007_assetcategory_default_depreciation_policy")]

    operations = [
        migrations.RunPython(
            install_postgresql_guards,
            reverse_code=remove_postgresql_guards,
        )
    ]
