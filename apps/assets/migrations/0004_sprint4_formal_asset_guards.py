from importlib import import_module

from django.db import migrations


CREATE_SQL = r"""
CREATE OR REPLACE FUNCTION assets_validate_asset_references()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    category_company bigint;
    category_active boolean;
    department_company bigint;
    department_active boolean;
    employee_company bigint;
    employee_department bigint;
    employee_status varchar;
    employee_active boolean;
    employee_department_active boolean;
    location_company bigint;
    location_active boolean;
    scheme_company bigint;
    scheme_status varchar;
    scheme_from date;
    scheme_to date;
    shanghai_today date;
    controlled_mutation boolean;
    actor_cleared boolean;
BEGIN
    IF TG_OP = 'INSERT' AND NEW.asset_status <> 'draft' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'assets must be created as drafts';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        controlled_mutation := COALESCE(
            current_setting('eam_lite.controlled_asset_mutation', true), ''
        ) = 'on';
        actor_cleared := OLD.submitted_by_id IS NOT NULL
            AND NEW.submitted_by_id IS NULL
            AND ROW(
                NEW.asset_status, NEW.record_status, NEW.asset_code,
                NEW.current_issued_code_id, NEW.requested_coding_scheme_id,
                NEW.submitted_at
            ) IS NOT DISTINCT FROM ROW(
                OLD.asset_status, OLD.record_status, OLD.asset_code,
                OLD.current_issued_code_id, OLD.requested_coding_scheme_id,
                OLD.submitted_at
            );
        IF ROW(
            NEW.asset_status, NEW.record_status, NEW.asset_code,
            NEW.current_issued_code_id, NEW.requested_coding_scheme_id,
            NEW.submitted_by_id, NEW.submitted_at
        ) IS DISTINCT FROM ROW(
            OLD.asset_status, OLD.record_status, OLD.asset_code,
            OLD.current_issued_code_id, OLD.requested_coding_scheme_id,
            OLD.submitted_by_id, OLD.submitted_at
        ) AND NOT controlled_mutation AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset protected state must be changed by the controlled service';
        END IF;
        IF controlled_mutation AND ROW(
            NEW.asset_status, NEW.record_status, NEW.asset_code,
            NEW.current_issued_code_id, NEW.requested_coding_scheme_id,
            NEW.submitted_by_id, NEW.submitted_at
        ) IS DISTINCT FROM ROW(
            OLD.asset_status, OLD.record_status, OLD.asset_code,
            OLD.current_issued_code_id, OLD.requested_coding_scheme_id,
            OLD.submitted_by_id, OLD.submitted_at
        ) THEN
            PERFORM set_config('eam_lite.controlled_asset_mutation', 'off', true);
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.asset_status IS DISTINCT FROM OLD.asset_status
       AND NOT (
           (OLD.asset_status = 'draft' AND NEW.asset_status = 'pending_finance')
           OR (OLD.asset_status = 'pending_finance' AND NEW.asset_status = 'draft')
           OR (OLD.asset_status = 'pending_finance' AND NEW.asset_status = 'pending_label')
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset status transition is not enabled in Sprint 4';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.asset_status = 'pending_label'
       AND NEW.asset_status = 'pending_label'
       AND ROW(NEW.asset_code, NEW.current_issued_code_id, NEW.requested_coding_scheme_id)
           IS DISTINCT FROM
           ROW(OLD.asset_code, OLD.current_issued_code_id, OLD.requested_coding_scheme_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'formal asset code and requested coding scheme are immutable in Sprint 4';
    END IF;

    SELECT company_id, is_active INTO category_company, category_active
      FROM masterdata_assetcategory WHERE id = NEW.category_id
      FOR SHARE;
    IF category_company IS NULL OR category_company <> NEW.company_id OR NOT category_active THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset category must be active and in the same company';
    END IF;

    IF NEW.department_id IS NOT NULL THEN
        SELECT company_id, is_active INTO department_company, department_active
          FROM masterdata_department WHERE id = NEW.department_id
          FOR SHARE;
        IF department_company IS NULL OR department_company <> NEW.company_id OR NOT department_active THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset department must be active and in the same company';
        END IF;
    END IF;

    IF NEW.responsible_employee_id IS NOT NULL THEN
        SELECT employee.company_id, employee.department_id,
               employee.employment_status, employee.is_active,
               department.is_active
          INTO employee_company, employee_department, employee_status,
               employee_active, employee_department_active
          FROM masterdata_employee employee
          JOIN masterdata_department department ON department.id = employee.department_id
         WHERE employee.id = NEW.responsible_employee_id
         FOR SHARE OF employee, department;
        IF employee_company IS NULL OR employee_company <> NEW.company_id
           OR NEW.department_id IS NULL OR employee_department <> NEW.department_id
           OR employee_status <> 'active' OR NOT employee_active
           OR NOT employee_department_active THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset responsible employee must be active in the asset department and company';
        END IF;
    END IF;

    IF NEW.location_id IS NOT NULL THEN
        SELECT company_id, is_active INTO location_company, location_active
          FROM masterdata_location WHERE id = NEW.location_id
          FOR SHARE;
        IF location_company IS NULL OR location_company <> NEW.company_id OR NOT location_active THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset location must be active and in the same company';
        END IF;
    END IF;

    IF NEW.requested_coding_scheme_id IS NOT NULL THEN
        SELECT company_id, status, effective_from, effective_to
          INTO scheme_company, scheme_status, scheme_from, scheme_to
          FROM masterdata_assetcodingscheme
         WHERE id = NEW.requested_coding_scheme_id;
        shanghai_today := (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date;
        IF scheme_company IS NULL OR scheme_company <> NEW.company_id
           OR scheme_status <> 'active' OR scheme_from > shanghai_today
           OR (scheme_to IS NOT NULL AND scheme_to < shanghai_today) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'requested coding scheme must be current and in the same company';
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.category_id IS DISTINCT FROM OLD.category_id
       AND EXISTS (
           SELECT 1
             FROM assets_assetcustomvalue value
             JOIN assets_assetcustomfield field ON field.id = value.custom_field_id
            WHERE value.asset_id = NEW.id AND field.category_id <> NEW.category_id
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'remove or migrate incompatible custom values before changing category';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_validate_formal_asset_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_asset_id uuid;
    target_asset assets_asset%%ROWTYPE;
    issued_company bigint;
    issued_display varchar;
    issued_status varchar;
    finance_id uuid;
    finance_company bigint;
    finance_treatment varchar;
    finance_cost numeric;
    finance_confirmed timestamptz;
    qr_count integer;
    formalization_count integer;
BEGIN
    IF TG_TABLE_NAME = 'assets_asset' THEN
        target_asset_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSIF TG_TABLE_NAME IN (
        'finance_assetfinance', 'assets_assetqridentity',
        'finance_assetdepreciationprofile', 'finance_financeformalizationrequest'
    ) THEN
        target_asset_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.asset_id ELSE NEW.asset_id END;
    ELSIF TG_TABLE_NAME IN (
        'finance_depreciationentry', 'finance_assetvalueadjustment'
    ) THEN
        target_asset_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.asset_id ELSE NEW.asset_id END;
    ELSIF TG_TABLE_NAME = 'masterdata_issuedcode' THEN
        SELECT asset.id INTO target_asset_id
          FROM assets_asset asset
         WHERE asset.current_issued_code_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSE
        RETURN NULL;
    END IF;
    SELECT * INTO target_asset FROM assets_asset WHERE id = target_asset_id;
    IF NOT FOUND OR target_asset.asset_status <> 'pending_label' THEN
        RETURN NULL;
    END IF;

    SELECT company_id, display_code, status
      INTO issued_company, issued_display, issued_status
      FROM masterdata_issuedcode WHERE id = target_asset.current_issued_code_id;
    IF issued_company IS NULL OR issued_company <> target_asset.company_id
       OR issued_display <> target_asset.asset_code OR issued_status <> 'active' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'pending label asset requires its active issued code mirror';
    END IF;

    SELECT id, company_id, accounting_treatment, original_cost, finance_confirmed_at
      INTO finance_id, finance_company, finance_treatment, finance_cost, finance_confirmed
      FROM finance_assetfinance WHERE asset_id = target_asset.id;
    IF finance_id IS NULL OR finance_company <> target_asset.company_id
       OR finance_confirmed IS NULL OR finance_treatment IS NULL OR finance_cost IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'pending label asset requires confirmed complete finance';
    END IF;

    SELECT count(*) INTO qr_count FROM assets_assetqridentity
     WHERE asset_id = target_asset.id AND company_id = target_asset.company_id
       AND status = 'active' AND label_status = 'ready_to_print';
    IF qr_count <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'pending label asset requires exactly one active ready-to-print QR identity';
    END IF;

    IF finance_treatment = 'fixed_asset' THEN
        IF (SELECT count(*) FROM finance_assetdepreciationprofile
             WHERE asset_id = target_asset.id AND company_id = target_asset.company_id
               AND status = 'active') <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'fixed asset formalization requires one active depreciation profile';
        END IF;
    ELSE
        IF EXISTS (
            SELECT 1 FROM finance_assetdepreciationprofile
             WHERE asset_id = target_asset.id AND status IN ('active', 'suspended')
        ) OR EXISTS (
            SELECT 1 FROM finance_depreciationentry WHERE asset_id = target_asset.id
        ) OR EXISTS (
            SELECT 1 FROM finance_assetvalueadjustment WHERE asset_id = target_asset.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'controlled non-fixed asset cannot have depreciation or value adjustment history';
        END IF;
    END IF;

    SELECT count(*) INTO formalization_count
      FROM finance_financeformalizationrequest request
     WHERE request.asset_id = target_asset.id
       AND request.company_id = target_asset.company_id
       AND request.result_finance_id = finance_id
       AND request.result_issued_code_id = target_asset.current_issued_code_id
       AND request.status = 'completed';
    IF formalization_count <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'pending label asset requires its durable formalization idempotency result';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION assets_validate_qr_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    current_asset_status varchar;
    controlled_qr boolean;
    actors_cleared boolean;
BEGIN
    SELECT asset.company_id, asset.asset_status
      INTO asset_company, current_asset_status
      FROM assets_asset AS asset WHERE asset.id = NEW.asset_id FOR SHARE;
    IF asset_company IS NULL OR asset_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'QR identity must be in the asset company';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'active' OR NEW.label_status <> 'ready_to_print'
           OR current_asset_status <> 'pending_finance' OR NEW.issued_by_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Sprint 4 QR identity is created active and ready-to-print during formalization';
        END IF;
    ELSE
        actors_cleared := (NEW.issued_by_id IS NOT DISTINCT FROM OLD.issued_by_id
                           OR (OLD.issued_by_id IS NOT NULL AND NEW.issued_by_id IS NULL))
            AND (NEW.revoked_by_id IS NOT DISTINCT FROM OLD.revoked_by_id
                 OR (OLD.revoked_by_id IS NOT NULL AND NEW.revoked_by_id IS NULL))
            AND (NEW.attached_by_id IS NOT DISTINCT FROM OLD.attached_by_id
                 OR (OLD.attached_by_id IS NOT NULL AND NEW.attached_by_id IS NULL));
        IF NOT actors_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'QR identity actors are immutable';
        END IF;
        IF ROW(NEW.company_id, NEW.asset_id, NEW.public_token, NEW.issued_at, NEW.version)
           IS DISTINCT FROM
           ROW(OLD.company_id, OLD.asset_id, OLD.public_token, OLD.issued_at, OLD.version) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'QR identity and token are immutable';
        END IF;
        IF ROW(NEW.status, NEW.label_status, NEW.revoked_at,
               NEW.revoke_reason, NEW.attached_at)
           IS DISTINCT FROM
           ROW(OLD.status, OLD.label_status, OLD.revoked_at,
               OLD.revoke_reason, OLD.attached_at) THEN
            controlled_qr := COALESCE(
                current_setting('eam_lite.controlled_qr_identity_mutation', true), ''
            ) = 'on';
            IF NOT controlled_qr THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'QR state must be changed by the controlled service';
            END IF;
            IF OLD.status = 'revoked' OR NEW.status NOT IN ('active', 'revoked')
               OR (OLD.status = 'active' AND NEW.status = 'active'
                   AND NEW.label_status <> OLD.label_status) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'QR printing, attachment and replacement are not enabled in Sprint 4';
            END IF;
            PERFORM set_config('eam_lite.controlled_qr_identity_mutation', 'off', true);
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_guard_qr_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23503',
        MESSAGE = 'QR identity history cannot be deleted';
END;
$$;

DROP TRIGGER IF EXISTS trg_asset_references ON assets_asset;
CREATE TRIGGER trg_asset_references
BEFORE INSERT OR UPDATE ON assets_asset
FOR EACH ROW EXECUTE FUNCTION assets_validate_asset_references();

DROP TRIGGER IF EXISTS trg_sprint4_formal_asset_commit ON assets_asset;
CREATE CONSTRAINT TRIGGER trg_sprint4_formal_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON assets_asset
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_sprint4_finance_asset_commit ON finance_assetfinance;
CREATE CONSTRAINT TRIGGER trg_sprint4_finance_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_assetfinance
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_sprint4_qr_asset_commit ON assets_assetqridentity;
CREATE CONSTRAINT TRIGGER trg_sprint4_qr_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON assets_assetqridentity
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_sprint4_profile_asset_commit ON finance_assetdepreciationprofile;
CREATE CONSTRAINT TRIGGER trg_sprint4_profile_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_assetdepreciationprofile
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_sprint4_entry_asset_commit ON finance_depreciationentry;
CREATE CONSTRAINT TRIGGER trg_sprint4_entry_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_depreciationentry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_sprint4_adjustment_asset_commit ON finance_assetvalueadjustment;
CREATE CONSTRAINT TRIGGER trg_sprint4_adjustment_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_assetvalueadjustment
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_sprint4_formalization_asset_commit ON finance_financeformalizationrequest;
CREATE CONSTRAINT TRIGGER trg_sprint4_formalization_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_financeformalizationrequest
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_sprint4_issued_asset_commit ON masterdata_issuedcode;
CREATE CONSTRAINT TRIGGER trg_sprint4_issued_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON masterdata_issuedcode
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_formal_asset_commit();

DROP TRIGGER IF EXISTS trg_assets_qr_validate ON assets_assetqridentity;
CREATE TRIGGER trg_assets_qr_validate
BEFORE INSERT OR UPDATE ON assets_assetqridentity
FOR EACH ROW EXECUTE FUNCTION assets_validate_qr_identity();

DROP TRIGGER IF EXISTS trg_assets_qr_delete ON assets_assetqridentity;
CREATE TRIGGER trg_assets_qr_delete
BEFORE DELETE ON assets_assetqridentity
FOR EACH ROW EXECUTE FUNCTION assets_guard_qr_delete();
"""


DROP_SQL = r"""
DROP TRIGGER IF EXISTS trg_assets_qr_delete ON assets_assetqridentity;
DROP TRIGGER IF EXISTS trg_assets_qr_validate ON assets_assetqridentity;
DROP TRIGGER IF EXISTS trg_sprint4_issued_asset_commit ON masterdata_issuedcode;
DROP TRIGGER IF EXISTS trg_sprint4_formalization_asset_commit ON finance_financeformalizationrequest;
DROP TRIGGER IF EXISTS trg_sprint4_adjustment_asset_commit ON finance_assetvalueadjustment;
DROP TRIGGER IF EXISTS trg_sprint4_entry_asset_commit ON finance_depreciationentry;
DROP TRIGGER IF EXISTS trg_sprint4_profile_asset_commit ON finance_assetdepreciationprofile;
DROP TRIGGER IF EXISTS trg_sprint4_qr_asset_commit ON assets_assetqridentity;
DROP TRIGGER IF EXISTS trg_sprint4_finance_asset_commit ON finance_assetfinance;
DROP TRIGGER IF EXISTS trg_sprint4_formal_asset_commit ON assets_asset;
DROP TRIGGER IF EXISTS trg_asset_references ON assets_asset;
DROP FUNCTION IF EXISTS assets_guard_qr_delete();
DROP FUNCTION IF EXISTS assets_validate_qr_identity();
DROP FUNCTION IF EXISTS assets_validate_formal_asset_commit();
DROP FUNCTION IF EXISTS assets_validate_asset_references();
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)
        previous = import_module(
            "apps.assets.migrations.0002_postgresql_asset_guards"
        )
        schema_editor.execute(previous.CREATE_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0003_assetqridentity"),
        ("finance", "0004_postgresql_finance_guards"),
    ]

    operations = [
        migrations.RunPython(
            install_postgresql_guards,
            reverse_code=remove_postgresql_guards,
        )
    ]
