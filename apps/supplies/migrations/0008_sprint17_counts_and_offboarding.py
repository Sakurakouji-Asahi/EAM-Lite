import importlib
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


SPRINT17_GUARDS = r"""
CREATE OR REPLACE FUNCTION supplies_validate_document_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ref_company bigint; task_row record; controlled boolean;
BEGIN
    IF NEW.source_warehouse_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.source_warehouse_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='source warehouse belongs to another company';
        END IF;
    END IF;
    IF NEW.target_warehouse_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.target_warehouse_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='target warehouse belongs to another company';
        END IF;
    END IF;
    IF NEW.department_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM masterdata_department WHERE id=NEW.department_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply document department belongs to another company';
        END IF;
    END IF;
    IF NEW.employee_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM masterdata_employee WHERE id=NEW.employee_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply document employee belongs to another company';
        END IF;
    END IF;
    IF NEW.reversal_of_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplydocument WHERE id=NEW.reversal_of_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='reversal document belongs to another company';
        END IF;
    END IF;
    IF NEW.document_type='count_adjustment' THEN
        SELECT * INTO task_row FROM supplies_supplycounttask WHERE id=NEW.source_count_task_id;
        IF task_row.id IS NULL OR task_row.company_id<>NEW.company_id
           OR task_row.count_domain<>'warehouse_stock'
           OR task_row.status<>'reconciliation' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count adjustment requires a reconciliation warehouse count task';
        END IF;
        IF TG_OP='INSERT' THEN
            controlled := COALESCE(current_setting('eam_lite.controlled_supply_count_adjustment_insert',true),'')='on';
            IF NOT controlled THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count adjustment can only be created by count close service';
            END IF;
            PERFORM set_config('eam_lite.controlled_supply_count_adjustment_insert','off',true);
        END IF;
    ELSIF NEW.source_count_task_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ordinary supply document cannot reference count task';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_count_task_s17()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE controlled boolean; actor_cleared boolean; ref_company bigint;
    employee_department bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        IF OLD.status<>'draft' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='published count task cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.warehouse_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM supplies_supplywarehouse WHERE id=NEW.warehouse_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count warehouse company mismatch';
        END IF;
    END IF;
    IF NEW.department_id IS NOT NULL THEN
        SELECT company_id INTO ref_company FROM masterdata_department WHERE id=NEW.department_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count department company mismatch';
        END IF;
    END IF;
    IF NEW.employee_id IS NOT NULL THEN
        SELECT company_id,department_id INTO ref_company,employee_department
          FROM masterdata_employee WHERE id=NEW.employee_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id
           OR employee_department<>NEW.department_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count employee scope mismatch';
        END IF;
    END IF;
    IF TG_OP='INSERT' THEN
        controlled := COALESCE(current_setting('eam_lite.controlled_supply_count_task_insert',true),'')='on';
        IF NOT controlled OR NEW.status<>'draft' OR NEW.created_by_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count task insert requires controlled service';
        END IF;
        PERFORM set_config('eam_lite.controlled_supply_count_task_insert','off',true);
        RETURN NEW;
    END IF;
    actor_cleared := (
        (OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL)
        OR (OLD.published_by_id IS NOT NULL AND NEW.published_by_id IS NULL)
        OR (OLD.stopped_by_id IS NOT NULL AND NEW.stopped_by_id IS NULL)
        OR (OLD.closed_by_id IS NOT NULL AND NEW.closed_by_id IS NULL)
        OR (OLD.cancelled_by_id IS NOT NULL AND NEW.cancelled_by_id IS NULL)
    ) AND (to_jsonb(NEW)-ARRAY['created_by_id','published_by_id','stopped_by_id','closed_by_id','cancelled_by_id'])
          =(to_jsonb(OLD)-ARRAY['created_by_id','published_by_id','stopped_by_id','closed_by_id','cancelled_by_id']);
    IF actor_cleared THEN RETURN NEW; END IF;
    IF ROW(NEW.company_id,NEW.task_no,NEW.name,NEW.count_domain,NEW.warehouse_id,
           NEW.department_id,NEW.employee_id,NEW.planned_start,NEW.planned_end,
           NEW.idempotency_key,NEW.created_by_id,NEW.created_at,NEW.remark)
       IS DISTINCT FROM
       ROW(OLD.company_id,OLD.task_no,OLD.name,OLD.count_domain,OLD.warehouse_id,
           OLD.department_id,OLD.employee_id,OLD.planned_start,OLD.planned_end,
           OLD.idempotency_key,OLD.created_by_id,OLD.created_at,OLD.remark) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count task identity and scope are immutable';
    END IF;
    controlled := COALESCE(current_setting('eam_lite.controlled_supply_count_task_mutation',true),'')='on';
    IF NOT controlled THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count task transition requires controlled service';
    END IF;
    IF NOT (
        (OLD.status='draft' AND NEW.status IN ('in_progress','cancelled'))
        OR (OLD.status='in_progress' AND NEW.status IN ('reconciliation','cancelled'))
        OR (OLD.status='reconciliation' AND NEW.status IN ('closed','cancelled'))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid supply count task transition';
    END IF;
    PERFORM set_config('eam_lite.controlled_supply_count_task_mutation','off',true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_custody_s15()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ref_company bigint; ref_item uuid; ref_type varchar;
    ref_document_type varchar; ref_document_status varchar;
    ref_import_type varchar; ref_import_status varchar; ref_row_status varchar;
    employee_department bigint; employee_status varchar; employee_active boolean;
    department_active boolean; captured_by_clearance boolean;
BEGIN
    SELECT company_id,item_type INTO ref_company,ref_type
      FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_type<>'durable_quantity' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody item must be a same-company durable quantity item';
    END IF;
    IF NEW.parent_custody_id IS NULL THEN
        IF (NEW.origin_issue_line_id IS NULL)=(NEW.origin_import_row_id IS NULL) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='root custody requires exactly one root origin';
        END IF;
    ELSE
        IF NEW.parent_custody_id=NEW.id OR NEW.origin_issue_line_id IS NOT NULL
           OR NEW.origin_import_row_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='child custody source shape is invalid';
        END IF;
        SELECT company_id,item_id INTO ref_company,ref_item
          FROM supplies_supplycustody WHERE id=NEW.parent_custody_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='parent custody must use the same company and item';
        END IF;
    END IF;
    IF NEW.origin_issue_line_id IS NOT NULL THEN
        SELECT line.company_id,line.item_id,document.document_type,document.status
          INTO ref_company,ref_item,ref_document_type,ref_document_status
          FROM supplies_supplydocumentline line
          JOIN supplies_supplydocument document ON document.id=line.document_id
         WHERE line.id=NEW.origin_issue_line_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id
           OR ref_document_type<>'issue' OR ref_document_status NOT IN ('posted','reversed') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody origin issue line is invalid';
        END IF;
    END IF;
    IF NEW.origin_import_row_id IS NOT NULL THEN
        SELECT batch.company_id,batch.import_type,batch.status,row.validation_status
          INTO ref_company,ref_import_type,ref_import_status,ref_row_status
          FROM masterdata_importrow row
          JOIN masterdata_importbatch batch ON batch.id=row.batch_id
         WHERE row.id=NEW.origin_import_row_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id
           OR ref_import_type<>'opening_custody' OR ref_import_status<>'confirmed'
           OR ref_row_status<>'created' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody origin import row is invalid';
        END IF;
    END IF;
    SELECT company_id,is_active INTO ref_company,department_active
      FROM masterdata_department WHERE id=NEW.department_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody department belongs to another company';
    END IF;
    IF TG_OP='INSERT' AND NOT department_active THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='new custody department must be active';
    END IF;
    IF NEW.employee_id IS NOT NULL THEN
        SELECT company_id,department_id,employment_status,is_active
          INTO ref_company,employee_department,employee_status,employee_active
          FROM masterdata_employee WHERE id=NEW.employee_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR employee_department<>NEW.department_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody employee is outside the department or company';
        END IF;
        SELECT EXISTS(
            SELECT 1 FROM supplies_employeesupplyclearanceitem item
            JOIN offboarding_employeeassetclearance clearance ON clearance.id=item.clearance_id
            WHERE item.custody_id=NEW.id AND item.company_id=NEW.company_id
              AND clearance.company_id=NEW.company_id
              AND clearance.employee_id=NEW.employee_id
              AND clearance.status IN ('open','blocked','completed')
        ) INTO captured_by_clearance;
        IF TG_OP='INSERT' AND (employee_status<>'active' OR NOT employee_active OR NOT department_active)
           AND NOT (employee_status IN ('leaving','resigned') AND NOT employee_active AND captured_by_clearance) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='new custody employee must be active';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_count_line_s17()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE controlled boolean; actor_cleared boolean; task_row record;
    item_company bigint; balance_row record; custody_row record;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count snapshot line cannot be deleted';
    END IF;
    SELECT * INTO task_row FROM supplies_supplycounttask WHERE id=NEW.count_task_id;
    SELECT company_id INTO item_company FROM supplies_supplyitem WHERE id=NEW.item_id;
    IF task_row.id IS NULL OR task_row.company_id<>NEW.company_id
       OR item_company IS NULL OR item_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count line company or item mismatch';
    END IF;
    IF NEW.stock_balance_id IS NOT NULL THEN
        SELECT * INTO balance_row FROM supplies_supplystockbalance WHERE id=NEW.stock_balance_id;
        IF balance_row.id IS NULL OR balance_row.company_id<>NEW.company_id
           OR balance_row.item_id<>NEW.item_id OR balance_row.warehouse_id<>task_row.warehouse_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count line stock balance mismatch';
        END IF;
    END IF;
    IF NEW.custody_id IS NOT NULL THEN
        SELECT * INTO custody_row FROM supplies_supplycustody WHERE id=NEW.custody_id;
        IF custody_row.id IS NULL OR custody_row.company_id<>NEW.company_id
           OR custody_row.item_id<>NEW.item_id OR custody_row.department_id<>task_row.department_id
           OR (task_row.employee_id IS NOT NULL AND custody_row.employee_id IS DISTINCT FROM task_row.employee_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count line custody mismatch';
        END IF;
    END IF;
    IF TG_OP='INSERT' THEN
        controlled := COALESCE(current_setting('eam_lite.controlled_supply_count_line_insert',true),'')='on';
        IF NOT controlled OR task_row.status NOT IN ('draft','in_progress')
           OR NEW.counted_quantity IS NOT NULL OR NEW.difference_quantity IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count line insert requires publish service';
        END IF;
        IF task_row.status='in_progress' AND NOT (
            task_row.count_domain='warehouse_stock'
            AND NEW.stock_balance_id IS NULL AND NEW.custody_id IS NULL
            AND NEW.expected_quantity=0 AND NEW.expected_amount=0
            AND NEW.expected_unit_cost=0
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='only zero-stock warehouse item can be added after publish';
        END IF;
        PERFORM set_config('eam_lite.controlled_supply_count_line_insert','off',true);
        RETURN NEW;
    END IF;
    actor_cleared := (
        (OLD.counted_by_id IS NOT NULL AND NEW.counted_by_id IS NULL)
        OR (OLD.resolved_by_id IS NOT NULL AND NEW.resolved_by_id IS NULL)
    ) AND (to_jsonb(NEW)-ARRAY['counted_by_id','resolved_by_id'])
          =(to_jsonb(OLD)-ARRAY['counted_by_id','resolved_by_id']);
    IF actor_cleared THEN RETURN NEW; END IF;
    IF ROW(NEW.company_id,NEW.count_task_id,NEW.item_id,NEW.stock_balance_id,NEW.custody_id,
           NEW.item_code_snapshot,NEW.item_name_snapshot,NEW.department_snapshot,
           NEW.employee_snapshot,NEW.expected_quantity,NEW.expected_amount,NEW.expected_unit_cost)
       IS DISTINCT FROM
       ROW(OLD.company_id,OLD.count_task_id,OLD.item_id,OLD.stock_balance_id,OLD.custody_id,
           OLD.item_code_snapshot,OLD.item_name_snapshot,OLD.department_snapshot,
           OLD.employee_snapshot,OLD.expected_quantity,OLD.expected_amount,OLD.expected_unit_cost) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count line snapshot is immutable';
    END IF;
    controlled := COALESCE(current_setting('eam_lite.controlled_supply_count_line_mutation',true),'')='on';
    IF NOT controlled OR task_row.status NOT IN ('in_progress','reconciliation') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count line update requires controlled active task';
    END IF;
    IF (NEW.counted_quantity IS DISTINCT FROM OLD.counted_quantity
        OR NEW.remark IS DISTINCT FROM OLD.remark
        OR NEW.counted_by_id IS DISTINCT FROM OLD.counted_by_id
        OR NEW.counted_at IS DISTINCT FROM OLD.counted_at)
       AND task_row.status<>'in_progress' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count entry is stopped';
    END IF;
    IF (NEW.adjustment_document_line_id IS DISTINCT FROM OLD.adjustment_document_line_id
        OR NEW.resolution_custody_movement_id IS DISTINCT FROM OLD.resolution_custody_movement_id
        OR NEW.resolution_type IS DISTINCT FROM OLD.resolution_type
        OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at)
       AND task_row.status<>'reconciliation' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='count evidence requires reconciliation task';
    END IF;
    PERFORM set_config('eam_lite.controlled_supply_count_line_mutation','off',true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_count_adjustment_line_s17()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE document_row record; controlled boolean;
BEGIN
    SELECT * INTO document_row FROM supplies_supplydocument WHERE id=NEW.document_id;
    IF document_row.document_type<>'count_adjustment' THEN RETURN NEW; END IF;
    controlled := COALESCE(current_setting('eam_lite.controlled_supply_count_adjustment_line_insert',true),'')='on';
    IF TG_OP<>'INSERT' OR NOT controlled OR document_row.status<>'draft'
       OR NEW.adjustment_direction NOT IN ('increase','decrease')
       OR NEW.source_issue_line_id IS NOT NULL OR NEW.source_custody_id IS NOT NULL
       OR (NEW.adjustment_direction='increase' AND NEW.entered_unit_cost IS NULL)
       OR (NEW.adjustment_direction='decrease' AND NEW.entered_unit_cost IS NOT NULL)
       OR btrim(NEW.line_remark)='' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid controlled count adjustment line';
    END IF;
    PERFORM set_config('eam_lite.controlled_supply_count_adjustment_line_insert','off',true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_count_adjustment_ledger_s17()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE document_row record; line_row record; task_warehouse uuid;
BEGIN
    SELECT * INTO document_row FROM supplies_supplydocument WHERE id=NEW.document_id;
    IF document_row.document_type<>'count_adjustment' THEN RETURN NEW; END IF;
    SELECT * INTO line_row FROM supplies_supplydocumentline WHERE id=NEW.document_line_id;
    SELECT warehouse_id INTO task_warehouse FROM supplies_supplycounttask WHERE id=document_row.source_count_task_id;
    IF task_warehouse IS NULL OR NEW.warehouse_id<>task_warehouse
       OR (line_row.adjustment_direction='increase'
           AND (NEW.movement_type<>'count_gain' OR NEW.quantity_delta<=0 OR NEW.amount_delta<0))
       OR (line_row.adjustment_direction='decrease'
           AND (NEW.movement_type<>'count_loss' OR NEW.quantity_delta>=0 OR NEW.amount_delta>0))
       OR line_row.adjustment_direction NOT IN ('increase','decrease') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid count adjustment stock ledger';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_guard_employee_supply_clearance_item_s17()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE controlled boolean; actor_cleared boolean; clearance_row record;
    custody_row record; custody_item_type varchar; movement_row record; target_employee bigint;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='employee supply clearance item cannot be deleted';
    END IF;
    SELECT * INTO clearance_row FROM offboarding_employeeassetclearance WHERE id=NEW.clearance_id;
    SELECT * INTO custody_row FROM supplies_supplycustody WHERE id=NEW.custody_id;
    SELECT supply_item.item_type INTO custody_item_type
      FROM supplies_supplyitem supply_item WHERE supply_item.id=custody_row.item_id;
    IF clearance_row.id IS NULL OR custody_row.id IS NULL
       OR clearance_row.company_id<>NEW.company_id OR custody_row.company_id<>NEW.company_id
       OR custody_row.employee_id IS DISTINCT FROM clearance_row.employee_id
       OR custody_item_type<>'durable_quantity' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='employee supply clearance item reference mismatch';
    END IF;
    IF TG_OP='INSERT' THEN
        controlled := COALESCE(current_setting('eam_lite.controlled_employee_supply_clearance_item_insert',true),'')='on';
        IF NOT controlled OR clearance_row.status NOT IN ('open','blocked')
           OR NEW.resolution<>'pending' OR custody_row.status<>'open'
           OR NEW.quantity_snapshot<>custody_row.current_quantity
           OR NEW.amount_snapshot<>custody_row.current_amount THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='employee supply clearance insert requires controlled snapshot';
        END IF;
        PERFORM set_config('eam_lite.controlled_employee_supply_clearance_item_insert','off',true);
        RETURN NEW;
    END IF;
    actor_cleared := OLD.resolved_by_id IS NOT NULL AND NEW.resolved_by_id IS NULL
        AND (to_jsonb(NEW)-'resolved_by_id')=(to_jsonb(OLD)-'resolved_by_id');
    IF actor_cleared THEN RETURN NEW; END IF;
    IF ROW(NEW.clearance_id,NEW.company_id,NEW.custody_id,NEW.item_code_snapshot,
           NEW.item_name_snapshot,NEW.quantity_snapshot,NEW.amount_snapshot,
           NEW.department_snapshot,NEW.employee_snapshot,NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.clearance_id,OLD.company_id,OLD.custody_id,OLD.item_code_snapshot,
           OLD.item_name_snapshot,OLD.quantity_snapshot,OLD.amount_snapshot,
           OLD.department_snapshot,OLD.employee_snapshot,OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='employee supply clearance snapshot is immutable';
    END IF;
    controlled := COALESCE(current_setting('eam_lite.controlled_employee_supply_clearance_item_resolution',true),'')='on';
    IF NOT controlled OR OLD.resolution<>'pending'
       OR NEW.resolution NOT IN ('returned','transferred','lost','scrapped') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='employee supply clearance resolution requires controlled service';
    END IF;
    SELECT * INTO movement_row FROM supplies_supplycustodymovement WHERE id=NEW.custody_movement_id;
    IF movement_row.id IS NULL OR movement_row.company_id<>NEW.company_id
       OR movement_row.from_custody_id<>NEW.custody_id
       OR custody_row.status<>'closed' OR custody_row.current_quantity<>0
       OR (NEW.resolution='returned' AND movement_row.action<>'return')
       OR (NEW.resolution='transferred' AND movement_row.action<>'transfer')
       OR (NEW.resolution='lost' AND movement_row.action<>'loss')
       OR (NEW.resolution='scrapped' AND movement_row.action<>'scrap') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='employee supply clearance movement evidence mismatch';
    END IF;
    IF movement_row.action='transfer' THEN
        SELECT employee_id INTO target_employee FROM supplies_supplycustody WHERE id=movement_row.to_custody_id;
        IF target_employee IS NOT DISTINCT FROM clearance_row.employee_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='transfer back to leaving employee cannot resolve clearance';
        END IF;
    END IF;
    PERFORM set_config('eam_lite.controlled_employee_supply_clearance_item_resolution','off',true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_validate_clearance_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; clearance_row record; total_count integer;
    unresolved_count integer; supply_total integer; supply_unresolved integer;
    combined_unresolved integer; employee_status varchar; employee_termination date;
BEGIN
    IF TG_TABLE_NAME='offboarding_employeeassetclearanceitem' THEN
        IF TG_OP='DELETE' THEN target_id:=OLD.clearance_id; ELSE target_id:=NEW.clearance_id; END IF;
    ELSIF TG_TABLE_NAME='supplies_employeesupplyclearanceitem' THEN
        IF TG_OP='DELETE' THEN target_id:=OLD.clearance_id; ELSE target_id:=NEW.clearance_id; END IF;
    ELSE
        IF TG_OP='DELETE' THEN target_id:=OLD.id; ELSE target_id:=NEW.id; END IF;
    END IF;
    SELECT * INTO clearance_row FROM offboarding_employeeassetclearance WHERE id=target_id;
    IF clearance_row.id IS NULL THEN RETURN NULL; END IF;
    SELECT count(*),count(*) FILTER (WHERE resolution IN ('pending','disposal_in_progress'))
      INTO total_count,unresolved_count FROM offboarding_employeeassetclearanceitem WHERE clearance_id=target_id;
    SELECT count(*),count(*) FILTER (WHERE resolution='pending')
      INTO supply_total,supply_unresolved FROM supplies_employeesupplyclearanceitem WHERE clearance_id=target_id;
    IF clearance_row.total_assets_snapshot<>total_count
       OR clearance_row.unresolved_assets<>unresolved_count
       OR clearance_row.total_supply_custodies_snapshot<>supply_total
       OR clearance_row.unresolved_supply_custodies<>supply_unresolved THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance counters must equal asset and supply items';
    END IF;
    combined_unresolved:=unresolved_count+supply_unresolved;
    IF clearance_row.status='blocked' AND combined_unresolved=0 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='blocked clearance requires unresolved items';
    ELSIF clearance_row.status='open' AND combined_unresolved<>0 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='open clearance cannot contain unresolved items';
    ELSIF clearance_row.status='completed' AND combined_unresolved<>0 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='completed clearance cannot contain unresolved items';
    END IF;
    SELECT employment_status,termination_date INTO employee_status,employee_termination
      FROM masterdata_employee WHERE id=clearance_row.employee_id;
    IF clearance_row.status IN ('open','blocked') THEN
        IF (clearance_row.supplements_clearance_id IS NULL AND employee_status<>'leaving')
           OR (clearance_row.supplements_clearance_id IS NOT NULL
               AND (employee_status<>'resigned' OR employee_termination IS NULL)) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='active clearance employee state mismatch';
        END IF;
    ELSIF clearance_row.status='completed' THEN
        IF clearance_row.completed_at IS NULL
           OR employee_status<>'resigned' OR employee_termination IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='completed clearance employee state mismatch';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_validate_employee_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_status varchar; current_termination date; match_count integer;
BEGIN
    SELECT employment_status,termination_date
      INTO current_status,current_termination
      FROM masterdata_employee WHERE id=NEW.id;
    IF current_status IS DISTINCT FROM NEW.employment_status THEN RETURN NULL; END IF;
    IF OLD.employment_status='active' AND NEW.employment_status='leaving' THEN
        SELECT count(*) INTO match_count
          FROM offboarding_employeeassetclearance
         WHERE company_id=NEW.company_id AND employee_id=NEW.id
           AND supplements_clearance_id IS NULL AND status IN ('open','blocked');
        IF match_count<>1 THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='active to leaving requires exactly one active initial clearance';
        END IF;
    ELSIF OLD.employment_status='leaving' AND NEW.employment_status='resigned' THEN
        SELECT count(*) INTO match_count
          FROM offboarding_employeeassetclearance
         WHERE company_id=NEW.company_id AND employee_id=NEW.id
           AND supplements_clearance_id IS NULL AND status='completed';
        IF current_termination IS NULL OR match_count<>1 OR EXISTS (
            SELECT 1 FROM offboarding_employeeassetclearance
             WHERE company_id=NEW.company_id AND employee_id=NEW.id
               AND status IN ('open','blocked')
               AND supplements_clearance_id IS NULL
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='leaving to resigned requires one completed initial clearance and no active initial clearance';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_supply_count_task_s17 ON supplies_supplycounttask;
CREATE TRIGGER trg_supply_count_task_s17 BEFORE INSERT OR UPDATE OR DELETE
ON supplies_supplycounttask FOR EACH ROW EXECUTE FUNCTION supplies_guard_count_task_s17();
DROP TRIGGER IF EXISTS trg_supply_count_line_s17 ON supplies_supplycountline;
CREATE TRIGGER trg_supply_count_line_s17 BEFORE INSERT OR UPDATE OR DELETE
ON supplies_supplycountline FOR EACH ROW EXECUTE FUNCTION supplies_guard_count_line_s17();
DROP TRIGGER IF EXISTS trg_supply_count_adjustment_line_s17 ON supplies_supplydocumentline;
CREATE TRIGGER trg_supply_count_adjustment_line_s17 BEFORE INSERT
ON supplies_supplydocumentline FOR EACH ROW EXECUTE FUNCTION supplies_guard_count_adjustment_line_s17();
DROP TRIGGER IF EXISTS trg_supply_count_adjustment_ledger_s17 ON supplies_supplystockledger;
CREATE TRIGGER trg_supply_count_adjustment_ledger_s17 BEFORE INSERT
ON supplies_supplystockledger FOR EACH ROW EXECUTE FUNCTION supplies_guard_count_adjustment_ledger_s17();
DROP TRIGGER IF EXISTS trg_employee_supply_clearance_item_s17 ON supplies_employeesupplyclearanceitem;
CREATE TRIGGER trg_employee_supply_clearance_item_s17 BEFORE INSERT OR UPDATE OR DELETE
ON supplies_employeesupplyclearanceitem FOR EACH ROW EXECUTE FUNCTION supplies_guard_employee_supply_clearance_item_s17();
DROP TRIGGER IF EXISTS trg_employee_supply_clearance_commit_s17 ON supplies_employeesupplyclearanceitem;
CREATE CONSTRAINT TRIGGER trg_employee_supply_clearance_commit_s17
AFTER INSERT OR UPDATE OR DELETE ON supplies_employeesupplyclearanceitem
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION offboarding_validate_clearance_commit();
"""


DROP_SPRINT17_GUARDS = r"""
DROP TRIGGER IF EXISTS trg_employee_supply_clearance_commit_s17 ON supplies_employeesupplyclearanceitem;
DROP TRIGGER IF EXISTS trg_employee_supply_clearance_item_s17 ON supplies_employeesupplyclearanceitem;
DROP TRIGGER IF EXISTS trg_supply_count_adjustment_ledger_s17 ON supplies_supplystockledger;
DROP TRIGGER IF EXISTS trg_supply_count_adjustment_line_s17 ON supplies_supplydocumentline;
DROP TRIGGER IF EXISTS trg_supply_count_line_s17 ON supplies_supplycountline;
DROP TRIGGER IF EXISTS trg_supply_count_task_s17 ON supplies_supplycounttask;
DROP FUNCTION IF EXISTS supplies_guard_employee_supply_clearance_item_s17();
DROP FUNCTION IF EXISTS supplies_guard_count_adjustment_ledger_s17();
DROP FUNCTION IF EXISTS supplies_guard_count_adjustment_line_s17();
DROP FUNCTION IF EXISTS supplies_guard_count_line_s17();
DROP FUNCTION IF EXISTS supplies_guard_count_task_s17();
"""


def install_sprint17_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(SPRINT17_GUARDS)


def restore_previous_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP_SPRINT17_GUARDS)
    sprint15 = importlib.import_module(
        "apps.supplies.migrations.0006_sprint15_postgresql_guards"
    )
    sprint16 = importlib.import_module(
        "apps.supplies.migrations.0007_sprint16_durable_custody_lifecycle"
    )
    schema_editor.execute(sprint15.DROP_SPRINT15_GUARDS)
    schema_editor.execute(sprint15.CREATE_GUARDS)
    schema_editor.execute(sprint16.SPRINT16_GUARDS)
    sprint14 = importlib.import_module(
        "apps.supplies.migrations.0004_sprint14_postgresql_guards"
    )
    start = sprint14.CREATE_GUARDS.index(
        "CREATE OR REPLACE FUNCTION supplies_validate_document_refs_s14()"
    )
    end = sprint14.CREATE_GUARDS.index("\n$$;", start) + len("\n$$;")
    schema_editor.execute(sprint14.CREATE_GUARDS[start:end])
    offboarding = importlib.import_module(
        "apps.offboarding.migrations.0002_postgresql_clearance_guards"
    )
    schema_editor.execute(offboarding.OFFBOARDING_GUARDS_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("offboarding", "0003_sprint17_supply_clearance_counters"),
        ("supplies", "0007_sprint16_durable_custody_lifecycle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="supplycustodymovement",
            name="ck_supply_custody_movement_shape",
        ),
        migrations.RemoveConstraint(
            model_name="supplydocument",
            name="ck_supply_document_s15_shape",
        ),
        migrations.RemoveConstraint(
            model_name="supplydocumentsequence",
            name="ck_supply_doc_sequence_type",
        ),
        migrations.AlterField(
            model_name="supplydocumentsequence",
            name="sequence_type",
            field=models.CharField(
                choices=[
                    ("opening", "期初入库"),
                    ("receipt", "日常入库"),
                    ("issue", "领用出库"),
                    ("return", "领用退回"),
                    ("transfer", "仓库调拨"),
                    ("count_adjustment", "盘点调整"),
                    ("reversal", "冲销"),
                    ("count_task", "盘点任务"),
                ],
                max_length=32,
                verbose_name="序号类型",
            ),
        ),
        migrations.CreateModel(
            name="SupplyCountTask",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("task_no", models.CharField(max_length=64, verbose_name="盘点任务编号")),
                ("name", models.CharField(max_length=200, verbose_name="盘点任务名称")),
                (
                    "count_domain",
                    models.CharField(
                        choices=[
                            ("warehouse_stock", "仓库库存盘点"),
                            ("custody", "耐用品保管盘点"),
                        ],
                        max_length=32,
                        verbose_name="盘点域",
                    ),
                ),
                ("planned_start", models.DateField(verbose_name="计划开始日期")),
                ("planned_end", models.DateField(verbose_name="计划结束日期")),
                (
                    "snapshot_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="快照时间"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "草稿"),
                            ("in_progress", "进行中"),
                            ("reconciliation", "差异处理中"),
                            ("closed", "已关闭"),
                            ("cancelled", "已取消"),
                        ],
                        default="draft",
                        max_length=20,
                        verbose_name="状态",
                    ),
                ),
                (
                    "idempotency_key",
                    models.CharField(max_length=128, verbose_name="创建幂等键"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "published_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="发布时间"),
                ),
                (
                    "stopped_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="停止录入时间"),
                ),
                (
                    "closed_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="关闭时间"),
                ),
                (
                    "cancelled_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="取消时间"),
                ),
                ("cancellation_reason", models.TextField(blank=True, verbose_name="取消原因")),
                ("remark", models.TextField(blank=True, verbose_name="备注")),
                (
                    "cancelled_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="cancelled_supply_count_tasks",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="取消人",
                    ),
                ),
                (
                    "closed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="closed_supply_count_tasks",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="关闭人",
                    ),
                ),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_count_tasks",
                        to="masterdata.company",
                        verbose_name="公司",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_supply_count_tasks",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="创建人",
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_count_tasks",
                        to="masterdata.department",
                        verbose_name="盘点部门",
                    ),
                ),
                (
                    "employee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_count_tasks",
                        to="masterdata.employee",
                        verbose_name="盘点员工",
                    ),
                ),
                (
                    "published_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="published_supply_count_tasks",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="发布人",
                    ),
                ),
                (
                    "stopped_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="stopped_supply_count_tasks",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="停止录入人",
                    ),
                ),
                (
                    "warehouse",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="count_tasks",
                        to="supplies.supplywarehouse",
                        verbose_name="盘点仓库",
                    ),
                ),
            ],
            options={
                "verbose_name": "低值物品盘点任务",
                "verbose_name_plural": "低值物品盘点任务",
                "ordering": ("-created_at", "-task_no"),
                "indexes": [
                    models.Index(
                        fields=["company", "status", "count_domain"],
                        name="supply_count_status_idx",
                    ),
                    models.Index(
                        fields=["company", "department", "employee", "status"],
                        name="supply_count_custody_scope_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("company", "task_no"),
                        name="uq_supply_count_task_company_no",
                    ),
                    models.UniqueConstraint(
                        fields=("company", "idempotency_key"),
                        name="uq_supply_count_task_company_idem",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            count_domain="warehouse_stock",
                            status__in=("in_progress", "reconciliation"),
                        ),
                        fields=("company", "warehouse"),
                        name="uq_supply_count_active_warehouse",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            count_domain="custody",
                            employee__isnull=False,
                            status__in=("in_progress", "reconciliation"),
                        ),
                        fields=("company", "employee"),
                        name="uq_supply_count_active_employee",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(count_domain__in=("warehouse_stock", "custody")),
                        name="ck_supply_count_task_domain",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            status__in=(
                                "draft",
                                "in_progress",
                                "reconciliation",
                                "closed",
                                "cancelled",
                            )
                        ),
                        name="ck_supply_count_task_status",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(planned_end__gte=models.F("planned_start")),
                        name="ck_supply_count_task_dates",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                count_domain="warehouse_stock",
                                warehouse__isnull=False,
                                department__isnull=True,
                                employee__isnull=True,
                            )
                            | models.Q(
                                count_domain="custody",
                                warehouse__isnull=True,
                                department__isnull=False,
                            )
                        ),
                        name="ck_supply_count_task_scope",
                    ),
                    models.CheckConstraint(
                        condition=(
                            ~models.Q(task_no="")
                            & ~models.Q(name="")
                            & ~models.Q(idempotency_key="")
                        ),
                        name="ck_supply_count_task_required_text",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(cancelled_at__isnull=True, cancellation_reason="")
                            | (
                                models.Q(cancelled_at__isnull=False)
                                & ~models.Q(cancellation_reason="")
                            )
                        ),
                        name="ck_supply_count_task_cancel_fields",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SupplyCountLine",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("item_code_snapshot", models.CharField(max_length=100, verbose_name="物品编码快照")),
                ("item_name_snapshot", models.CharField(max_length=200, verbose_name="物品名称快照")),
                ("department_snapshot", models.CharField(blank=True, max_length=200, verbose_name="责任部门快照")),
                ("employee_snapshot", models.CharField(blank=True, max_length=200, verbose_name="责任员工快照")),
                ("expected_quantity", models.DecimalField(decimal_places=4, max_digits=18, verbose_name="应盘数量")),
                ("expected_amount", models.DecimalField(decimal_places=2, max_digits=18, verbose_name="应盘金额")),
                ("expected_unit_cost", models.DecimalField(decimal_places=6, max_digits=18, verbose_name="发布时单位成本")),
                ("counted_quantity", models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True, verbose_name="实盘数量")),
                ("difference_quantity", models.DecimalField(blank=True, decimal_places=4, max_digits=18, null=True, verbose_name="差异数量")),
                ("adjustment_unit_cost", models.DecimalField(blank=True, decimal_places=6, max_digits=18, null=True, verbose_name="盘盈调整单位成本")),
                ("zero_cost_reason", models.TextField(blank=True, verbose_name="零成本原因")),
                (
                    "resolution_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("return", "归还"),
                            ("transfer", "转交"),
                            ("loss", "报损"),
                            ("scrap", "报废"),
                            ("correction", "盘点更正"),
                        ],
                        max_length=16,
                        null=True,
                        verbose_name="解决方式",
                    ),
                ),
                ("remark", models.TextField(blank=True, verbose_name="差异原因/备注")),
                ("counted_at", models.DateTimeField(blank=True, null=True, verbose_name="盘点录入时间")),
                ("resolved_at", models.DateTimeField(blank=True, null=True, verbose_name="差异处理时间")),
                (
                    "adjustment_document_line",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="source_count_line",
                        to="supplies.supplydocumentline",
                        verbose_name="盘点调整单明细",
                    ),
                ),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_count_lines",
                        to="masterdata.company",
                        verbose_name="公司",
                    ),
                ),
                (
                    "count_task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lines",
                        to="supplies.supplycounttask",
                        verbose_name="盘点任务",
                    ),
                ),
                (
                    "counted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="recorded_supply_count_lines",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="盘点录入人",
                    ),
                ),
                (
                    "custody",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="count_lines",
                        to="supplies.supplycustody",
                        verbose_name="发布时保管记录",
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="count_lines",
                        to="supplies.supplyitem",
                        verbose_name="物品",
                    ),
                ),
                (
                    "resolution_custody_movement",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="source_count_line",
                        to="supplies.supplycustodymovement",
                        verbose_name="保管差异解决流水",
                    ),
                ),
                (
                    "resolved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="resolved_supply_count_lines",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="差异处理人",
                    ),
                ),
                (
                    "stock_balance",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="count_lines",
                        to="supplies.supplystockbalance",
                        verbose_name="发布时库存余额",
                    ),
                ),
            ],
            options={
                "verbose_name": "低值物品盘点行",
                "verbose_name_plural": "低值物品盘点行",
                "ordering": ("item_code_snapshot", "id"),
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(custody__isnull=True),
                        fields=("count_task", "item"),
                        name="uq_supply_count_warehouse_item",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(custody__isnull=False),
                        fields=("count_task", "custody"),
                        name="uq_supply_count_custody",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(stock_balance__isnull=True)
                        | models.Q(custody__isnull=True),
                        name="ck_supply_count_line_one_source",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            expected_quantity__gte=0,
                            expected_amount__gte=0,
                            expected_unit_cost__gte=0,
                        ),
                        name="ck_supply_count_line_expected",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(counted_quantity__isnull=True)
                        | models.Q(counted_quantity__gte=0),
                        name="ck_supply_count_line_counted",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(adjustment_unit_cost__isnull=True)
                        | models.Q(adjustment_unit_cost__gte=0),
                        name="ck_supply_count_line_adjust_cost",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                counted_quantity__isnull=True,
                                difference_quantity__isnull=True,
                            )
                            | models.Q(
                                counted_quantity__isnull=False,
                                difference_quantity__isnull=False,
                            )
                        ),
                        name="ck_supply_count_line_count_pair",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                adjustment_document_line__isnull=True,
                                resolution_custody_movement__isnull=True,
                                resolution_type__isnull=True,
                                resolved_by__isnull=True,
                                resolved_at__isnull=True,
                            )
                            | models.Q(
                                adjustment_document_line__isnull=False,
                                resolution_custody_movement__isnull=True,
                                resolution_type__isnull=True,
                                resolved_by__isnull=False,
                                resolved_at__isnull=False,
                            )
                            | models.Q(
                                adjustment_document_line__isnull=True,
                                resolution_custody_movement__isnull=False,
                                resolution_type__isnull=False,
                                resolved_by__isnull=False,
                                resolved_at__isnull=False,
                            )
                        ),
                        name="ck_supply_count_line_evidence",
                    ),
                    models.CheckConstraint(
                        condition=~models.Q(item_code_snapshot="")
                        & ~models.Q(item_name_snapshot=""),
                        name="ck_supply_count_line_snapshots",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="EmployeeSupplyClearanceItem",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("item_code_snapshot", models.CharField(max_length=100, verbose_name="物品编码快照")),
                ("item_name_snapshot", models.CharField(max_length=200, verbose_name="物品名称快照")),
                ("quantity_snapshot", models.DecimalField(decimal_places=4, max_digits=18, verbose_name="数量快照")),
                ("amount_snapshot", models.DecimalField(decimal_places=2, max_digits=18, verbose_name="金额快照")),
                ("department_snapshot", models.CharField(max_length=200, verbose_name="责任部门快照")),
                ("employee_snapshot", models.CharField(max_length=200, verbose_name="责任员工快照")),
                (
                    "resolution",
                    models.CharField(
                        choices=[
                            ("pending", "待处理"),
                            ("returned", "已归还"),
                            ("transferred", "已转交"),
                            ("lost", "已报损"),
                            ("scrapped", "已报废"),
                        ],
                        default="pending",
                        max_length=16,
                        verbose_name="解决方式",
                    ),
                ),
                ("resolved_at", models.DateTimeField(blank=True, null=True, verbose_name="处理时间")),
                ("remark", models.TextField(blank=True, verbose_name="备注")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "clearance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_items",
                        to="offboarding.employeeassetclearance",
                        verbose_name="离职清退单",
                    ),
                ),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="employee_supply_clearance_items",
                        to="masterdata.company",
                        verbose_name="公司",
                    ),
                ),
                (
                    "custody",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="clearance_items",
                        to="supplies.supplycustody",
                        verbose_name="耐用品保管记录",
                    ),
                ),
                (
                    "custody_movement",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="clearance_items",
                        to="supplies.supplycustodymovement",
                        verbose_name="解决保管流水",
                    ),
                ),
                (
                    "resolved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="resolved_employee_supply_clearance_items",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="处理人",
                    ),
                ),
            ],
            options={
                "verbose_name": "员工离职耐用品清退项目",
                "verbose_name_plural": "员工离职耐用品清退项目",
                "ordering": ("clearance_id", "item_code_snapshot", "id"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clearance", "custody"),
                        name="uq_employee_supply_clearance_custody",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            quantity_snapshot__gt=0,
                            amount_snapshot__gte=0,
                        ),
                        name="ck_employee_supply_clearance_amounts",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            resolution__in=(
                                "pending",
                                "returned",
                                "transferred",
                                "lost",
                                "scrapped",
                            )
                        ),
                        name="ck_employee_supply_clearance_resolution",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                resolution="pending",
                                resolved_by__isnull=True,
                                resolved_at__isnull=True,
                                custody_movement__isnull=True,
                            )
                            | models.Q(
                                resolution__in=(
                                    "returned",
                                    "transferred",
                                    "lost",
                                    "scrapped",
                                ),
                                resolved_by__isnull=False,
                                resolved_at__isnull=False,
                                custody_movement__isnull=False,
                            )
                        ),
                        name="ck_employee_supply_clearance_evidence",
                    ),
                    models.CheckConstraint(
                        condition=(
                            ~models.Q(item_code_snapshot="")
                            & ~models.Q(item_name_snapshot="")
                            & ~models.Q(department_snapshot="")
                            & ~models.Q(employee_snapshot="")
                        ),
                        name="ck_employee_supply_clearance_snapshots",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="supplydocument",
            name="source_count_task",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="adjustment_document",
                to="supplies.supplycounttask",
                verbose_name="来源盘点任务",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        action__in=("issue", "opening"),
                        from_custody__isnull=True,
                        to_custody__isnull=False,
                    )
                    | models.Q(
                        action__in=("return", "loss", "scrap"),
                        from_custody__isnull=False,
                        to_custody__isnull=True,
                    )
                    | (
                        models.Q(
                            action="transfer",
                            from_custody__isnull=False,
                            to_custody__isnull=False,
                        )
                        & ~models.Q(from_custody=models.F("to_custody"))
                    )
                    | (
                        models.Q(action="correction")
                        & (
                            models.Q(
                                from_custody__isnull=True,
                                to_custody__isnull=False,
                            )
                            | models.Q(
                                from_custody__isnull=False,
                                to_custody__isnull=True,
                            )
                        )
                    )
                    | (
                        models.Q(action="reversal")
                        & (
                            models.Q(
                                from_custody__isnull=True,
                                to_custody__isnull=False,
                            )
                            | models.Q(
                                from_custody__isnull=False,
                                to_custody__isnull=True,
                            )
                            | (
                                models.Q(
                                    from_custody__isnull=False,
                                    to_custody__isnull=False,
                                )
                                & ~models.Q(from_custody=models.F("to_custody"))
                            )
                        )
                    )
                ),
                name="ck_supply_custody_movement_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.CheckConstraint(
                condition=~models.Q(action="correction") | ~models.Q(reason=""),
                name="ck_supply_custody_correction_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplydocument",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        document_type__in=("opening", "receipt"),
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | models.Q(
                        document_type="issue",
                        source_warehouse__isnull=False,
                        target_warehouse__isnull=True,
                        department__isnull=False,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | models.Q(
                        document_type="return",
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=False,
                        department__isnull=False,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | models.Q(
                        document_type="transfer",
                        source_warehouse__isnull=False,
                        target_warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                        source_count_task__isnull=True,
                    )
                    | models.Q(
                        document_type="reversal",
                        reversal_of__isnull=False,
                        source_count_task__isnull=True,
                    )
                    | models.Q(
                        document_type="count_adjustment",
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=True,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                        source_count_task__isnull=False,
                    )
                ),
                name="ck_supply_document_s17_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplydocumentsequence",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    sequence_type__in=(
                        "opening",
                        "receipt",
                        "issue",
                        "return",
                        "transfer",
                        "count_adjustment",
                        "reversal",
                        "count_task",
                    )
                ),
                name="ck_supply_doc_sequence_type",
            ),
        ),
        migrations.RunPython(
            install_sprint17_guards,
            reverse_code=restore_previous_guards,
        ),
    ]
