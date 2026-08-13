from django.db import migrations


ENTRY_SOURCE_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION finance_sprint11_entry_base_source_is_posted(
    target_entry uuid
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    entry_source varchar;
    entry_company bigint;
    entry_asset uuid;
    entry_profile uuid;
    entry_batch_item uuid;
    entry_opening_profile uuid;
    entry_adjustment uuid;
    source_company bigint;
    source_asset uuid;
    source_profile uuid;
    source_status varchar;
    source_type varchar;
    item_status varchar;
    profile_version integer;
BEGIN
    SELECT entry.source_type, entry.company_id, entry.asset_id,
           entry.depreciation_profile_id, entry.batch_item_id,
           entry.opening_profile_id, entry.value_adjustment_id
      INTO entry_source, entry_company, entry_asset, entry_profile,
           entry_batch_item, entry_opening_profile, entry_adjustment
      FROM finance_depreciationentry entry
     WHERE entry.id = target_entry;
    IF NOT FOUND THEN
        RETURN true;
    END IF;

    IF entry_source = 'batch' THEN
        SELECT item.company_id, item.asset_id, item.depreciation_profile_id,
               item.status, batch.status
          INTO source_company, source_asset, source_profile,
               item_status, source_status
          FROM finance_depreciationbatchitem item
          JOIN finance_depreciationbatch batch ON batch.id = item.batch_id
         WHERE item.id = entry_batch_item;
        RETURN FOUND
           AND source_company = entry_company
           AND source_asset = entry_asset
           AND source_profile = entry_profile
           AND item_status = 'ready'
           AND source_status IN ('confirmed', 'reversed');
    ELSIF entry_source = 'opening' THEN
        SELECT company_id, asset_id, id, status, version
          INTO source_company, source_asset, source_profile,
               source_status, profile_version
          FROM finance_assetdepreciationprofile
         WHERE id = entry_opening_profile;
        RETURN FOUND
           AND source_company = entry_company
           AND source_asset = entry_asset
           AND source_profile = entry_profile
           AND entry_opening_profile = entry_profile
           AND profile_version = 1
           AND source_status IN ('active', 'suspended', 'completed', 'stopped');
    ELSIF entry_source = 'adjustment' THEN
        SELECT company_id, asset_id, adjustment_type, status
          INTO source_company, source_asset, source_type, source_status
          FROM finance_assetvalueadjustment
         WHERE id = entry_adjustment;
        RETURN FOUND
           AND source_company = entry_company
           AND source_asset = entry_asset
           AND source_type = 'depreciation_adjustment'
           AND source_status IN ('confirmed', 'reversed');
    END IF;
    RETURN false;
END;
$$;

CREATE OR REPLACE FUNCTION finance_sprint11_assert_entry_source(
    target_entry uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    entry_source varchar;
    entry_batch_item uuid;
    entry_adjustment uuid;
    entry_reversal_of uuid;
    source_batch uuid;
    source_batch_type varchar;
    source_batch_status varchar;
    source_reverses_batch uuid;
    source_adjustment_status varchar;
    source_reverses_adjustment uuid;
    original_source varchar;
    original_batch_item uuid;
    original_adjustment uuid;
    original_batch uuid;
    original_batch_type varchar;
    original_batch_status varchar;
    original_adjustment_status varchar;
    linked_reversal uuid;
BEGIN
    SELECT source_type, batch_item_id, value_adjustment_id, reversal_of_id
      INTO entry_source, entry_batch_item, entry_adjustment, entry_reversal_of
      FROM finance_depreciationentry
     WHERE id = target_entry;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF NOT finance_sprint11_entry_base_source_is_posted(target_entry) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation entry requires a posted source';
    END IF;

    IF entry_source = 'batch' THEN
        SELECT batch.id, batch.batch_type, batch.status, batch.reverses_batch_id
          INTO source_batch, source_batch_type, source_batch_status,
               source_reverses_batch
          FROM finance_depreciationbatchitem item
          JOIN finance_depreciationbatch batch ON batch.id = item.batch_id
         WHERE item.id = entry_batch_item;
    ELSIF entry_source = 'adjustment' THEN
        SELECT status, reversal_of_id
          INTO source_adjustment_status, source_reverses_adjustment
          FROM finance_assetvalueadjustment
         WHERE id = entry_adjustment;
    END IF;

    IF entry_reversal_of IS NULL THEN
        IF entry_source = 'batch' AND source_batch_type = 'reversal' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'reversal batch entry requires an original entry';
        ELSIF entry_source = 'batch' AND source_batch_status = 'reversed' THEN
            SELECT id INTO linked_reversal
              FROM finance_depreciationentry
             WHERE reversal_of_id = target_entry;
            IF linked_reversal IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'reversed depreciation batch requires complete entry reversals';
            END IF;
            PERFORM finance_sprint11_assert_entry_source(linked_reversal);
        ELSIF entry_source = 'adjustment'
              AND source_reverses_adjustment IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'reversal adjustment entry requires an original entry';
        END IF;
        RETURN;
    END IF;

    IF NOT finance_sprint11_entry_base_source_is_posted(entry_reversal_of) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation entry reversal requires a posted original source';
    END IF;
    SELECT source_type, batch_item_id, value_adjustment_id
      INTO original_source, original_batch_item, original_adjustment
      FROM finance_depreciationentry
     WHERE id = entry_reversal_of;
    IF entry_source IS DISTINCT FROM original_source THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation entry reversal source type is inconsistent';
    END IF;

    IF entry_source = 'batch' THEN
        SELECT batch.id, batch.batch_type, batch.status
          INTO original_batch, original_batch_type, original_batch_status
          FROM finance_depreciationbatchitem item
          JOIN finance_depreciationbatch batch ON batch.id = item.batch_id
         WHERE item.id = original_batch_item;
        IF source_batch_type <> 'reversal'
           OR source_batch_status <> 'confirmed'
           OR source_reverses_batch IS DISTINCT FROM original_batch
           OR original_batch_type <> 'regular'
           OR original_batch_status <> 'reversed' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'depreciation entry reversal batch chain is invalid';
        END IF;
    ELSIF entry_source = 'adjustment' THEN
        SELECT status INTO original_adjustment_status
          FROM finance_assetvalueadjustment
         WHERE id = original_adjustment;
        IF source_adjustment_status <> 'confirmed'
           OR source_reverses_adjustment IS DISTINCT FROM original_adjustment
           OR original_adjustment_status <> 'reversed' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'depreciation entry reversal adjustment chain is invalid';
        END IF;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'opening depreciation entry cannot be a reversal';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_sprint11_validate_entry_source_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    changed_row jsonb;
    target_id uuid;
    affected_entry uuid;
BEGIN
    IF TG_TABLE_NAME = 'finance_depreciationentry' THEN
        changed_row := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
        target_id := (changed_row ->> 'id')::uuid;
        PERFORM finance_sprint11_assert_entry_source(target_id);
        FOR affected_entry IN
            SELECT id FROM finance_depreciationentry WHERE reversal_of_id = target_id
        LOOP
            PERFORM finance_sprint11_assert_entry_source(affected_entry);
        END LOOP;
    ELSIF TG_TABLE_NAME = 'finance_depreciationbatch' THEN
        changed_row := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
        target_id := (changed_row ->> 'id')::uuid;
        FOR affected_entry IN
            WITH source_entries AS (
                SELECT entry.id
                  FROM finance_depreciationentry entry
                  JOIN finance_depreciationbatchitem item
                    ON item.id = entry.batch_item_id
                 WHERE item.batch_id = target_id
            )
            SELECT id FROM source_entries
            UNION
            SELECT entry.id
              FROM finance_depreciationentry entry
             WHERE entry.reversal_of_id IN (SELECT id FROM source_entries)
        LOOP
            PERFORM finance_sprint11_assert_entry_source(affected_entry);
        END LOOP;
    ELSIF TG_TABLE_NAME = 'finance_depreciationbatchitem' THEN
        changed_row := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
        target_id := (changed_row ->> 'id')::uuid;
        FOR affected_entry IN
            WITH source_entries AS (
                SELECT id FROM finance_depreciationentry
                 WHERE batch_item_id = target_id
            )
            SELECT id FROM source_entries
            UNION
            SELECT entry.id
              FROM finance_depreciationentry entry
             WHERE entry.reversal_of_id IN (SELECT id FROM source_entries)
        LOOP
            PERFORM finance_sprint11_assert_entry_source(affected_entry);
        END LOOP;
    ELSIF TG_TABLE_NAME = 'finance_assetvalueadjustment' THEN
        changed_row := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
        target_id := (changed_row ->> 'id')::uuid;
        FOR affected_entry IN
            WITH source_entries AS (
                SELECT id FROM finance_depreciationentry
                 WHERE value_adjustment_id = target_id
            )
            SELECT id FROM source_entries
            UNION
            SELECT entry.id
              FROM finance_depreciationentry entry
             WHERE entry.reversal_of_id IN (SELECT id FROM source_entries)
        LOOP
            PERFORM finance_sprint11_assert_entry_source(affected_entry);
        END LOOP;
    ELSIF TG_TABLE_NAME = 'finance_assetdepreciationprofile' THEN
        changed_row := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
        target_id := (changed_row ->> 'id')::uuid;
        FOR affected_entry IN
            WITH source_entries AS (
                SELECT id FROM finance_depreciationentry
                 WHERE depreciation_profile_id = target_id
                    OR opening_profile_id = target_id
            )
            SELECT id FROM source_entries
            UNION
            SELECT entry.id
              FROM finance_depreciationentry entry
             WHERE entry.reversal_of_id IN (SELECT id FROM source_entries)
        LOOP
            PERFORM finance_sprint11_assert_entry_source(affected_entry);
        END LOOP;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_finance_s11_entry_source_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_depreciationentry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_sprint11_validate_entry_source_commit();

CREATE CONSTRAINT TRIGGER trg_finance_s11_batch_entry_source_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_depreciationbatch
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_sprint11_validate_entry_source_commit();

CREATE CONSTRAINT TRIGGER trg_finance_s11_batch_item_entry_source_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_depreciationbatchitem
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_sprint11_validate_entry_source_commit();

CREATE CONSTRAINT TRIGGER trg_finance_s11_adjustment_entry_source_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_assetvalueadjustment
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_sprint11_validate_entry_source_commit();

CREATE CONSTRAINT TRIGGER trg_finance_s11_profile_entry_source_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_assetdepreciationprofile
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_sprint11_validate_entry_source_commit();
"""


ENTRY_SOURCE_GUARDS_REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_finance_s11_profile_entry_source_commit
    ON finance_assetdepreciationprofile;
DROP TRIGGER IF EXISTS trg_finance_s11_adjustment_entry_source_commit
    ON finance_assetvalueadjustment;
DROP TRIGGER IF EXISTS trg_finance_s11_batch_item_entry_source_commit
    ON finance_depreciationbatchitem;
DROP TRIGGER IF EXISTS trg_finance_s11_batch_entry_source_commit
    ON finance_depreciationbatch;
DROP TRIGGER IF EXISTS trg_finance_s11_entry_source_commit
    ON finance_depreciationentry;
DROP FUNCTION IF EXISTS finance_sprint11_validate_entry_source_commit();
DROP FUNCTION IF EXISTS finance_sprint11_assert_entry_source(uuid);
DROP FUNCTION IF EXISTS finance_sprint11_entry_base_source_is_posted(uuid);
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(ENTRY_SOURCE_GUARDS_SQL)


def remove_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(ENTRY_SOURCE_GUARDS_REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0007_remove_depreciationprofileevent_ck_depr_event_type_valid_and_more"),
    ]

    operations = [
        migrations.RunPython(
            install_postgresql_guards,
            reverse_code=remove_postgresql_guards,
        ),
    ]
