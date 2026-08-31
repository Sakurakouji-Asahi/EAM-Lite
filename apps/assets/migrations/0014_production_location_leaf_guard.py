from django.db import migrations


POSTGRESQL_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION assets_guard_current_location_leaf()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.parent_id IS NOT DISTINCT FROM OLD.parent_id THEN
            RETURN NEW;
        END IF;
    END IF;
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Serialize a hierarchy change with assignment Services that lock the
    -- selected Location before validating it as a leaf.
    PERFORM id
      FROM masterdata_location
     WHERE id = NEW.parent_id
     FOR UPDATE;

    IF EXISTS (
        SELECT 1
          FROM assets_asset
         WHERE location_id = NEW.parent_id
           AND record_status = 'active'
           AND asset_status IN (
               'pending_finance', 'pending_label', 'in_use', 'idle',
               'loaned', 'under_repair', 'pending_disposal'
           )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'current formal asset location must remain a leaf';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_assets_current_location_leaf
    ON masterdata_location;
CREATE TRIGGER trg_assets_current_location_leaf
BEFORE INSERT OR UPDATE OF parent_id ON masterdata_location
FOR EACH ROW EXECUTE FUNCTION assets_guard_current_location_leaf();
"""


POSTGRESQL_GUARD_REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_assets_current_location_leaf
    ON masterdata_location;
DROP FUNCTION IF EXISTS assets_guard_current_location_leaf();
"""


def ensure_existing_locations_are_valid(apps, schema_editor):
    """Fail the upgrade instead of preserving a known-invalid current state."""

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT asset.location_id)
              FROM assets_asset asset
             WHERE asset.record_status = 'active'
               AND asset.asset_status IN (
                   'pending_finance', 'pending_label', 'in_use', 'idle',
                   'loaned', 'under_repair', 'pending_disposal'
               )
               AND EXISTS (
                   SELECT 1
                     FROM masterdata_location child
                    WHERE child.parent_id = asset.location_id
               )
            """
        )
        asset_count, location_count = cursor.fetchone()
    if asset_count:
        raise RuntimeError(
            "无法安装当前资产位置叶级保护："
            f"发现 {asset_count} 项当前正式资产位于 {location_count} 个非叶级位置；"
            "请先通过受控业务流程整改后重试迁移。"
        )


def install_guard(apps, schema_editor):
    ensure_existing_locations_are_valid(apps, schema_editor)
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(POSTGRESQL_GUARD_SQL)


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(POSTGRESQL_GUARD_REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0013_assetexternalreference"),
    ]

    operations = [
        migrations.RunPython(install_guard, reverse_code=remove_guard),
    ]
