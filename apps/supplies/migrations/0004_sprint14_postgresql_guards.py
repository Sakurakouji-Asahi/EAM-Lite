from django.db import migrations


CREATE_GUARDS = r"""
CREATE OR REPLACE FUNCTION supplies_reject_stock_company_change_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply stock company is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_sequence_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF COALESCE(current_setting('eam_lite.controlled_supply_sequence_increment', true), '') <> 'on' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply document sequence requires controlled service';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.current_value <> 0 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='new supply sequence must start at zero';
        END IF;
    ELSIF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.sequence_type IS DISTINCT FROM OLD.sequence_type
       OR NEW.year IS DISTINCT FROM OLD.year
       OR NEW.current_value <> OLD.current_value + 1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply sequence must increment exactly once';
    END IF;
    PERFORM set_config('eam_lite.controlled_supply_sequence_increment', 'off', true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_document_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ref_company bigint;
BEGIN
    IF NEW.source_warehouse_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.source_warehouse_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='source warehouse belongs to another company';
        END IF;
    END IF;
    IF NEW.target_warehouse_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.target_warehouse_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='target warehouse belongs to another company';
        END IF;
    END IF;
    IF NEW.department_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM masterdata_department WHERE id=NEW.department_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply document department belongs to another company';
        END IF;
    END IF;
    IF NEW.employee_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM masterdata_employee WHERE id=NEW.employee_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply document employee belongs to another company';
        END IF;
    END IF;
    IF NEW.reversal_of_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplydocument WHERE id=NEW.reversal_of_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='reversal document belongs to another company';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_document_mutation_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE controlled boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='non-draft supply document cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'draft' THEN
        IF (to_jsonb(NEW) - ARRAY['created_by_id','posted_by_id','cancelled_by_id','reversed_by_id'])
              IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['created_by_id','posted_by_id','cancelled_by_id','reversed_by_id'])
           OR (NEW.created_by_id IS DISTINCT FROM OLD.created_by_id AND NEW.created_by_id IS NOT NULL)
           OR (NEW.posted_by_id IS DISTINCT FROM OLD.posted_by_id AND NEW.posted_by_id IS NOT NULL)
           OR (NEW.cancelled_by_id IS DISTINCT FROM OLD.cancelled_by_id AND NEW.cancelled_by_id IS NOT NULL)
           OR (NEW.reversed_by_id IS DISTINCT FROM OLD.reversed_by_id AND NEW.reversed_by_id IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='posted or cancelled supply document is immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        controlled := COALESCE(current_setting('eam_lite.controlled_supply_document_transition', true), '') = 'on';
        IF NOT controlled THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply document transition requires controlled service';
        END IF;
        PERFORM set_config('eam_lite.controlled_supply_document_transition', 'off', true);
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_line_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ref_company bigint; ref_document uuid;
BEGIN
    SELECT company_id INTO ref_company FROM supplies_supplydocument WHERE id=NEW.document_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply line document belongs to another company';
    END IF;
    SELECT company_id INTO ref_company FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply line item belongs to another company';
    END IF;
    IF NEW.source_issue_line_id IS NOT NULL THEN
        SELECT company_id, document_id INTO ref_company, ref_document
          FROM supplies_supplydocumentline WHERE id=NEW.source_issue_line_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id OR ref_document=NEW.document_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='source issue line is invalid';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_line_mutation_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE document_status varchar;
BEGIN
    SELECT status INTO document_status
      FROM supplies_supplydocument
     WHERE id=CASE WHEN TG_OP='DELETE' THEN OLD.document_id ELSE NEW.document_id END;
    IF TG_OP IN ('UPDATE','DELETE') AND document_status <> 'draft' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='non-draft supply document lines are immutable';
    END IF;
    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_line_state_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    current_document_id uuid;
    current_entered_cost numeric;
    current_posted_cost numeric;
    current_posted_amount numeric;
    current_direction varchar;
    current_source_issue uuid;
    document_status varchar;
    document_type varchar;
BEGIN
    SELECT line.document_id,
           line.entered_unit_cost,
           line.posted_unit_cost,
           line.posted_amount,
           line.adjustment_direction,
           line.source_issue_line_id
      INTO current_document_id,
           current_entered_cost,
           current_posted_cost,
           current_posted_amount,
           current_direction,
           current_source_issue
      FROM supplies_supplydocumentline line
     WHERE line.id=NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT document.status, document.document_type INTO document_status, document_type
      FROM supplies_supplydocument document WHERE document.id=current_document_id;
    IF document_type IN ('opening','receipt') THEN
        IF current_entered_cost IS NULL OR current_direction IS NOT NULL OR current_source_issue IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='receipt line fields are invalid';
        END IF;
    END IF;
    IF document_status IN ('posted','reversed') THEN
        IF current_posted_cost IS NULL OR current_posted_amount IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='posted supply line requires cost snapshot';
        END IF;
    ELSE
        IF current_posted_cost IS NOT NULL OR current_posted_amount IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='unposted supply line cannot have posted values';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_balance_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ref_company bigint;
BEGIN
    SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.warehouse_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock balance warehouse belongs to another company';
    END IF;
    SELECT company_id INTO ref_company FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock balance item belongs to another company';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_balance_mutation_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF COALESCE(current_setting('eam_lite.controlled_supply_balance_mutation', true), '') <> 'on' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock balance requires controlled service';
    END IF;
    PERFORM set_config('eam_lite.controlled_supply_balance_mutation', 'off', true);
    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_ledger_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ref_company bigint; line_document uuid; line_item uuid; document_status varchar; document_type varchar;
BEGIN
    SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.warehouse_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger warehouse belongs to another company';
    END IF;
    SELECT company_id INTO ref_company FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger item belongs to another company';
    END IF;
    SELECT document.company_id, document.status, document.document_type
      INTO ref_company, document_status, document_type
      FROM supplies_supplydocument document WHERE document.id=NEW.document_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id OR document_status NOT IN ('posted','reversed') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger document is not a posted same-company document';
    END IF;
    SELECT company_id, document_id, item_id INTO ref_company, line_document, line_item
      FROM supplies_supplydocumentline WHERE id=NEW.document_line_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id OR line_document<>NEW.document_id OR line_item<>NEW.item_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger line is inconsistent';
    END IF;
    IF document_type='opening' AND NEW.movement_type<>'opening_in' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='opening document requires opening ledger';
    END IF;
    IF document_type='receipt' AND NEW.movement_type<>'receipt_in' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='receipt document requires receipt ledger';
    END IF;
    IF document_type IN ('opening','receipt') AND (NEW.quantity_delta<=0 OR NEW.amount_delta<0) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='receipt ledger deltas must be nonnegative inbound values';
    END IF;
    IF NEW.reverses_ledger_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplystockledger WHERE id=NEW.reverses_ledger_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='reversed ledger belongs to another company';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_ledger_mutation_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF COALESCE(current_setting('eam_lite.controlled_supply_ledger_insert', true), '') <> 'on' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger insert requires controlled service';
        END IF;
        PERFORM set_config('eam_lite.controlled_supply_ledger_insert', 'off', true);
        RETURN NEW;
    ELSIF TG_OP='DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger is append-only';
    END IF;
    IF (to_jsonb(NEW) - 'created_by_id') IS DISTINCT FROM (to_jsonb(OLD) - 'created_by_id')
       OR (NEW.created_by_id IS DISTINCT FROM OLD.created_by_id AND NEW.created_by_id IS NOT NULL) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger is append-only';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_supply_sequence_controlled_s14
BEFORE INSERT OR UPDATE ON supplies_supplydocumentsequence
FOR EACH ROW EXECUTE FUNCTION supplies_guard_sequence_s14();

CREATE TRIGGER trg_supply_document_company_s14
BEFORE UPDATE OF company_id ON supplies_supplydocument
FOR EACH ROW EXECUTE FUNCTION supplies_reject_stock_company_change_s14();
CREATE TRIGGER trg_supply_document_refs_s14
BEFORE INSERT OR UPDATE OF company_id,source_warehouse_id,target_warehouse_id,department_id,employee_id,reversal_of_id
ON supplies_supplydocument FOR EACH ROW EXECUTE FUNCTION supplies_validate_document_refs_s14();
CREATE TRIGGER trg_supply_document_mutation_s14
BEFORE UPDATE OR DELETE ON supplies_supplydocument
FOR EACH ROW EXECUTE FUNCTION supplies_guard_document_mutation_s14();

CREATE TRIGGER trg_supply_line_company_s14
BEFORE UPDATE OF company_id ON supplies_supplydocumentline
FOR EACH ROW EXECUTE FUNCTION supplies_reject_stock_company_change_s14();
CREATE TRIGGER trg_supply_line_refs_s14
BEFORE INSERT OR UPDATE OF company_id,document_id,item_id,source_issue_line_id
ON supplies_supplydocumentline FOR EACH ROW EXECUTE FUNCTION supplies_validate_line_refs_s14();
CREATE TRIGGER trg_supply_line_mutation_s14
BEFORE UPDATE OR DELETE ON supplies_supplydocumentline
FOR EACH ROW EXECUTE FUNCTION supplies_guard_line_mutation_s14();
CREATE CONSTRAINT TRIGGER trg_supply_line_state_s14
AFTER INSERT OR UPDATE ON supplies_supplydocumentline
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION supplies_validate_line_state_s14();

CREATE TRIGGER trg_supply_balance_company_s14
BEFORE UPDATE OF company_id ON supplies_supplystockbalance
FOR EACH ROW EXECUTE FUNCTION supplies_reject_stock_company_change_s14();
CREATE TRIGGER trg_supply_balance_refs_s14
BEFORE INSERT OR UPDATE OF company_id,warehouse_id,item_id
ON supplies_supplystockbalance FOR EACH ROW EXECUTE FUNCTION supplies_validate_balance_refs_s14();
CREATE TRIGGER trg_supply_balance_mutation_s14
BEFORE INSERT OR UPDATE OR DELETE ON supplies_supplystockbalance
FOR EACH ROW EXECUTE FUNCTION supplies_guard_balance_mutation_s14();

CREATE TRIGGER trg_supply_ledger_company_s14
BEFORE UPDATE OF company_id ON supplies_supplystockledger
FOR EACH ROW EXECUTE FUNCTION supplies_reject_stock_company_change_s14();
CREATE TRIGGER trg_supply_ledger_mutation_s14
BEFORE INSERT OR UPDATE OR DELETE ON supplies_supplystockledger
FOR EACH ROW EXECUTE FUNCTION supplies_guard_ledger_mutation_s14();
CREATE CONSTRAINT TRIGGER trg_supply_ledger_refs_s14
AFTER INSERT OR UPDATE ON supplies_supplystockledger
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION supplies_validate_ledger_refs_s14();
"""


DROP_GUARDS = r"""
DROP TRIGGER IF EXISTS trg_supply_ledger_refs_s14 ON supplies_supplystockledger;
DROP TRIGGER IF EXISTS trg_supply_ledger_mutation_s14 ON supplies_supplystockledger;
DROP TRIGGER IF EXISTS trg_supply_ledger_company_s14 ON supplies_supplystockledger;
DROP TRIGGER IF EXISTS trg_supply_balance_mutation_s14 ON supplies_supplystockbalance;
DROP TRIGGER IF EXISTS trg_supply_balance_refs_s14 ON supplies_supplystockbalance;
DROP TRIGGER IF EXISTS trg_supply_balance_company_s14 ON supplies_supplystockbalance;
DROP TRIGGER IF EXISTS trg_supply_line_state_s14 ON supplies_supplydocumentline;
DROP TRIGGER IF EXISTS trg_supply_line_mutation_s14 ON supplies_supplydocumentline;
DROP TRIGGER IF EXISTS trg_supply_line_refs_s14 ON supplies_supplydocumentline;
DROP TRIGGER IF EXISTS trg_supply_line_company_s14 ON supplies_supplydocumentline;
DROP TRIGGER IF EXISTS trg_supply_document_mutation_s14 ON supplies_supplydocument;
DROP TRIGGER IF EXISTS trg_supply_document_refs_s14 ON supplies_supplydocument;
DROP TRIGGER IF EXISTS trg_supply_document_company_s14 ON supplies_supplydocument;
DROP TRIGGER IF EXISTS trg_supply_sequence_controlled_s14 ON supplies_supplydocumentsequence;
DROP FUNCTION IF EXISTS supplies_guard_ledger_mutation_s14();
DROP FUNCTION IF EXISTS supplies_validate_ledger_refs_s14();
DROP FUNCTION IF EXISTS supplies_guard_balance_mutation_s14();
DROP FUNCTION IF EXISTS supplies_validate_balance_refs_s14();
DROP FUNCTION IF EXISTS supplies_validate_line_state_s14();
DROP FUNCTION IF EXISTS supplies_guard_line_mutation_s14();
DROP FUNCTION IF EXISTS supplies_validate_line_refs_s14();
DROP FUNCTION IF EXISTS supplies_guard_document_mutation_s14();
DROP FUNCTION IF EXISTS supplies_validate_document_refs_s14();
DROP FUNCTION IF EXISTS supplies_guard_sequence_s14();
DROP FUNCTION IF EXISTS supplies_reject_stock_company_change_s14();
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(CREATE_GUARDS)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(DROP_GUARDS)


class Migration(migrations.Migration):
    dependencies = [("supplies", "0003_sprint14_stock_models")]
    operations = [migrations.RunPython(install, uninstall)]
