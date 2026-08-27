import importlib

from django.db import migrations


CREATE_GUARDS = r"""
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
    controlled := COALESCE(current_setting('eam_lite.controlled_supply_document_transition', true), '') = 'on';
    IF OLD.status = 'draft' THEN
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            IF NOT controlled THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply document transition requires controlled service';
            END IF;
            PERFORM set_config('eam_lite.controlled_supply_document_transition', 'off', true);
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'posted' AND NEW.status = 'reversed' THEN
        IF NOT controlled
           OR NEW.reversed_at IS NULL
           OR NEW.reversed_by_id IS NULL
           OR (to_jsonb(NEW) - ARRAY['status','reversed_by_id','reversed_at','updated_at'])
                IS DISTINCT FROM
              (to_jsonb(OLD) - ARRAY['status','reversed_by_id','reversed_at','updated_at']) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply reversal transition requires controlled service';
        END IF;
        PERFORM set_config('eam_lite.controlled_supply_document_transition', 'off', true);
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['created_by_id','posted_by_id','cancelled_by_id','reversed_by_id'])
          IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['created_by_id','posted_by_id','cancelled_by_id','reversed_by_id'])
       OR (NEW.created_by_id IS DISTINCT FROM OLD.created_by_id AND NEW.created_by_id IS NOT NULL)
       OR (NEW.posted_by_id IS DISTINCT FROM OLD.posted_by_id AND NEW.posted_by_id IS NOT NULL)
       OR (NEW.cancelled_by_id IS DISTINCT FROM OLD.cancelled_by_id AND NEW.cancelled_by_id IS NOT NULL)
       OR (NEW.reversed_by_id IS DISTINCT FROM OLD.reversed_by_id AND NEW.reversed_by_id IS NOT NULL) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='posted, reversed or cancelled supply document is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_line_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ref_company bigint;
    ref_document uuid;
    ref_item uuid;
    ref_document_type varchar;
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
        SELECT line.company_id, line.document_id, line.item_id, document.document_type
          INTO ref_company, ref_document, ref_item, ref_document_type
          FROM supplies_supplydocumentline line
          JOIN supplies_supplydocument document ON document.id=line.document_id
         WHERE line.id=NEW.source_issue_line_id;
        IF ref_company IS NULL OR ref_company <> NEW.company_id
           OR ref_document=NEW.document_id OR ref_item<>NEW.item_id
           OR ref_document_type<>'issue' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='source issue line is invalid';
        END IF;
    END IF;
    IF NEW.source_custody_id IS NOT NULL THEN
        SELECT company_id, item_id INTO ref_company, ref_item
          FROM supplies_supplycustody WHERE id=NEW.source_custody_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='source custody is invalid';
        END IF;
    END IF;
    RETURN NEW;
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
    current_source_custody uuid;
    document_status varchar;
    document_type varchar;
BEGIN
    SELECT line.document_id, line.entered_unit_cost, line.posted_unit_cost,
           line.posted_amount, line.adjustment_direction,
           line.source_issue_line_id, line.source_custody_id
      INTO current_document_id, current_entered_cost, current_posted_cost,
           current_posted_amount, current_direction,
           current_source_issue, current_source_custody
      FROM supplies_supplydocumentline line WHERE line.id=NEW.id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT document.status, document.document_type INTO document_status, document_type
      FROM supplies_supplydocument document WHERE document.id=current_document_id;
    IF document_type IN ('opening','receipt') THEN
        IF current_entered_cost IS NULL OR current_direction IS NOT NULL
           OR current_source_issue IS NOT NULL OR current_source_custody IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='receipt line fields are invalid';
        END IF;
    ELSIF document_type IN ('issue','transfer') THEN
        IF current_entered_cost IS NOT NULL OR current_direction IS NOT NULL
           OR current_source_issue IS NOT NULL OR current_source_custody IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='issue or transfer cost must be system calculated';
        END IF;
    ELSIF document_type='return' THEN
        IF current_entered_cost IS NOT NULL OR current_direction IS NOT NULL
           OR current_source_issue IS NULL OR current_source_custody IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Sprint 15 consumable return line fields are invalid';
        END IF;
    ELSIF document_type='reversal' THEN
        IF current_entered_cost IS NOT NULL OR current_direction IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='reversal line fields are system generated';
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

CREATE OR REPLACE FUNCTION supplies_validate_ledger_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ref_company bigint;
    line_document uuid;
    line_item uuid;
    document_status varchar;
    document_type varchar;
    source_warehouse uuid;
    target_warehouse uuid;
    original_company bigint;
    original_warehouse uuid;
    original_item uuid;
    original_type varchar;
    original_quantity_delta numeric;
    original_amount_delta numeric;
    original_quantity_before numeric;
    original_quantity_after numeric;
    original_amount_before numeric;
    original_amount_after numeric;
    original_average_before numeric;
    original_average_after numeric;
BEGIN
    SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.warehouse_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger warehouse belongs to another company';
    END IF;
    SELECT company_id INTO ref_company FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger item belongs to another company';
    END IF;
    SELECT document.company_id, document.status, document.document_type,
           document.source_warehouse_id, document.target_warehouse_id
      INTO ref_company, document_status, document_type, source_warehouse, target_warehouse
      FROM supplies_supplydocument document WHERE document.id=NEW.document_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id OR document_status NOT IN ('posted','reversed') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger document is not a posted same-company document';
    END IF;
    SELECT company_id, document_id, item_id INTO ref_company, line_document, line_item
      FROM supplies_supplydocumentline WHERE id=NEW.document_line_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id
       OR line_document<>NEW.document_id OR line_item<>NEW.item_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger line is inconsistent';
    END IF;
    IF document_type='opening' THEN
        IF NEW.movement_type<>'opening_in' OR NEW.quantity_delta<=0 OR NEW.amount_delta<0 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='opening ledger is invalid';
        END IF;
    ELSIF document_type='receipt' THEN
        IF NEW.movement_type<>'receipt_in' OR NEW.quantity_delta<=0 OR NEW.amount_delta<0 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='receipt ledger is invalid';
        END IF;
    ELSIF document_type='issue' THEN
        IF NEW.movement_type<>'issue_out' OR NEW.quantity_delta>=0 OR NEW.amount_delta>0 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='issue ledger is invalid';
        END IF;
    ELSIF document_type='return' THEN
        IF NEW.movement_type<>'return_in' OR NEW.quantity_delta<=0 OR NEW.amount_delta<0 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='return ledger is invalid';
        END IF;
    ELSIF document_type='transfer' THEN
        IF NEW.warehouse_id=source_warehouse THEN
            IF NEW.movement_type<>'transfer_out' OR NEW.quantity_delta>=0 OR NEW.amount_delta>0 THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='transfer out ledger is invalid';
            END IF;
        ELSIF NEW.warehouse_id=target_warehouse THEN
            IF NEW.movement_type<>'transfer_in' OR NEW.quantity_delta<=0 OR NEW.amount_delta<0 THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='transfer in ledger is invalid';
            END IF;
        ELSE
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='transfer ledger warehouse is invalid';
        END IF;
    ELSIF document_type='reversal' THEN
        IF NEW.movement_type<>'reversal' OR NEW.reverses_ledger_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='reversal ledger requires original ledger';
        END IF;
        SELECT company_id, warehouse_id, item_id, movement_type,
               quantity_delta, amount_delta, quantity_before, quantity_after,
               amount_before, amount_after,
               average_unit_cost_before, average_unit_cost_after
          INTO original_company, original_warehouse, original_item, original_type,
               original_quantity_delta, original_amount_delta,
               original_quantity_before, original_quantity_after,
               original_amount_before, original_amount_after,
               original_average_before, original_average_after
          FROM supplies_supplystockledger WHERE id=NEW.reverses_ledger_id;
        IF original_company IS NULL OR original_company<>NEW.company_id
           OR original_warehouse<>NEW.warehouse_id OR original_item<>NEW.item_id
           OR original_type='reversal'
           OR NEW.quantity_delta<>-original_quantity_delta
           OR NEW.amount_delta<>-original_amount_delta
           OR NEW.quantity_before<>original_quantity_after
           OR NEW.quantity_after<>original_quantity_before
           OR NEW.amount_before<>original_amount_after
           OR NEW.amount_after<>original_amount_before
           OR NEW.average_unit_cost_before<>original_average_after
           OR NEW.average_unit_cost_after<>original_average_before THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='reversal ledger does not restore the original snapshot';
        END IF;
    ELSIF document_type<>'count_adjustment' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='stock ledger document type is invalid';
    END IF;
    IF document_type<>'reversal' AND NEW.reverses_ledger_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ordinary ledger cannot reverse another ledger';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_supply_line_refs_s14 ON supplies_supplydocumentline;
CREATE TRIGGER trg_supply_line_refs_s14
BEFORE INSERT OR UPDATE OF company_id,document_id,item_id,source_issue_line_id,source_custody_id
ON supplies_supplydocumentline FOR EACH ROW EXECUTE FUNCTION supplies_validate_line_refs_s14();

CREATE OR REPLACE FUNCTION supplies_validate_custody_s15()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ref_company bigint;
    ref_item uuid;
    ref_type varchar;
    ref_document_type varchar;
    ref_document_status varchar;
    employee_department bigint;
    employee_status varchar;
    employee_active boolean;
    department_active boolean;
BEGIN
    SELECT company_id, item_type INTO ref_company, ref_type
      FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_type<>'durable_quantity' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody item must be a same-company durable quantity item';
    END IF;
    SELECT line.company_id, line.item_id, document.document_type, document.status
      INTO ref_company, ref_item, ref_document_type, ref_document_status
      FROM supplies_supplydocumentline line
      JOIN supplies_supplydocument document ON document.id=line.document_id
     WHERE line.id=NEW.origin_issue_line_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id
       OR ref_document_type<>'issue' OR ref_document_status NOT IN ('posted','reversed') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody origin issue line is invalid';
    END IF;
    SELECT company_id, is_active INTO ref_company, department_active
      FROM masterdata_department WHERE id=NEW.department_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody department belongs to another company';
    END IF;
    IF NEW.employee_id IS NOT NULL THEN
        SELECT company_id, department_id, employment_status, is_active
          INTO ref_company, employee_department, employee_status, employee_active
          FROM masterdata_employee WHERE id=NEW.employee_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR employee_department<>NEW.department_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody employee is outside the department or company';
        END IF;
        IF TG_OP='INSERT' AND (employee_status<>'active' OR NOT employee_active OR NOT department_active) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='new custody employee must be active';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_custody_mutation_s15()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply custody cannot be deleted';
    END IF;
    IF COALESCE(current_setting('eam_lite.controlled_supply_custody_mutation', true), '')<>'on' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply custody requires controlled service';
    END IF;
    PERFORM set_config('eam_lite.controlled_supply_custody_mutation', 'off', true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_custody_movement_s15()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ref_company bigint;
    ref_item uuid;
    ref_from uuid;
    ref_to uuid;
    original_action varchar;
    original_quantity numeric;
    original_amount numeric;
    original_cost numeric;
BEGIN
    SELECT company_id, item_type INTO ref_company, original_action
      FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id OR original_action<>'durable_quantity' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody movement item is invalid';
    END IF;
    IF NEW.from_custody_id IS NOT NULL THEN
        SELECT company_id, item_id INTO ref_company, ref_item
          FROM supplies_supplycustody WHERE id=NEW.from_custody_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='from custody is invalid';
        END IF;
    END IF;
    IF NEW.to_custody_id IS NOT NULL THEN
        SELECT company_id, item_id INTO ref_company, ref_item
          FROM supplies_supplycustody WHERE id=NEW.to_custody_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='to custody is invalid';
        END IF;
    END IF;
    IF NEW.source_document_line_id IS NOT NULL THEN
        SELECT company_id, item_id INTO ref_company, ref_item
          FROM supplies_supplydocumentline WHERE id=NEW.source_document_line_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody movement source line is invalid';
        END IF;
    END IF;
    IF NEW.action='reversal' THEN
        SELECT company_id, item_id, action, from_custody_id, to_custody_id,
               quantity, amount, unit_cost
          INTO ref_company, ref_item, original_action, ref_from, ref_to,
               original_quantity, original_amount, original_cost
          FROM supplies_supplycustodymovement WHERE id=NEW.reverses_movement_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id
           OR original_action<>'issue' OR ref_from IS NOT NULL
           OR ref_to<>NEW.from_custody_id
           OR NEW.to_custody_id IS NOT NULL
           OR NEW.quantity<>original_quantity OR NEW.amount<>original_amount
           OR NEW.unit_cost<>original_cost THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody reversal movement is invalid';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_custody_movement_s15()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF COALESCE(current_setting('eam_lite.controlled_supply_custody_movement_insert', true), '')<>'on' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody movement insert requires controlled service';
        END IF;
        PERFORM set_config('eam_lite.controlled_supply_custody_movement_insert', 'off', true);
        RETURN NEW;
    ELSIF TG_OP='DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody movement is append-only';
    END IF;
    IF (to_jsonb(NEW)-'created_by_id') IS DISTINCT FROM (to_jsonb(OLD)-'created_by_id')
       OR (NEW.created_by_id IS DISTINCT FROM OLD.created_by_id AND NEW.created_by_id IS NOT NULL) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody movement is append-only';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_supply_custody_mutation_s15
BEFORE INSERT OR UPDATE OR DELETE ON supplies_supplycustody
FOR EACH ROW EXECUTE FUNCTION supplies_guard_custody_mutation_s15();
CREATE CONSTRAINT TRIGGER trg_supply_custody_refs_s15
AFTER INSERT OR UPDATE ON supplies_supplycustody
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION supplies_validate_custody_s15();

CREATE TRIGGER trg_supply_custody_move_mutation_s15
BEFORE INSERT OR UPDATE OR DELETE ON supplies_supplycustodymovement
FOR EACH ROW EXECUTE FUNCTION supplies_guard_custody_movement_s15();
CREATE CONSTRAINT TRIGGER trg_supply_custody_move_refs_s15
AFTER INSERT OR UPDATE ON supplies_supplycustodymovement
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION supplies_validate_custody_movement_s15();
"""


DROP_SPRINT15_GUARDS = r"""
DROP TRIGGER IF EXISTS trg_supply_custody_move_refs_s15 ON supplies_supplycustodymovement;
DROP TRIGGER IF EXISTS trg_supply_custody_move_mutation_s15 ON supplies_supplycustodymovement;
DROP TRIGGER IF EXISTS trg_supply_custody_refs_s15 ON supplies_supplycustody;
DROP TRIGGER IF EXISTS trg_supply_custody_mutation_s15 ON supplies_supplycustody;
DROP FUNCTION IF EXISTS supplies_guard_custody_movement_s15();
DROP FUNCTION IF EXISTS supplies_validate_custody_movement_s15();
DROP FUNCTION IF EXISTS supplies_guard_custody_mutation_s15();
DROP FUNCTION IF EXISTS supplies_validate_custody_s15();
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(CREATE_GUARDS)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    sprint14 = importlib.import_module(
        "apps.supplies.migrations.0004_sprint14_postgresql_guards"
    )
    connection = schema_editor.connection.connection
    connection.execute(DROP_SPRINT15_GUARDS)
    connection.execute(sprint14.DROP_GUARDS)
    connection.execute(sprint14.CREATE_GUARDS)


class Migration(migrations.Migration):
    dependencies = [("supplies", "0005_sprint15_movements_custody")]
    operations = [migrations.RunPython(install, uninstall)]
