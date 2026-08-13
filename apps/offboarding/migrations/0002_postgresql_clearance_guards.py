from django.db import migrations


# These guards deliberately live in a follow-up migration.  0001 creates the
# circularly referenced clearance tables, while assets.0012 then adds the real
# AttachmentLink foreign keys.  This migration is therefore the first point at
# which every Sprint 10 table exists and commit-time invariants can be installed
# without fake integer references.
OFFBOARDING_GUARDS_SQL = r"""
CREATE OR REPLACE FUNCTION offboarding_validate_employee_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE controlled boolean;
BEGIN
    IF ROW(NEW.employment_status,NEW.termination_date,NEW.is_active)
       IS NOT DISTINCT FROM ROW(OLD.employment_status,OLD.termination_date,OLD.is_active) THEN
        RETURN NEW;
    END IF;

    -- An active employee may still be independently enabled/disabled.  Once
    -- leaving starts, however, status, termination date and active=false are a
    -- single controlled offboarding transition.
    IF NEW.employment_status=OLD.employment_status
       AND NEW.employment_status='active'
       AND NEW.termination_date IS NOT DISTINCT FROM OLD.termination_date THEN
        RETURN NEW;
    END IF;

    controlled := COALESCE(
        current_setting('eam_lite.controlled_employee_offboarding',true),''
    )='on';
    IF NOT controlled OR NOT (
        (OLD.employment_status='active' AND NEW.employment_status='leaving'
         AND NEW.is_active=false AND NEW.termination_date IS NULL)
        OR
        (OLD.employment_status='leaving' AND NEW.employment_status='resigned'
         AND NEW.is_active=false AND NEW.termination_date IS NOT NULL
         AND (NEW.hire_date IS NULL OR NEW.termination_date>=NEW.hire_date)
         AND NEW.termination_date<=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='employee offboarding status requires the controlled clearance service';
    END IF;
    PERFORM set_config('eam_lite.controlled_employee_offboarding','off',true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_validate_employee_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_status varchar; current_termination date; match_count integer;
BEGIN
    SELECT employment_status,termination_date
      INTO current_status,current_termination
      FROM masterdata_employee WHERE id=NEW.id;
    IF current_status IS DISTINCT FROM NEW.employment_status THEN
        RETURN NULL;
    END IF;
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
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='leaving to resigned requires one completed initial clearance and no active clearance';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_validate_clearance_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE controlled boolean; actor_cleared boolean; employee_company bigint;
    employee_status varchar; employee_termination date; original record;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance history cannot be deleted';
    END IF;

    IF TG_OP='INSERT' THEN
        controlled := COALESCE(
            current_setting('eam_lite.controlled_clearance_insert',true),''
        )='on';
        IF NOT controlled THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='clearance must be created by the controlled service';
        END IF;
        IF NEW.initiated_by_id IS NULL OR NEW.initiated_at>clock_timestamp()
           OR btrim(NEW.idempotency_key)='' OR NEW.status NOT IN ('open','blocked')
           OR NEW.completed_at IS NOT NULL OR NEW.completed_by_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid new clearance';
        END IF;
        SELECT company_id,employment_status,termination_date
          INTO employee_company,employee_status,employee_termination
          FROM masterdata_employee WHERE id=NEW.employee_id;
        IF employee_company IS NULL OR employee_company<>NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance employee company mismatch';
        END IF;
        IF NEW.supplements_clearance_id IS NULL THEN
            -- The Employee and Clearance writes are in one transaction; allow
            -- either statement order and let the deferred trigger require the
            -- final leaving state.
            IF employee_status NOT IN ('active','leaving') OR NEW.supplement_reason<>'' THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid initial clearance employee state';
            END IF;
        ELSE
            SELECT * INTO original FROM offboarding_employeeassetclearance
             WHERE id=NEW.supplements_clearance_id;
            IF original.id IS NULL OR original.company_id<>NEW.company_id
               OR original.employee_id<>NEW.employee_id
               OR original.supplements_clearance_id IS NOT NULL
               OR original.status<>'completed'
               OR employee_status<>'resigned' OR employee_termination IS NULL
               OR btrim(NEW.supplement_reason)='' THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid supplemental clearance reference';
            END IF;
        END IF;
        PERFORM set_config('eam_lite.controlled_clearance_insert','off',true);
        RETURN NEW;
    END IF;

    actor_cleared := (
        (OLD.initiated_by_id IS NOT NULL AND NEW.initiated_by_id IS NULL)
        OR (OLD.completed_by_id IS NOT NULL AND NEW.completed_by_id IS NULL)
    ) AND (to_jsonb(NEW)-'initiated_by_id'-'completed_by_id')
          =(to_jsonb(OLD)-'initiated_by_id'-'completed_by_id')
      AND (NEW.initiated_by_id IS NOT DISTINCT FROM OLD.initiated_by_id
           OR (OLD.initiated_by_id IS NOT NULL AND NEW.initiated_by_id IS NULL))
      AND (NEW.completed_by_id IS NOT DISTINCT FROM OLD.completed_by_id
           OR (OLD.completed_by_id IS NOT NULL AND NEW.completed_by_id IS NULL));
    IF actor_cleared THEN RETURN NEW; END IF;

    IF ROW(NEW.company_id,NEW.employee_id,NEW.supplements_clearance_id,
           NEW.supplement_reason,NEW.initiated_at,NEW.initiated_by_id,
           NEW.idempotency_key)
       IS DISTINCT FROM
       ROW(OLD.company_id,OLD.employee_id,OLD.supplements_clearance_id,
           OLD.supplement_reason,OLD.initiated_at,OLD.initiated_by_id,
           OLD.idempotency_key) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance identity is immutable';
    END IF;
    controlled := COALESCE(
        current_setting('eam_lite.controlled_clearance_mutation',true),''
    )='on';
    IF NOT controlled THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='clearance state requires the controlled service';
    END IF;
    IF OLD.status IN ('completed','cancelled') OR NOT (
        NEW.status=OLD.status
        OR (OLD.status IN ('open','blocked') AND NEW.status IN ('open','blocked','completed','cancelled'))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid clearance status transition';
    END IF;
    IF NEW.status='completed' AND OLD.status<>'completed'
       AND (NEW.completed_at IS NULL OR NEW.completed_by_id IS NULL
            OR NEW.unresolved_assets<>0) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='clearance completion requires zero unresolved items, time and operator';
    END IF;
    PERFORM set_config('eam_lite.controlled_clearance_mutation','off',true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_validate_item_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE controlled boolean; actor_cleared boolean; clearance_row record;
    asset_row record; employee_row record; loan_row record; movement_row record;
    disposal_row record; expected_association timestamptz; responsibility_at timestamptz;
    loan_at timestamptz; loan_movement_at timestamptz;
    expected_path text; active_qr_count integer;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance item history cannot be deleted';
    END IF;

    IF TG_OP='INSERT' THEN
        controlled := COALESCE(
            current_setting('eam_lite.controlled_clearance_item_insert',true),''
        )='on';
        IF NOT controlled THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='clearance item must be created by the controlled service';
        END IF;
        SELECT * INTO clearance_row FROM offboarding_employeeassetclearance
         WHERE id=NEW.clearance_id;
        SELECT company_id,asset_code,asset_name,asset_status,record_status,
               department_id,responsible_employee_id,location_id
          INTO asset_row FROM assets_asset WHERE id=NEW.asset_id;
        IF clearance_row.id IS NULL OR clearance_row.company_id<>NEW.company_id
           OR clearance_row.status NOT IN ('open','blocked')
           OR asset_row.company_id IS NULL OR asset_row.company_id<>NEW.company_id
           OR asset_row.record_status<>'active' OR asset_row.asset_code IS NULL
           OR asset_row.asset_status NOT IN (
                'pending_label','in_use','idle','loaned','under_repair','pending_disposal'
           )
           OR NEW.original_department_id<>asset_row.department_id
           OR NEW.original_employee_id<>asset_row.responsible_employee_id
           OR NEW.original_location_id<>asset_row.location_id
           OR NEW.asset_code_snapshot<>asset_row.asset_code
           OR NEW.asset_name_snapshot<>asset_row.asset_name
           OR NEW.original_status<>asset_row.asset_status THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance item asset snapshot mismatch';
        END IF;
        SELECT company_id,name INTO employee_row
          FROM masterdata_employee WHERE id=NEW.original_employee_id;
        IF employee_row.company_id<>NEW.company_id
           OR NEW.original_employee_snapshot<>employee_row.name THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance item employee snapshot mismatch';
        END IF;
        SELECT company_id,name INTO employee_row
          FROM masterdata_department WHERE id=NEW.original_department_id;
        IF employee_row.company_id<>NEW.company_id
           OR NEW.original_department_snapshot<>employee_row.name THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance item department snapshot mismatch';
        END IF;
        WITH RECURSIVE location_ancestors(id,parent_id,name,depth,company_id) AS (
            SELECT id,parent_id,name,0,company_id FROM masterdata_location
             WHERE id=NEW.original_location_id
            UNION ALL
            SELECT parent.id,parent.parent_id,parent.name,child.depth+1,parent.company_id
              FROM masterdata_location parent
              JOIN location_ancestors child ON child.parent_id=parent.id
             WHERE child.depth<100
        )
        SELECT string_agg(name,' / ' ORDER BY depth DESC),
               count(*) FILTER (WHERE company_id<>NEW.company_id)
          INTO expected_path,active_qr_count FROM location_ancestors;
        IF expected_path IS NULL OR active_qr_count<>0
           OR expected_path<>NEW.original_location_path_snapshot THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance item location snapshot mismatch';
        END IF;

        IF NEW.source_type IN ('responsibility','both') THEN
            IF asset_row.responsible_employee_id<>clearance_row.employee_id THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='responsibility source is not current';
            END IF;
            IF asset_row.asset_status='pending_label' THEN
                SELECT count(*),max(issued_at) INTO active_qr_count,responsibility_at
                  FROM assets_assetqridentity
                 WHERE company_id=NEW.company_id AND asset_id=NEW.asset_id
                   AND status='active';
                IF active_qr_count<>1 THEN
                    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='pending-label responsibility requires one active QR identity';
                END IF;
                -- Approved Sprint 10 fallback: pending_label has no activation
                -- Movement.  The active QR issued_at is created in the same
                -- financial-formalization transaction and is the only durable
                -- association time; Asset.created_at is never accepted.
            ELSE
                SELECT effective_at INTO responsibility_at
                  FROM assets_assetmovement
                 WHERE company_id=NEW.company_id AND asset_id=NEW.asset_id
                   AND to_employee_id=clearance_row.employee_id
                   AND (movement_type='label_activation'
                        OR from_employee_id IS DISTINCT FROM to_employee_id)
                 ORDER BY effective_at DESC,created_at DESC,id DESC LIMIT 1;
                IF responsibility_at IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='responsibility source lacks establishing movement evidence';
                END IF;
            END IF;
        END IF;
        IF NEW.source_type IN ('internal_loan','both') THEN
            SELECT company_id,asset_id,borrower_type,borrower_employee_id,status,
                   loan_movement_id,loan_date
              INTO loan_row FROM assets_assetloan WHERE id=NEW.source_loan_id;
            IF loan_row.company_id IS NULL OR loan_row.company_id<>NEW.company_id
               OR loan_row.asset_id<>NEW.asset_id OR loan_row.borrower_type<>'internal_employee'
               OR loan_row.borrower_employee_id<>clearance_row.employee_id
               OR loan_row.status<>'active' THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid active internal-loan clearance source';
            END IF;
            SELECT effective_at INTO loan_movement_at FROM assets_assetmovement
             WHERE id=loan_row.loan_movement_id AND company_id=NEW.company_id
               AND asset_id=NEW.asset_id AND movement_type='loan';
            IF loan_movement_at IS NULL THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='loan source lacks movement effective time';
            END IF;
            -- docs/04 §11.2 defines the association date for an internal Loan
            -- as loan_date.  Interpret that business date at Shanghai midnight;
            -- the structured loan Movement remains mandatory provenance but is
            -- not substituted as the association timestamp.
            loan_at := loan_row.loan_date::timestamp AT TIME ZONE 'Asia/Shanghai';
        END IF;
        expected_association := CASE NEW.source_type
            WHEN 'responsibility' THEN responsibility_at
            WHEN 'internal_loan' THEN loan_at
            ELSE GREATEST(responsibility_at,loan_at) END;
        IF NEW.association_effective_at IS DISTINCT FROM expected_association
           OR NEW.association_effective_at>clearance_row.initiated_at
           OR NEW.discovered_at<clearance_row.initiated_at
           OR (NEW.added_during_clearance AND NEW.discovered_at<=clearance_row.initiated_at)
           OR (NOT NEW.added_during_clearance AND NEW.discovered_at<>clearance_row.initiated_at) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid clearance association or discovery time';
        END IF;
        PERFORM set_config('eam_lite.controlled_clearance_item_insert','off',true);
        RETURN NEW;
    END IF;

    actor_cleared := OLD.resolved_by_id IS NOT NULL AND NEW.resolved_by_id IS NULL
        AND (to_jsonb(NEW)-'resolved_by_id')=(to_jsonb(OLD)-'resolved_by_id');
    IF actor_cleared THEN RETURN NEW; END IF;
    IF ROW(NEW.company_id,NEW.clearance_id,NEW.asset_id,NEW.source_type,
           NEW.source_loan_id,NEW.association_effective_at,NEW.discovered_at,
           NEW.addition_reason,NEW.asset_code_snapshot,NEW.asset_name_snapshot,
           NEW.original_department_id,NEW.original_employee_id,NEW.original_location_id,
           NEW.original_department_snapshot,NEW.original_employee_snapshot,
           NEW.original_location_path_snapshot,NEW.original_status,
           NEW.added_during_clearance)
       IS DISTINCT FROM
       ROW(OLD.company_id,OLD.clearance_id,OLD.asset_id,OLD.source_type,
           OLD.source_loan_id,OLD.association_effective_at,OLD.discovered_at,
           OLD.addition_reason,OLD.asset_code_snapshot,OLD.asset_name_snapshot,
           OLD.original_department_id,OLD.original_employee_id,OLD.original_location_id,
           OLD.original_department_snapshot,OLD.original_employee_snapshot,
           OLD.original_location_path_snapshot,OLD.original_status,
           OLD.added_during_clearance) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance item source and snapshot are immutable';
    END IF;
    controlled := COALESCE(
        current_setting('eam_lite.controlled_clearance_item_resolution',true),''
    )='on';
    IF NOT controlled THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance item resolution requires the controlled service';
    END IF;
    SELECT * INTO clearance_row FROM offboarding_employeeassetclearance WHERE id=NEW.clearance_id;
    IF clearance_row.status NOT IN ('open','blocked') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='completed clearance items are immutable';
    END IF;
    IF OLD.resolution IN ('returned','transferred','disposed')
       AND NEW.resolution IS DISTINCT FROM OLD.resolution THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='resolved clearance item is immutable';
    END IF;
    IF NEW.resolution IS NOT DISTINCT FROM OLD.resolution THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='clearance item evidence cannot change without a resolution transition';
    END IF;
    -- Intermediate lifecycle services may already have returned/transferred or
    -- cancelled a disposal before the synchronizer updates this item.  That
    -- authoritative current state is intentionally allowed to flow back to
    -- pending in the same transaction.
    IF NOT (
        (OLD.resolution='pending' AND NEW.resolution IN ('returned','transferred','disposal_in_progress','disposed'))
        OR (OLD.resolution='disposal_in_progress' AND NEW.resolution IN ('pending','disposed'))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid clearance item resolution transition';
    END IF;
        IF NEW.resolution IN ('returned','transferred')
           AND NEW.resolution IS DISTINCT FROM OLD.resolution THEN
        SELECT company_id,asset_id,movement_type,to_employee_id
          INTO movement_row FROM assets_assetmovement WHERE id=NEW.movement_id;
        IF NEW.resolved_by_id IS NULL OR movement_row.company_id<>NEW.company_id
           OR movement_row.asset_id<>NEW.asset_id
           OR movement_row.movement_type NOT IN ('assignment_return','transfer','loan_return')
           OR EXISTS (
                SELECT 1 FROM assets_asset
                 WHERE id=NEW.asset_id AND record_status='active'
                   AND asset_status NOT IN ('disposed','sold','other_disposed')
                   AND responsible_employee_id=clearance_row.employee_id
           ) OR EXISTS (
                SELECT 1 FROM assets_assetloan WHERE company_id=NEW.company_id
                  AND asset_id=NEW.asset_id AND borrower_type='internal_employee'
                  AND borrower_employee_id=clearance_row.employee_id AND status='active'
           ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='returned or transferred item still has an active employee relation';
        END IF;
    ELSIF NEW.resolution='disposal_in_progress'
          AND NEW.resolution IS DISTINCT FROM OLD.resolution THEN
        SELECT company_id,asset_id,status INTO disposal_row
          FROM assets_assetdisposal WHERE id=NEW.disposal_id;
        IF disposal_row.company_id<>NEW.company_id OR disposal_row.asset_id<>NEW.asset_id
           OR disposal_row.status NOT IN ('draft','finance_locked') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid in-progress disposal evidence';
        END IF;
    ELSIF NEW.resolution='disposed'
          AND NEW.resolution IS DISTINCT FROM OLD.resolution THEN
        SELECT company_id,asset_id,status INTO disposal_row
          FROM assets_assetdisposal WHERE id=NEW.disposal_id;
        IF NEW.resolved_by_id IS NULL OR disposal_row.company_id<>NEW.company_id
           OR disposal_row.asset_id<>NEW.asset_id OR disposal_row.status<>'confirmed'
           OR NOT EXISTS (
                SELECT 1 FROM assets_asset WHERE id=NEW.asset_id
                  AND asset_status IN ('disposed','sold','other_disposed')
           ) OR EXISTS (
                SELECT 1 FROM assets_assetloan WHERE company_id=NEW.company_id
                  AND asset_id=NEW.asset_id AND borrower_type='internal_employee'
                  AND borrower_employee_id=clearance_row.employee_id AND status='active'
           ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid confirmed disposal clearance evidence';
        END IF;
    END IF;
    IF NEW.resolution IN ('returned','transferred','disposed')
       AND NEW.resolution IS DISTINCT FROM OLD.resolution
       AND NEW.resolved_by_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='resolved clearance item requires an operator';
    END IF;
    PERFORM set_config('eam_lite.controlled_clearance_item_resolution','off',true);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_validate_clearance_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; clearance_row record; total_count integer;
    unresolved_count integer; employee_status varchar; employee_termination date;
BEGIN
    -- Do not use a heterogeneous CASE over NEW/OLD fields: PostgreSQL trigger
    -- records have the concrete row type of their source table.
    IF TG_TABLE_NAME='offboarding_employeeassetclearanceitem' THEN
        IF TG_OP='DELETE' THEN target_id := OLD.clearance_id;
        ELSE target_id := NEW.clearance_id;
        END IF;
    ELSE
        IF TG_OP='DELETE' THEN target_id := OLD.id;
        ELSE target_id := NEW.id;
        END IF;
    END IF;
    SELECT * INTO clearance_row FROM offboarding_employeeassetclearance WHERE id=target_id;
    IF clearance_row.id IS NULL THEN RETURN NULL; END IF;
    SELECT count(*),count(*) FILTER (WHERE resolution IN ('pending','disposal_in_progress'))
      INTO total_count,unresolved_count
      FROM offboarding_employeeassetclearanceitem WHERE clearance_id=target_id;
    IF clearance_row.total_assets_snapshot<>total_count
       OR clearance_row.unresolved_assets<>unresolved_count THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance counters must equal authoritative items';
    END IF;
    IF clearance_row.status='blocked' AND unresolved_count=0 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='blocked clearance requires unresolved items';
    ELSIF clearance_row.status='open' AND unresolved_count<>0 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='open clearance cannot contain unresolved items';
    ELSIF clearance_row.status='completed' AND unresolved_count<>0 THEN
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
        -- completed_by may later become NULL through the declared SET_NULL FK;
        -- the controlled status transition above requires a real operator.
        IF clearance_row.completed_at IS NULL
           OR employee_status<>'resigned' OR employee_termination IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='completed clearance employee state mismatch';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_assert_clearance_item_evidence(target_item uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE item_row record; clearance_employee bigint; asset_row record;
    evidence_company bigint; evidence_asset uuid; evidence_type varchar; evidence_status varchar;
BEGIN
    SELECT * INTO item_row FROM offboarding_employeeassetclearanceitem WHERE id=target_item;
    IF item_row.id IS NULL THEN RETURN; END IF;
    SELECT employee_id INTO clearance_employee FROM offboarding_employeeassetclearance WHERE id=item_row.clearance_id;
    SELECT asset_status,record_status,responsible_employee_id INTO asset_row FROM assets_asset WHERE id=item_row.asset_id;
    IF item_row.resolution IN ('returned','transferred') THEN
        SELECT company_id,asset_id,movement_type INTO evidence_company,evidence_asset,evidence_type
          FROM assets_assetmovement WHERE id=item_row.movement_id;
        IF evidence_company<>item_row.company_id OR evidence_asset<>item_row.asset_id
           OR evidence_type NOT IN ('assignment_return','transfer','loan_return')
           OR (asset_row.record_status='active'
               AND asset_row.asset_status NOT IN ('disposed','sold','other_disposed')
               AND asset_row.responsible_employee_id=clearance_employee)
           OR EXISTS (SELECT 1 FROM assets_assetloan WHERE company_id=item_row.company_id
                       AND asset_id=item_row.asset_id AND borrower_type='internal_employee'
                       AND borrower_employee_id=clearance_employee AND status='active') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='clearance evidence no longer resolves all active employee relations';
        END IF;
    ELSIF item_row.resolution='disposed' THEN
        SELECT company_id,asset_id,status INTO evidence_company,evidence_asset,evidence_status
          FROM assets_assetdisposal WHERE id=item_row.disposal_id;
        IF evidence_company<>item_row.company_id OR evidence_asset<>item_row.asset_id
           OR evidence_status<>'confirmed'
           OR asset_row.asset_status NOT IN ('disposed','sold','other_disposed')
           OR EXISTS (SELECT 1 FROM assets_assetloan WHERE company_id=item_row.company_id
                       AND asset_id=item_row.asset_id AND borrower_type='internal_employee'
                       AND borrower_employee_id=clearance_employee AND status='active') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='disposed clearance evidence is no longer authoritative';
        END IF;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_validate_item_evidence_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id uuid; item_row record;
BEGIN
    IF TG_TABLE_NAME='offboarding_employeeassetclearanceitem' THEN
        IF TG_OP='DELETE' THEN target_id := OLD.id;
        ELSE target_id := NEW.id;
        END IF;
        PERFORM offboarding_assert_clearance_item_evidence(target_id);
    ELSIF TG_TABLE_NAME='assets_asset' THEN
        IF TG_OP='DELETE' THEN target_id := OLD.id;
        ELSE target_id := NEW.id;
        END IF;
        FOR item_row IN
            SELECT id FROM offboarding_employeeassetclearanceitem
             WHERE asset_id=target_id AND resolution IN ('returned','transferred','disposed')
        LOOP
            PERFORM offboarding_assert_clearance_item_evidence(item_row.id);
        END LOOP;
    ELSE
        IF TG_OP='DELETE' THEN target_id := OLD.asset_id;
        ELSE target_id := NEW.asset_id;
        END IF;
        FOR item_row IN
            SELECT id FROM offboarding_employeeassetclearanceitem
             WHERE asset_id=target_id AND resolution IN ('returned','transferred','disposed')
        LOOP
            PERFORM offboarding_assert_clearance_item_evidence(item_row.id);
        END LOOP;
    END IF;
    RETURN NULL;
END;
$$;

-- The core Asset validator intentionally requires an active responsible
-- employee whenever responsibility itself changes.  Clearance also needs to
-- permit lifecycle-only status writes (disposal start/cancel/complete) while
-- an unchanged leaving/resigned employee remains as the historical last
-- holder.  Replacing the function keeps every existing check and narrows the
-- active-employee exception to an unchanged responsibility pair on UPDATE.
CREATE OR REPLACE FUNCTION assets_validate_asset_references()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    category_company bigint; category_active boolean;
    department_company bigint; department_active boolean;
    employee_company bigint; employee_department bigint; employee_status varchar;
    employee_active boolean; employee_department_active boolean;
    location_company bigint; location_active boolean;
    scheme_company bigint; scheme_status varchar; scheme_from date; scheme_to date;
    shanghai_today date; controlled_mutation boolean; actor_cleared boolean;
BEGIN
    IF TG_OP='INSERT' AND NEW.asset_status<>'draft' THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='assets must be created as drafts'; END IF;
    IF TG_OP='UPDATE' THEN
        controlled_mutation := COALESCE(current_setting('eam_lite.controlled_asset_mutation',true),'')='on';
        actor_cleared := OLD.submitted_by_id IS NOT NULL AND NEW.submitted_by_id IS NULL
            AND ROW(NEW.asset_status,NEW.record_status,NEW.asset_code,NEW.current_issued_code_id,
                    NEW.requested_coding_scheme_id,NEW.submitted_at,NEW.department_id,
                    NEW.responsible_employee_id,NEW.location_id)
                IS NOT DISTINCT FROM
                ROW(OLD.asset_status,OLD.record_status,OLD.asset_code,OLD.current_issued_code_id,
                    OLD.requested_coding_scheme_id,OLD.submitted_at,OLD.department_id,
                    OLD.responsible_employee_id,OLD.location_id);
        IF ROW(NEW.asset_status,NEW.record_status,NEW.asset_code,NEW.current_issued_code_id,
               NEW.requested_coding_scheme_id,NEW.submitted_by_id,NEW.submitted_at,
               NEW.department_id,NEW.responsible_employee_id,NEW.location_id)
           IS DISTINCT FROM
           ROW(OLD.asset_status,OLD.record_status,OLD.asset_code,OLD.current_issued_code_id,
               OLD.requested_coding_scheme_id,OLD.submitted_by_id,OLD.submitted_at,
               OLD.department_id,OLD.responsible_employee_id,OLD.location_id)
           AND NOT controlled_mutation AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='asset protected state must be changed by controlled service';
        END IF;
        IF controlled_mutation AND ROW(NEW.asset_status,NEW.record_status,NEW.asset_code,
               NEW.current_issued_code_id,NEW.requested_coding_scheme_id,NEW.submitted_by_id,
               NEW.submitted_at,NEW.department_id,NEW.responsible_employee_id,NEW.location_id)
           IS DISTINCT FROM ROW(OLD.asset_status,OLD.record_status,OLD.asset_code,
               OLD.current_issued_code_id,OLD.requested_coding_scheme_id,OLD.submitted_by_id,
               OLD.submitted_at,OLD.department_id,OLD.responsible_employee_id,OLD.location_id) THEN
            PERFORM set_config('eam_lite.controlled_asset_mutation','off',true);
        END IF;
        IF NEW.asset_status IS DISTINCT FROM OLD.asset_status AND NOT (
            (OLD.asset_status='draft' AND NEW.asset_status='pending_finance')
            OR (OLD.asset_status='pending_finance' AND NEW.asset_status='draft')
            OR (OLD.asset_status='pending_finance' AND NEW.asset_status='pending_label')
            OR (OLD.asset_status='pending_label' AND NEW.asset_status IN ('in_use','idle'))
            OR (OLD.asset_status='in_use' AND NEW.asset_status IN ('idle','loaned','under_repair','pending_disposal'))
            OR (OLD.asset_status='idle' AND NEW.asset_status IN ('in_use','loaned','under_repair','pending_disposal'))
            OR (OLD.asset_status='loaned' AND NEW.asset_status IN ('in_use','idle'))
            OR (OLD.asset_status='under_repair' AND NEW.asset_status IN ('in_use','idle','pending_disposal'))
            OR (OLD.asset_status='pending_disposal' AND NEW.asset_status IN ('in_use','idle','under_repair','disposed','sold','other_disposed'))
            OR (OLD.asset_status IN ('disposed','sold','other_disposed') AND NEW.asset_status IN ('in_use','idle','under_repair'))
        ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='asset status transition is not approved'; END IF;
        IF NEW.record_status IS DISTINCT FROM OLD.record_status
           AND (NEW.asset_status NOT IN ('disposed','sold','other_disposed')
                OR OLD.asset_status NOT IN ('disposed','sold','other_disposed')) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='only terminal assets can be archived or restored';
        END IF;
        IF OLD.asset_status NOT IN ('draft','pending_finance')
           AND ROW(NEW.asset_code,NEW.current_issued_code_id,NEW.requested_coding_scheme_id)
               IS DISTINCT FROM ROW(OLD.asset_code,OLD.current_issued_code_id,OLD.requested_coding_scheme_id)
           AND NOT controlled_mutation THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='formal asset coding can only change through controlled correction';
        END IF;
    END IF;
    SELECT company_id,is_active INTO category_company,category_active FROM masterdata_assetcategory WHERE id=NEW.category_id FOR SHARE;
    IF category_company IS NULL OR category_company<>NEW.company_id OR NOT category_active THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='asset category must be active in same company'; END IF;
    IF NEW.department_id IS NOT NULL THEN
        SELECT company_id,is_active INTO department_company,department_active FROM masterdata_department WHERE id=NEW.department_id FOR SHARE;
        IF department_company<>NEW.company_id OR NOT department_active THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='asset department must be active in same company'; END IF;
    END IF;
    IF NEW.responsible_employee_id IS NOT NULL THEN
        SELECT e.company_id,e.department_id,e.employment_status,e.is_active,d.is_active
          INTO employee_company,employee_department,employee_status,employee_active,employee_department_active
          FROM masterdata_employee e JOIN masterdata_department d ON d.id=e.department_id
         WHERE e.id=NEW.responsible_employee_id FOR SHARE OF e,d;
        IF employee_company<>NEW.company_id OR employee_department<>NEW.department_id
           OR NOT employee_department_active
           OR ((TG_OP='INSERT' OR NEW.responsible_employee_id IS DISTINCT FROM OLD.responsible_employee_id
                OR NEW.department_id IS DISTINCT FROM OLD.department_id)
               AND (employee_status<>'active' OR NOT employee_active)) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='responsible employee must be active in asset department';
        END IF;
    END IF;
    IF NEW.location_id IS NOT NULL THEN
        SELECT company_id,is_active INTO location_company,location_active FROM masterdata_location WHERE id=NEW.location_id FOR SHARE;
        IF location_company<>NEW.company_id OR NOT location_active THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='asset location must be active in same company'; END IF;
    END IF;
    IF NEW.requested_coding_scheme_id IS NOT NULL THEN
        SELECT company_id,status,effective_from,effective_to INTO scheme_company,scheme_status,scheme_from,scheme_to
          FROM masterdata_assetcodingscheme WHERE id=NEW.requested_coding_scheme_id;
        shanghai_today := (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date;
        IF scheme_company IS NULL OR scheme_company<>NEW.company_id OR scheme_status<>'active'
           OR scheme_from>shanghai_today OR (scheme_to IS NOT NULL AND scheme_to<shanghai_today) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='requested coding scheme must be current and in the same company';
        END IF;
    END IF;
    IF TG_OP='UPDATE' AND NEW.category_id IS DISTINCT FROM OLD.category_id
       AND EXISTS (SELECT 1 FROM assets_assetcustomvalue value
                    JOIN assets_assetcustomfield field ON field.id=value.custom_field_id
                    WHERE value.asset_id=NEW.id AND field.category_id<>NEW.category_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='remove or migrate incompatible custom values before changing category';
    END IF;
    RETURN NEW;
END;
$$;

-- A returned historical loan may retain the now-leaving borrower.  An active
-- loan normally requires an active borrower; the sole exception is the exact
-- unresolved Loan captured by that leaving employee's active clearance.
CREATE OR REPLACE FUNCTION assets_sprint7_validate_loan_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a_company bigint; a_status varchar; m_company bigint; m_asset uuid; m_type varchar;
    emp_company bigint; emp_status varchar; emp_active boolean; current_loan_status varchar;
BEGIN
    SELECT status INTO current_loan_status FROM assets_assetloan WHERE id=NEW.id;
    IF current_loan_status IS DISTINCT FROM NEW.status THEN RETURN NULL; END IF;
    SELECT company_id,asset_status INTO a_company,a_status FROM assets_asset WHERE id=NEW.asset_id;
    IF a_company IS NULL OR a_company<>NEW.company_id THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='loan asset company mismatch'; END IF;
    SELECT company_id,asset_id,movement_type INTO m_company,m_asset,m_type FROM assets_assetmovement WHERE id=NEW.loan_movement_id;
    IF m_company<>NEW.company_id OR m_asset<>NEW.asset_id OR m_type<>'loan' THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='loan movement mismatch'; END IF;
    IF NEW.borrower_type='internal_employee' THEN
        SELECT company_id,employment_status,is_active INTO emp_company,emp_status,emp_active FROM masterdata_employee WHERE id=NEW.borrower_employee_id;
        IF emp_company<>NEW.company_id OR (
            NEW.status='active'
            AND (emp_status<>'active' OR NOT emp_active)
            -- A pre-existing active Loan is deliberately captured while the
            -- employee is changed to leaving in the same transaction.  It may
            -- remain active only while that exact Loan is unresolved evidence
            -- on the employee's active clearance; this does not permit a new
            -- Loan to be created for an already-leaving employee.
            AND NOT (
                emp_status='leaving' AND NOT emp_active AND EXISTS (
                    SELECT 1
                      FROM offboarding_employeeassetclearanceitem item
                      JOIN offboarding_employeeassetclearance clearance
                        ON clearance.id=item.clearance_id
                     WHERE item.source_loan_id=NEW.id
                       AND item.company_id=NEW.company_id
                       AND item.asset_id=NEW.asset_id
                       AND item.source_type IN ('internal_loan','both')
                       AND item.resolution IN ('pending','disposal_in_progress')
                       AND clearance.company_id=NEW.company_id
                       AND clearance.employee_id=NEW.borrower_employee_id
                       AND clearance.status IN ('open','blocked')
                )
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='active internal borrower must be active in same company';
        END IF;
    END IF;
    IF NEW.status='active' AND a_status<>'loaned' THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='active loan requires loaned asset'; END IF;
    IF NEW.status='returned' THEN
        SELECT company_id,asset_id,movement_type INTO m_company,m_asset,m_type FROM assets_assetmovement WHERE id=NEW.return_movement_id;
        IF m_company<>NEW.company_id OR m_asset<>NEW.asset_id OR m_type<>'loan_return' OR a_status<>NEW.return_asset_status THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='return movement or asset state mismatch'; END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION offboarding_block_disposal_reversal()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM offboarding_employeeassetclearanceitem
         WHERE company_id=NEW.company_id AND disposal_id=NEW.asset_disposal_id
           AND resolution='disposed'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='disposal used as disposed clearance evidence cannot be reversed';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_offboarding_employee_transition ON masterdata_employee;
CREATE TRIGGER trg_offboarding_employee_transition
BEFORE UPDATE OF employment_status,termination_date,is_active ON masterdata_employee
FOR EACH ROW EXECUTE FUNCTION offboarding_validate_employee_transition();
DROP TRIGGER IF EXISTS trg_offboarding_employee_commit ON masterdata_employee;
CREATE CONSTRAINT TRIGGER trg_offboarding_employee_commit
AFTER UPDATE OF employment_status,termination_date,is_active ON masterdata_employee
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION offboarding_validate_employee_commit();
DROP TRIGGER IF EXISTS trg_offboarding_clearance_write ON offboarding_employeeassetclearance;
CREATE TRIGGER trg_offboarding_clearance_write
BEFORE INSERT OR UPDATE OR DELETE ON offboarding_employeeassetclearance
FOR EACH ROW EXECUTE FUNCTION offboarding_validate_clearance_write();
DROP TRIGGER IF EXISTS trg_offboarding_item_write ON offboarding_employeeassetclearanceitem;
CREATE TRIGGER trg_offboarding_item_write
BEFORE INSERT OR UPDATE OR DELETE ON offboarding_employeeassetclearanceitem
FOR EACH ROW EXECUTE FUNCTION offboarding_validate_item_write();
DROP TRIGGER IF EXISTS trg_offboarding_clearance_commit ON offboarding_employeeassetclearance;
CREATE CONSTRAINT TRIGGER trg_offboarding_clearance_commit
AFTER INSERT OR UPDATE OR DELETE ON offboarding_employeeassetclearance
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION offboarding_validate_clearance_commit();
DROP TRIGGER IF EXISTS trg_offboarding_item_clearance_commit ON offboarding_employeeassetclearanceitem;
CREATE CONSTRAINT TRIGGER trg_offboarding_item_clearance_commit
AFTER INSERT OR UPDATE OR DELETE ON offboarding_employeeassetclearanceitem
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION offboarding_validate_clearance_commit();
DROP TRIGGER IF EXISTS trg_offboarding_item_evidence_commit ON offboarding_employeeassetclearanceitem;
CREATE CONSTRAINT TRIGGER trg_offboarding_item_evidence_commit
AFTER INSERT OR UPDATE ON offboarding_employeeassetclearanceitem
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION offboarding_validate_item_evidence_commit();
DROP TRIGGER IF EXISTS trg_offboarding_asset_evidence_commit ON assets_asset;
CREATE CONSTRAINT TRIGGER trg_offboarding_asset_evidence_commit
AFTER UPDATE ON assets_asset DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION offboarding_validate_item_evidence_commit();
DROP TRIGGER IF EXISTS trg_offboarding_loan_evidence_commit ON assets_assetloan;
CREATE CONSTRAINT TRIGGER trg_offboarding_loan_evidence_commit
AFTER INSERT OR UPDATE ON assets_assetloan DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION offboarding_validate_item_evidence_commit();
DROP TRIGGER IF EXISTS trg_00_offboarding_block_disposal_reversal ON assets_assetdisposalreversal;
CREATE TRIGGER trg_00_offboarding_block_disposal_reversal
BEFORE INSERT ON assets_assetdisposalreversal FOR EACH ROW
EXECUTE FUNCTION offboarding_block_disposal_reversal();
"""


OFFBOARDING_GUARDS_REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_00_offboarding_block_disposal_reversal ON assets_assetdisposalreversal;
DROP TRIGGER IF EXISTS trg_offboarding_loan_evidence_commit ON assets_assetloan;
DROP TRIGGER IF EXISTS trg_offboarding_asset_evidence_commit ON assets_asset;
DROP TRIGGER IF EXISTS trg_offboarding_item_evidence_commit ON offboarding_employeeassetclearanceitem;
DROP TRIGGER IF EXISTS trg_offboarding_item_clearance_commit ON offboarding_employeeassetclearanceitem;
DROP TRIGGER IF EXISTS trg_offboarding_clearance_commit ON offboarding_employeeassetclearance;
DROP TRIGGER IF EXISTS trg_offboarding_item_write ON offboarding_employeeassetclearanceitem;
DROP TRIGGER IF EXISTS trg_offboarding_clearance_write ON offboarding_employeeassetclearance;
DROP TRIGGER IF EXISTS trg_offboarding_employee_commit ON masterdata_employee;
DROP TRIGGER IF EXISTS trg_offboarding_employee_transition ON masterdata_employee;
DROP FUNCTION IF EXISTS offboarding_validate_clearance_commit();
DROP FUNCTION IF EXISTS offboarding_validate_item_evidence_commit();
DROP FUNCTION IF EXISTS offboarding_assert_clearance_item_evidence(uuid);
DROP FUNCTION IF EXISTS offboarding_block_disposal_reversal();
DROP FUNCTION IF EXISTS offboarding_validate_item_write();
DROP FUNCTION IF EXISTS offboarding_validate_clearance_write();
DROP FUNCTION IF EXISTS offboarding_validate_employee_commit();
DROP FUNCTION IF EXISTS offboarding_validate_employee_transition();
"""


PREVIOUS_LOAN_COMMIT_SQL = r"""
CREATE OR REPLACE FUNCTION assets_sprint7_validate_loan_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    a_company bigint; a_status varchar; m_company bigint; m_asset uuid; m_type varchar;
    emp_company bigint; emp_status varchar; emp_active boolean; current_loan_status varchar;
BEGIN
    SELECT status INTO current_loan_status FROM assets_assetloan WHERE id=NEW.id;
    IF current_loan_status IS DISTINCT FROM NEW.status THEN RETURN NULL; END IF;
    SELECT company_id,asset_status INTO a_company,a_status FROM assets_asset WHERE id=NEW.asset_id;
    IF a_company IS NULL OR a_company<>NEW.company_id THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='loan asset company mismatch'; END IF;
    SELECT company_id,asset_id,movement_type INTO m_company,m_asset,m_type FROM assets_assetmovement WHERE id=NEW.loan_movement_id;
    IF m_company<>NEW.company_id OR m_asset<>NEW.asset_id OR m_type<>'loan' THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='loan movement mismatch'; END IF;
    IF NEW.borrower_type='internal_employee' THEN
        SELECT company_id,employment_status,is_active INTO emp_company,emp_status,emp_active FROM masterdata_employee WHERE id=NEW.borrower_employee_id;
        IF emp_company<>NEW.company_id OR emp_status<>'active' OR NOT emp_active THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='internal borrower must be active in same company'; END IF;
    END IF;
    IF NEW.status='active' AND a_status<>'loaned' THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='active loan requires loaned asset'; END IF;
    IF NEW.status='returned' THEN
        SELECT company_id,asset_id,movement_type INTO m_company,m_asset,m_type FROM assets_assetmovement WHERE id=NEW.return_movement_id;
        IF m_company<>NEW.company_id OR m_asset<>NEW.asset_id OR m_type<>'loan_return' OR a_status<>NEW.return_asset_status THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='return movement or asset state mismatch'; END IF;
    END IF;
    RETURN NULL;
END;
$$;
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(OFFBOARDING_GUARDS_SQL)


def restore_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(OFFBOARDING_GUARDS_REVERSE_SQL)
    # Restore only the Sprint 7 loan commit validator replaced above.  Running
    # the whole old guard script here would incorrectly reinstall the pre-
    # Sprint-10 AttachmentLink trigger after assets.0012 has already reversed.
    schema_editor.execute(PREVIOUS_LOAN_COMMIT_SQL)
    import importlib

    previous = importlib.import_module(
        "apps.assets.migrations.0008_assetdisposal_assetdisposalreversal_assetloan_and_more"
    )
    asset_reference_start = previous.ASSET_GUARDS_SQL.index(
        "CREATE OR REPLACE FUNCTION assets_validate_asset_references()"
    )
    asset_reference_end = previous.ASSET_GUARDS_SQL.index(
        "\n$$;", asset_reference_start
    ) + len("\n$$;")
    schema_editor.execute(
        previous.ASSET_GUARDS_SQL[asset_reference_start:asset_reference_end]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0012_attachmentlink_clearance_and_more"),
        ("offboarding", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            install_postgresql_guards,
            reverse_code=restore_postgresql_guards,
        ),
    ]
