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
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Sprint 3 assets must be created as drafts';
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
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset status transition is not enabled in Sprint 3';
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

CREATE OR REPLACE FUNCTION assets_validate_attachment_availability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.is_available
       AND NEW.malware_scan_status NOT IN ('policy_limited', 'clean') THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'available attachment requires an allowed scan status';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_validate_asset_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_asset_id uuid;
    target_asset assets_asset%%ROWTYPE;
    issued_company bigint;
    issued_display varchar;
    issued_status varchar;
BEGIN
    IF TG_TABLE_NAME = 'assets_asset' THEN
        target_asset_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSIF TG_TABLE_NAME = 'assets_attachmentlink' THEN
        target_asset_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.asset_id ELSE NEW.asset_id END;
    ELSE
        target_asset_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.asset_id ELSE NEW.asset_id END;
    END IF;
    SELECT * INTO target_asset FROM assets_asset WHERE id = target_asset_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF target_asset.current_issued_code_id IS NOT NULL THEN
        SELECT company_id, display_code, status
          INTO issued_company, issued_display, issued_status
          FROM masterdata_issuedcode WHERE id = target_asset.current_issued_code_id;
        IF issued_company IS NULL OR issued_company <> target_asset.company_id
           OR issued_display <> target_asset.asset_code OR issued_status <> 'active' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset code and current issued code are inconsistent';
        END IF;
    END IF;

    IF target_asset.asset_status = 'pending_finance' THEN
        IF btrim(target_asset.asset_name) = '' OR btrim(target_asset.unit) = ''
           OR target_asset.department_id IS NULL
           OR target_asset.responsible_employee_id IS NULL
           OR target_asset.location_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset physical data is incomplete';
        END IF;
        IF EXISTS (
            SELECT 1 FROM masterdata_location child
             WHERE child.parent_id = target_asset.location_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset requires a leaf location';
        END IF;
        PERFORM attachment.id
          FROM assets_attachmentlink link
          JOIN masterdata_attachment attachment ON attachment.id = link.attachment_id
         WHERE link.asset_id = target_asset.id
           AND link.status = 'active'
           AND link.role IN ('cover', 'photo')
         FOR SHARE OF attachment;
        IF NOT EXISTS (
            SELECT 1
              FROM assets_attachmentlink link
              JOIN masterdata_attachment attachment ON attachment.id = link.attachment_id
             WHERE link.asset_id = target_asset.id
               AND link.status = 'active'
               AND link.role IN ('cover', 'photo')
               AND link.security_class = 'A0'
               AND attachment.is_available
               AND attachment.malware_scan_status IN ('policy_limited', 'clean')
               AND attachment.mime_type LIKE 'image/%%'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset requires an available asset photo';
        END IF;
        PERFORM field.id
          FROM assets_assetcustomfield field
         WHERE field.company_id = target_asset.company_id
           AND field.category_id = target_asset.category_id
           AND field.is_active
         FOR SHARE OF field;
        IF EXISTS (
            SELECT 1
              FROM assets_assetcustomfield field
             WHERE field.company_id = target_asset.company_id
               AND field.category_id = target_asset.category_id
               AND field.is_active AND field.required
               AND NOT EXISTS (
                   SELECT 1 FROM assets_assetcustomvalue value
                    WHERE value.asset_id = target_asset.id
                      AND value.custom_field_id = field.id
                      AND (field.field_type <> 'text' OR btrim(value.value_text) <> '')
               )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset has missing required custom fields';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION assets_guard_asset_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    controlled_mutation boolean;
BEGIN
    controlled_mutation := COALESCE(
        current_setting('eam_lite.controlled_asset_mutation', true), ''
    ) = 'on';
    IF NOT controlled_mutation THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset deletion must use the controlled service';
    END IF;
    IF OLD.asset_status <> 'draft' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'submitted or formal assets cannot be deleted';
    END IF;
    PERFORM set_config('eam_lite.controlled_asset_mutation', 'off', true);
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION assets_reject_attachment_link_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment links are retained evidence and cannot be deleted';
END;
$$;

CREATE OR REPLACE FUNCTION assets_validate_custom_field()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    category_company bigint;
BEGIN
    SELECT company_id INTO category_company
      FROM masterdata_assetcategory WHERE id = NEW.category_id;
    IF category_company IS NULL OR category_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom field category must be in the same company';
    END IF;
    IF NEW.field_type NOT IN ('text', 'decimal', 'date', 'boolean', 'select') THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom field type is not approved';
    END IF;
    IF NEW.normalized_code IS NULL OR btrim(NEW.normalized_code) = ''
       OR NEW.code IS NULL OR btrim(NEW.code) = '' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom field code must be non-empty';
    END IF;
    IF NEW.field_type = 'select' THEN
        IF jsonb_typeof(NEW.options_json) <> 'array'
           OR jsonb_array_length(NEW.options_json) = 0
           OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(NEW.options_json) option
                WHERE jsonb_typeof(option) <> 'string'
                   OR btrim(option #>> '{}') = ''
                   OR option #>> '{}' <> btrim(option #>> '{}')
           )
           OR (SELECT count(*) FROM jsonb_array_elements_text(NEW.options_json))
              <> (SELECT count(DISTINCT option)
                    FROM jsonb_array_elements_text(NEW.options_json) option) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'select custom field options must be unique non-empty trimmed strings';
        END IF;
    ELSIF NEW.options_json IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'non-select custom field cannot have options';
    END IF;
    IF TG_OP = 'UPDATE' AND EXISTS (
        SELECT 1
          FROM assets_assetcustomvalue value
          JOIN assets_asset asset ON asset.id = value.asset_id
         WHERE value.custom_field_id = OLD.id
           AND asset.asset_status NOT IN ('draft', 'pending_finance')
    ) AND (NEW.company_id, NEW.category_id, NEW.name, NEW.code,
           NEW.normalized_code, NEW.field_type, NEW.options_json)
          IS DISTINCT FROM
          (OLD.company_id, OLD.category_id, OLD.name, OLD.code,
           OLD.normalized_code, OLD.field_type, OLD.options_json) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom field used by a formal asset is immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.field_type = 'select' AND EXISTS (
        SELECT 1 FROM assets_assetcustomvalue value
         WHERE value.custom_field_id = NEW.id
           AND NOT (NEW.options_json ? value.value_text)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom field options cannot orphan existing values';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_validate_custom_value()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    asset_category bigint;
    field_company bigint;
    field_category bigint;
    target_type varchar;
    target_options jsonb;
    field_active boolean;
BEGIN
    SELECT company_id, category_id INTO asset_company, field_category
      FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, category_id, field_type, options_json, is_active
      INTO field_company, field_category, target_type, target_options, field_active
      FROM assets_assetcustomfield WHERE id = NEW.custom_field_id;
    SELECT category_id INTO asset_category FROM assets_asset WHERE id = NEW.asset_id;
    IF asset_company IS NULL OR field_company IS NULL
       OR asset_company <> NEW.company_id OR field_company <> NEW.company_id
       OR field_category <> asset_category OR NOT field_active THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom value asset and field scope is invalid';
    END IF;
    IF (target_type IN ('text', 'select') AND NOT (
            NEW.value_text IS NOT NULL AND NEW.value_decimal IS NULL
            AND NEW.value_date IS NULL AND NEW.value_boolean IS NULL
        )) OR (target_type = 'decimal' AND NOT (
            NEW.value_text IS NULL AND NEW.value_decimal IS NOT NULL
            AND NEW.value_date IS NULL AND NEW.value_boolean IS NULL
        )) OR (target_type = 'date' AND NOT (
            NEW.value_text IS NULL AND NEW.value_decimal IS NULL
            AND NEW.value_date IS NOT NULL AND NEW.value_boolean IS NULL
        )) OR (target_type = 'boolean' AND NOT (
            NEW.value_text IS NULL AND NEW.value_decimal IS NULL
            AND NEW.value_date IS NULL AND NEW.value_boolean IS NOT NULL
        )) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom value is stored in the wrong typed column';
    END IF;
    IF target_type = 'select' AND NOT (target_options ? NEW.value_text) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'custom select value is not an approved option';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_validate_code_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    old_company bigint;
    new_company bigint;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    IF NEW.old_issued_code_id IS NOT NULL THEN
        SELECT company_id INTO old_company FROM masterdata_issuedcode WHERE id = NEW.old_issued_code_id;
    END IF;
    IF NEW.new_issued_code_id IS NOT NULL THEN
        SELECT company_id INTO new_company FROM masterdata_issuedcode WHERE id = NEW.new_issued_code_id;
    END IF;
    IF asset_company IS NULL OR asset_company <> NEW.company_id
       OR (NEW.old_issued_code_id IS NOT NULL AND old_company <> NEW.company_id)
       OR (NEW.new_issued_code_id IS NOT NULL AND new_company <> NEW.company_id)
       OR (NEW.event_type IN ('corrected', 'voided') AND btrim(NEW.reason) = '') THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset code history scope or reason is invalid';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_reject_code_history_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset code history is append-only';
END;
$$;

CREATE OR REPLACE FUNCTION assets_validate_attachment_link()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    attachment_company bigint;
    attachment_mime varchar;
    controlled_mutation boolean;
    actor_cleared boolean;
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status <> 'active' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment links must be created active';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF ROW(
            NEW.company_id, NEW.asset_id, NEW.attachment_id,
            NEW.role, NEW.security_class
        ) IS DISTINCT FROM ROW(
            OLD.company_id, OLD.asset_id, OLD.attachment_id,
            OLD.role, OLD.security_class
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment link identity, role and security class are immutable; void and recreate';
        END IF;
        controlled_mutation := COALESCE(
            current_setting('eam_lite.controlled_asset_mutation', true), ''
        ) = 'on';
        actor_cleared := OLD.voided_by_id IS NOT NULL
            AND NEW.voided_by_id IS NULL
            AND ROW(NEW.status, NEW.void_reason, NEW.voided_at)
                IS NOT DISTINCT FROM
                ROW(OLD.status, OLD.void_reason, OLD.voided_at);
        IF ROW(NEW.status, NEW.void_reason, NEW.voided_by_id, NEW.voided_at)
                IS DISTINCT FROM
                ROW(OLD.status, OLD.void_reason, OLD.voided_by_id, OLD.voided_at)
           AND NOT controlled_mutation AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment link protected state must be changed by the controlled service';
        END IF;
        IF controlled_mutation
           AND ROW(NEW.status, NEW.void_reason, NEW.voided_by_id, NEW.voided_at)
               IS DISTINCT FROM
               ROW(OLD.status, OLD.void_reason, OLD.voided_by_id, OLD.voided_at) THEN
            PERFORM set_config('eam_lite.controlled_asset_mutation', 'off', true);
        END IF;
    END IF;
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, mime_type INTO attachment_company, attachment_mime
      FROM masterdata_attachment WHERE id = NEW.attachment_id;
    IF asset_company IS NULL OR attachment_company IS NULL
       OR asset_company <> NEW.company_id OR attachment_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment link objects must be in the same company';
    END IF;
    IF NEW.role IN ('cover', 'photo') AND attachment_mime NOT LIKE 'image/%%' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'asset cover and photo roles require an image attachment';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION assets_guard_pending_reference_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'masterdata_assetcategory' THEN
        IF ROW(NEW.company_id, NEW.is_active)
            IS NOT DISTINCT FROM ROW(OLD.company_id, OLD.is_active) THEN
            RETURN NEW;
        END IF;
        IF EXISTS (
            SELECT 1 FROM assets_asset
             WHERE asset_status = 'pending_finance'
               AND category_id = OLD.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset prevents category scope or active-state changes';
        END IF;
        RETURN NEW;
    ELSIF TG_TABLE_NAME = 'masterdata_department' THEN
        IF ROW(NEW.company_id, NEW.is_active)
            IS NOT DISTINCT FROM ROW(OLD.company_id, OLD.is_active) THEN
            RETURN NEW;
        END IF;
        IF EXISTS (
            SELECT 1 FROM assets_asset
             WHERE asset_status = 'pending_finance'
               AND department_id = OLD.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset prevents department scope or active-state changes';
        END IF;
        RETURN NEW;
    ELSIF TG_TABLE_NAME = 'masterdata_employee' THEN
        IF ROW(NEW.company_id, NEW.department_id, NEW.employment_status, NEW.is_active)
            IS NOT DISTINCT FROM
            ROW(OLD.company_id, OLD.department_id, OLD.employment_status, OLD.is_active) THEN
            RETURN NEW;
        END IF;
        IF EXISTS (
            SELECT 1 FROM assets_asset
             WHERE asset_status = 'pending_finance'
               AND responsible_employee_id = OLD.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset prevents responsible employee scope or active-state changes';
        END IF;
        RETURN NEW;
    ELSIF TG_TABLE_NAME = 'masterdata_location' THEN
        IF TG_OP = 'INSERT' THEN
            IF NEW.parent_id IS NULL THEN
                RETURN NEW;
            END IF;
            PERFORM id FROM masterdata_location
             WHERE id = NEW.parent_id
             FOR UPDATE;
            IF EXISTS (
                SELECT 1 FROM assets_asset
                 WHERE asset_status = 'pending_finance'
                   AND location_id = NEW.parent_id
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset location must remain a leaf';
            END IF;
            RETURN NEW;
        END IF;
        IF ROW(NEW.company_id, NEW.parent_id, NEW.is_active)
            IS NOT DISTINCT FROM ROW(OLD.company_id, OLD.parent_id, OLD.is_active) THEN
            RETURN NEW;
        END IF;
        IF EXISTS (
            SELECT 1 FROM assets_asset
             WHERE asset_status = 'pending_finance'
               AND location_id = OLD.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset prevents location hierarchy, scope or active-state changes';
        END IF;
        RETURN NEW;
    ELSIF TG_TABLE_NAME = 'assets_assetcustomfield' THEN
        IF ROW(
               NEW.company_id, NEW.category_id, NEW.name, NEW.code,
               NEW.normalized_code, NEW.field_type, NEW.required,
               NEW.options_json, NEW.is_active
           ) IS NOT DISTINCT FROM ROW(
               OLD.company_id, OLD.category_id, OLD.name, OLD.code,
               OLD.normalized_code, OLD.field_type, OLD.required,
               OLD.options_json, OLD.is_active
           ) THEN
            RETURN NEW;
        END IF;
        IF EXISTS (
            SELECT 1 FROM assets_asset
             WHERE asset_status = 'pending_finance'
               AND category_id = OLD.category_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset prevents custom field definition changes';
        END IF;
        RETURN NEW;
    ELSIF TG_TABLE_NAME = 'masterdata_attachment' THEN
        IF ROW(NEW.company_id, NEW.mime_type, NEW.malware_scan_status, NEW.is_available)
            IS NOT DISTINCT FROM
            ROW(OLD.company_id, OLD.mime_type, OLD.malware_scan_status, OLD.is_available) THEN
            RETURN NEW;
        END IF;
        IF EXISTS (
            SELECT 1
              FROM assets_asset asset
              JOIN assets_attachmentlink link ON link.asset_id = asset.id
             WHERE asset.asset_status = 'pending_finance'
               AND link.attachment_id = OLD.id
               AND link.status = 'active'
               AND link.role IN ('cover', 'photo')
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'pending finance asset prevents linked photo availability or security changes';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'unsupported pending reference guard table';
END;
$$;

CREATE OR REPLACE FUNCTION assets_guard_referenced_company_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.company_id IS NOT DISTINCT FROM OLD.company_id THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'masterdata_department' AND EXISTS (
        SELECT 1 FROM assets_asset WHERE department_id = OLD.id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'department company change would break asset references';
    ELSIF TG_TABLE_NAME = 'masterdata_employee' AND EXISTS (
        SELECT 1 FROM assets_asset WHERE responsible_employee_id = OLD.id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'employee company change would break asset references';
    ELSIF TG_TABLE_NAME = 'masterdata_location' AND EXISTS (
        SELECT 1 FROM assets_asset WHERE location_id = OLD.id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'location company change would break asset references';
    ELSIF TG_TABLE_NAME = 'masterdata_assetcategory' AND (
        EXISTS (SELECT 1 FROM assets_asset WHERE category_id = OLD.id)
        OR EXISTS (SELECT 1 FROM assets_assetcustomfield WHERE category_id = OLD.id)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'category company change would break asset references';
    ELSIF TG_TABLE_NAME = 'masterdata_attachment' AND EXISTS (
        SELECT 1 FROM assets_attachmentlink WHERE attachment_id = OLD.id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'attachment company change would break asset references';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_asset_references ON assets_asset;
CREATE TRIGGER trg_asset_references
BEFORE INSERT OR UPDATE ON assets_asset
FOR EACH ROW EXECUTE FUNCTION assets_validate_asset_references();

DROP TRIGGER IF EXISTS trg_asset_commit ON assets_asset;
CREATE CONSTRAINT TRIGGER trg_asset_commit
AFTER INSERT OR UPDATE ON assets_asset
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_asset_commit();

DROP TRIGGER IF EXISTS trg_asset_delete ON assets_asset;
CREATE TRIGGER trg_asset_delete
BEFORE DELETE ON assets_asset
FOR EACH ROW EXECUTE FUNCTION assets_guard_asset_delete();

DROP TRIGGER IF EXISTS trg_custom_field_validate ON assets_assetcustomfield;
CREATE TRIGGER trg_custom_field_validate
BEFORE INSERT OR UPDATE ON assets_assetcustomfield
FOR EACH ROW EXECUTE FUNCTION assets_validate_custom_field();

DROP TRIGGER IF EXISTS trg_custom_value_validate ON assets_assetcustomvalue;
CREATE TRIGGER trg_custom_value_validate
BEFORE INSERT OR UPDATE ON assets_assetcustomvalue
FOR EACH ROW EXECUTE FUNCTION assets_validate_custom_value();

DROP TRIGGER IF EXISTS trg_custom_value_asset_commit ON assets_assetcustomvalue;
CREATE CONSTRAINT TRIGGER trg_custom_value_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON assets_assetcustomvalue
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_asset_commit();

DROP TRIGGER IF EXISTS trg_code_history_validate ON assets_assetcodehistory;
CREATE TRIGGER trg_code_history_validate
BEFORE INSERT ON assets_assetcodehistory
FOR EACH ROW EXECUTE FUNCTION assets_validate_code_history();

DROP TRIGGER IF EXISTS trg_code_history_immutable ON assets_assetcodehistory;
CREATE TRIGGER trg_code_history_immutable
BEFORE UPDATE OR DELETE ON assets_assetcodehistory
FOR EACH ROW EXECUTE FUNCTION assets_reject_code_history_change();

DROP TRIGGER IF EXISTS trg_attachment_link_validate ON assets_attachmentlink;
CREATE TRIGGER trg_attachment_link_validate
BEFORE INSERT OR UPDATE ON assets_attachmentlink
FOR EACH ROW EXECUTE FUNCTION assets_validate_attachment_link();

DROP TRIGGER IF EXISTS trg_attachment_link_delete ON assets_attachmentlink;
CREATE TRIGGER trg_attachment_link_delete
BEFORE DELETE ON assets_attachmentlink
FOR EACH ROW EXECUTE FUNCTION assets_reject_attachment_link_delete();

DROP TRIGGER IF EXISTS trg_attachment_link_asset_commit ON assets_attachmentlink;
CREATE CONSTRAINT TRIGGER trg_attachment_link_asset_commit
AFTER INSERT OR UPDATE OR DELETE ON assets_attachmentlink
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION assets_validate_asset_commit();

DROP TRIGGER IF EXISTS trg_assets_department_company ON masterdata_department;
CREATE TRIGGER trg_assets_department_company
BEFORE UPDATE OF company_id ON masterdata_department
FOR EACH ROW EXECUTE FUNCTION assets_guard_referenced_company_change();

DROP TRIGGER IF EXISTS trg_assets_employee_company ON masterdata_employee;
CREATE TRIGGER trg_assets_employee_company
BEFORE UPDATE OF company_id ON masterdata_employee
FOR EACH ROW EXECUTE FUNCTION assets_guard_referenced_company_change();

DROP TRIGGER IF EXISTS trg_assets_location_company ON masterdata_location;
CREATE TRIGGER trg_assets_location_company
BEFORE UPDATE OF company_id ON masterdata_location
FOR EACH ROW EXECUTE FUNCTION assets_guard_referenced_company_change();

DROP TRIGGER IF EXISTS trg_assets_category_company ON masterdata_assetcategory;
CREATE TRIGGER trg_assets_category_company
BEFORE UPDATE OF company_id ON masterdata_assetcategory
FOR EACH ROW EXECUTE FUNCTION assets_guard_referenced_company_change();

DROP TRIGGER IF EXISTS trg_assets_attachment_company ON masterdata_attachment;
CREATE TRIGGER trg_assets_attachment_company
BEFORE UPDATE OF company_id ON masterdata_attachment
FOR EACH ROW EXECUTE FUNCTION assets_guard_referenced_company_change();

DROP TRIGGER IF EXISTS trg_assets_pending_category ON masterdata_assetcategory;
CREATE TRIGGER trg_assets_pending_category
BEFORE UPDATE OF company_id, is_active ON masterdata_assetcategory
FOR EACH ROW EXECUTE FUNCTION assets_guard_pending_reference_change();

DROP TRIGGER IF EXISTS trg_assets_pending_department ON masterdata_department;
CREATE TRIGGER trg_assets_pending_department
BEFORE UPDATE OF company_id, is_active ON masterdata_department
FOR EACH ROW EXECUTE FUNCTION assets_guard_pending_reference_change();

DROP TRIGGER IF EXISTS trg_assets_pending_employee ON masterdata_employee;
CREATE TRIGGER trg_assets_pending_employee
BEFORE UPDATE OF company_id, department_id, employment_status, is_active ON masterdata_employee
FOR EACH ROW EXECUTE FUNCTION assets_guard_pending_reference_change();

DROP TRIGGER IF EXISTS trg_assets_pending_location ON masterdata_location;
CREATE TRIGGER trg_assets_pending_location
BEFORE INSERT OR UPDATE OF company_id, parent_id, is_active ON masterdata_location
FOR EACH ROW EXECUTE FUNCTION assets_guard_pending_reference_change();

DROP TRIGGER IF EXISTS trg_assets_pending_custom_field ON assets_assetcustomfield;
CREATE TRIGGER trg_assets_pending_custom_field
BEFORE UPDATE ON assets_assetcustomfield
FOR EACH ROW EXECUTE FUNCTION assets_guard_pending_reference_change();

DROP TRIGGER IF EXISTS trg_assets_pending_attachment ON masterdata_attachment;
CREATE TRIGGER trg_assets_pending_attachment
BEFORE UPDATE OF company_id, mime_type, malware_scan_status, is_available ON masterdata_attachment
FOR EACH ROW EXECUTE FUNCTION assets_guard_pending_reference_change();

DROP TRIGGER IF EXISTS trg_assets_attachment_availability ON masterdata_attachment;
CREATE TRIGGER trg_assets_attachment_availability
BEFORE INSERT OR UPDATE OF is_available, malware_scan_status ON masterdata_attachment
FOR EACH ROW EXECUTE FUNCTION assets_validate_attachment_availability();
"""


DROP_SQL = r"""
DROP TRIGGER IF EXISTS trg_assets_attachment_availability ON masterdata_attachment;
DROP TRIGGER IF EXISTS trg_assets_pending_attachment ON masterdata_attachment;
DROP TRIGGER IF EXISTS trg_assets_pending_custom_field ON assets_assetcustomfield;
DROP TRIGGER IF EXISTS trg_assets_pending_location ON masterdata_location;
DROP TRIGGER IF EXISTS trg_assets_pending_employee ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_assets_pending_department ON masterdata_department;
DROP TRIGGER IF EXISTS trg_assets_pending_category ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_assets_attachment_company ON masterdata_attachment;
DROP TRIGGER IF EXISTS trg_assets_category_company ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_assets_location_company ON masterdata_location;
DROP TRIGGER IF EXISTS trg_assets_employee_company ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_assets_department_company ON masterdata_department;
DROP TRIGGER IF EXISTS trg_attachment_link_delete ON assets_attachmentlink;
DROP TRIGGER IF EXISTS trg_attachment_link_asset_commit ON assets_attachmentlink;
DROP TRIGGER IF EXISTS trg_attachment_link_validate ON assets_attachmentlink;
DROP TRIGGER IF EXISTS trg_code_history_immutable ON assets_assetcodehistory;
DROP TRIGGER IF EXISTS trg_code_history_validate ON assets_assetcodehistory;
DROP TRIGGER IF EXISTS trg_custom_value_asset_commit ON assets_assetcustomvalue;
DROP TRIGGER IF EXISTS trg_custom_value_validate ON assets_assetcustomvalue;
DROP TRIGGER IF EXISTS trg_custom_field_validate ON assets_assetcustomfield;
DROP TRIGGER IF EXISTS trg_asset_delete ON assets_asset;
DROP TRIGGER IF EXISTS trg_asset_commit ON assets_asset;
DROP TRIGGER IF EXISTS trg_asset_references ON assets_asset;
DROP FUNCTION IF EXISTS assets_guard_pending_reference_change();
DROP FUNCTION IF EXISTS assets_validate_attachment_availability();
DROP FUNCTION IF EXISTS assets_guard_referenced_company_change();
DROP FUNCTION IF EXISTS assets_reject_attachment_link_delete();
DROP FUNCTION IF EXISTS assets_validate_attachment_link();
DROP FUNCTION IF EXISTS assets_reject_code_history_change();
DROP FUNCTION IF EXISTS assets_validate_code_history();
DROP FUNCTION IF EXISTS assets_validate_custom_value();
DROP FUNCTION IF EXISTS assets_validate_custom_field();
DROP FUNCTION IF EXISTS assets_guard_asset_delete();
DROP FUNCTION IF EXISTS assets_validate_asset_commit();
DROP FUNCTION IF EXISTS assets_validate_asset_references();
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("assets", "0001_initial")]

    operations = [
        migrations.RunPython(
            install_postgresql_guards,
            reverse_code=remove_postgresql_guards,
        )
    ]
