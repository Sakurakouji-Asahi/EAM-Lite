from django.db import migrations


CREATE_SQL = r"""
CREATE OR REPLACE FUNCTION masterdata_validate_coding_scheme()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    previous_company_id bigint;
    previous_scheme_key varchar;
    previous_version integer;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.company_id::text || ':' || NEW.scheme_key, 0)
    );

    IF NEW.previous_version_id IS NOT NULL THEN
        SELECT company_id, scheme_key, version
          INTO previous_company_id, previous_scheme_key, previous_version
          FROM masterdata_assetcodingscheme
         WHERE id = NEW.previous_version_id;
        IF previous_company_id IS NULL
           OR previous_company_id <> NEW.company_id
           OR previous_scheme_key <> NEW.scheme_key
           OR previous_version >= NEW.version THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid coding scheme previous version';
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.status = 'retired' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'retired coding scheme is immutable';
        END IF;
        IF OLD.status = 'active' AND NEW.status = 'draft' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'active coding scheme cannot return to draft';
        END IF;
        IF OLD.status <> 'draft'
           AND (NEW.company_id, NEW.scheme_key, NEW.version, NEW.name,
                NEW.description, NEW.reset_mode, NEW.sequence_start,
                NEW.category_scope_level, NEW.effective_from,
                NEW.previous_version_id)
               IS DISTINCT FROM
               (OLD.company_id, OLD.scheme_key, OLD.version, OLD.name,
                OLD.description, OLD.reset_mode, OLD.sequence_start,
                OLD.category_scope_level, OLD.effective_from,
                OLD.previous_version_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'active coding scheme rules are immutable';
        END IF;
        IF EXISTS (
            SELECT 1 FROM masterdata_assetcategory
             WHERE default_coding_scheme_id = NEW.id
        ) AND (
            NEW.status <> 'active'
            OR NEW.effective_from > (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date
            OR (NEW.effective_to IS NOT NULL AND NEW.effective_to < (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date)
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'category references must be cleared before retiring a coding scheme';
        END IF;
        IF EXISTS (
            SELECT 1 FROM masterdata_sequencecounter counter
             WHERE counter.coding_scheme_id = NEW.id
               AND counter.current_value < NEW.sequence_start - 1
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'coding scheme start exceeds an existing counter';
        END IF;
    END IF;

    IF NEW.status = 'active' AND EXISTS (
        SELECT 1
          FROM masterdata_assetcodingscheme other
         WHERE other.company_id = NEW.company_id
           AND other.scheme_key = NEW.scheme_key
           AND other.status = 'active'
           AND other.id <> COALESCE(NEW.id, -1)
           AND daterange(other.effective_from, other.effective_to, '[]')
               && daterange(NEW.effective_from, NEW.effective_to, '[]')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23P01', MESSAGE = 'active coding scheme effective ranges overlap';
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.effective_to IS DISTINCT FROM OLD.effective_to
       AND NEW.effective_to IS NOT NULL AND EXISTS (
        SELECT 1 FROM masterdata_issuedcode issued
         WHERE issued.coding_scheme_id = NEW.id
           AND issued.effective_date > NEW.effective_to
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'coding scheme end date precedes an issued code';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_guard_coding_scheme_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status <> 'draft'
       OR EXISTS (SELECT 1 FROM masterdata_issuedcode WHERE coding_scheme_id = OLD.id)
       OR EXISTS (SELECT 1 FROM masterdata_sequencecounter WHERE coding_scheme_id = OLD.id)
       OR EXISTS (SELECT 1 FROM masterdata_assetcategory WHERE default_coding_scheme_id = OLD.id) THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'coding scheme is protected';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_coding_structure()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_scheme_id bigint;
    target_status varchar;
    segment_count integer;
    sequence_count integer;
    maximum_order integer;
BEGIN
    IF TG_TABLE_NAME = 'masterdata_assetcodingscheme' THEN
        target_scheme_id := NEW.id;
        IF TG_OP = 'INSERT' THEN
            RETURN NULL;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        target_scheme_id := OLD.coding_scheme_id;
    ELSE
        target_scheme_id := NEW.coding_scheme_id;
    END IF;
    SELECT status INTO target_status
      FROM masterdata_assetcodingscheme
     WHERE id = target_scheme_id;
    IF target_status = 'active' THEN
        SELECT count(*),
               count(*) FILTER (WHERE segment_type = 'sequence'),
               COALESCE(max(sequence_order), 0)
          INTO segment_count, sequence_count, maximum_order
          FROM masterdata_assetcodingsegment
         WHERE coding_scheme_id = target_scheme_id;
        IF segment_count = 0 OR sequence_count <> 1 OR segment_count <> maximum_order THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'active coding scheme requires contiguous segments and exactly one sequence';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_guard_coding_segment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_scheme_id bigint;
    target_status varchar;
BEGIN
    target_scheme_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.coding_scheme_id ELSE NEW.coding_scheme_id END;
    SELECT status INTO target_status
      FROM masterdata_assetcodingscheme
     WHERE id = target_scheme_id;
    IF target_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'non-draft coding scheme segments are immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.coding_scheme_id <> OLD.coding_scheme_id THEN
        SELECT status INTO target_status
          FROM masterdata_assetcodingscheme
         WHERE id = OLD.coding_scheme_id;
        IF target_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'non-draft coding scheme segments are immutable';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_category_coding_scheme()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    scheme_company_id bigint;
    scheme_status varchar;
    scheme_from date;
    scheme_to date;
    shanghai_today date;
BEGIN
    IF NEW.default_coding_scheme_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT company_id, status, effective_from, effective_to
      INTO scheme_company_id, scheme_status, scheme_from, scheme_to
      FROM masterdata_assetcodingscheme
     WHERE id = NEW.default_coding_scheme_id;
    shanghai_today := (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date;
    IF scheme_company_id IS NULL OR scheme_company_id <> NEW.company_id
       OR scheme_status <> 'active' OR scheme_from > shanghai_today
       OR (scheme_to IS NOT NULL AND scheme_to < shanghai_today) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'category default coding scheme must be current and in the same company';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_validate_sequence_counter()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    scheme_company_id bigint;
    scheme_sequence_start bigint;
BEGIN
    SELECT company_id, sequence_start
      INTO scheme_company_id, scheme_sequence_start
      FROM masterdata_assetcodingscheme
     WHERE id = NEW.coding_scheme_id;
    IF scheme_company_id IS NULL OR scheme_company_id <> NEW.company_id
       OR NEW.current_value < scheme_sequence_start - 1 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid sequence counter company or value';
    END IF;
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
        IF (NEW.company_id, NEW.coding_scheme_id, NEW.scope_key,
            NEW.sequence_value, NEW.display_code, NEW.normalized_code,
            NEW.effective_date, NEW.effective_date_reason, NEW.idempotency_key,
            NEW.issued_by_id, NEW.issued_at)
           IS DISTINCT FROM
           (OLD.company_id, OLD.coding_scheme_id, OLD.scope_key,
            OLD.sequence_value, OLD.display_code, OLD.normalized_code,
            OLD.effective_date, OLD.effective_date_reason, OLD.idempotency_key,
            OLD.issued_by_id, OLD.issued_at) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'issued code identity is immutable';
        END IF;
        IF OLD.status <> 'active' OR NEW.status NOT IN ('replaced', 'voided') THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid issued code status transition';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION masterdata_reject_issued_code_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'issued code permanently occupies its number';
END;
$$;

DROP TRIGGER IF EXISTS trg_coding_scheme_validate ON masterdata_assetcodingscheme;
CREATE TRIGGER trg_coding_scheme_validate
BEFORE INSERT OR UPDATE ON masterdata_assetcodingscheme
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_coding_scheme();

DROP TRIGGER IF EXISTS trg_coding_scheme_delete ON masterdata_assetcodingscheme;
CREATE TRIGGER trg_coding_scheme_delete
BEFORE DELETE ON masterdata_assetcodingscheme
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_coding_scheme_delete();

DROP TRIGGER IF EXISTS trg_coding_scheme_structure ON masterdata_assetcodingscheme;
CREATE CONSTRAINT TRIGGER trg_coding_scheme_structure
AFTER INSERT OR UPDATE ON masterdata_assetcodingscheme
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_coding_structure();

DROP TRIGGER IF EXISTS trg_coding_segment_guard ON masterdata_assetcodingsegment;
CREATE TRIGGER trg_coding_segment_guard
BEFORE INSERT OR UPDATE OR DELETE ON masterdata_assetcodingsegment
FOR EACH ROW EXECUTE FUNCTION masterdata_guard_coding_segment();

DROP TRIGGER IF EXISTS trg_coding_segment_structure ON masterdata_assetcodingsegment;
CREATE CONSTRAINT TRIGGER trg_coding_segment_structure
AFTER INSERT OR UPDATE OR DELETE ON masterdata_assetcodingsegment
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_coding_structure();

DROP TRIGGER IF EXISTS trg_category_coding_scheme ON masterdata_assetcategory;
CREATE TRIGGER trg_category_coding_scheme
BEFORE INSERT OR UPDATE OF company_id, default_coding_scheme_id ON masterdata_assetcategory
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_category_coding_scheme();

DROP TRIGGER IF EXISTS trg_sequence_counter_validate ON masterdata_sequencecounter;
CREATE TRIGGER trg_sequence_counter_validate
BEFORE INSERT OR UPDATE OF company_id, coding_scheme_id, current_value ON masterdata_sequencecounter
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_sequence_counter();

DROP TRIGGER IF EXISTS trg_issued_code_validate ON masterdata_issuedcode;
CREATE TRIGGER trg_issued_code_validate
BEFORE INSERT OR UPDATE ON masterdata_issuedcode
FOR EACH ROW EXECUTE FUNCTION masterdata_validate_issued_code();

DROP TRIGGER IF EXISTS trg_issued_code_delete ON masterdata_issuedcode;
CREATE TRIGGER trg_issued_code_delete
BEFORE DELETE ON masterdata_issuedcode
FOR EACH ROW EXECUTE FUNCTION masterdata_reject_issued_code_delete();
"""


DROP_SQL = r"""
DROP TRIGGER IF EXISTS trg_issued_code_delete ON masterdata_issuedcode;
DROP TRIGGER IF EXISTS trg_issued_code_validate ON masterdata_issuedcode;
DROP TRIGGER IF EXISTS trg_sequence_counter_validate ON masterdata_sequencecounter;
DROP TRIGGER IF EXISTS trg_category_coding_scheme ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_coding_segment_structure ON masterdata_assetcodingsegment;
DROP TRIGGER IF EXISTS trg_coding_segment_guard ON masterdata_assetcodingsegment;
DROP TRIGGER IF EXISTS trg_coding_scheme_structure ON masterdata_assetcodingscheme;
DROP TRIGGER IF EXISTS trg_coding_scheme_delete ON masterdata_assetcodingscheme;
DROP TRIGGER IF EXISTS trg_coding_scheme_validate ON masterdata_assetcodingscheme;
DROP FUNCTION IF EXISTS masterdata_reject_issued_code_delete();
DROP FUNCTION IF EXISTS masterdata_validate_issued_code();
DROP FUNCTION IF EXISTS masterdata_validate_sequence_counter();
DROP FUNCTION IF EXISTS masterdata_validate_category_coding_scheme();
DROP FUNCTION IF EXISTS masterdata_guard_coding_segment();
DROP FUNCTION IF EXISTS masterdata_validate_coding_structure();
DROP FUNCTION IF EXISTS masterdata_guard_coding_scheme_delete();
DROP FUNCTION IF EXISTS masterdata_validate_coding_scheme();
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0004_sprint2_coding_models")]

    operations = [
        migrations.RunPython(
            install_postgresql_guards,
            reverse_code=remove_postgresql_guards,
        )
    ]
