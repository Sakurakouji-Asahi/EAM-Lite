from django.db import migrations


CREATE_GUARDS = r"""
ALTER TABLE supplies_supplyitem
    ADD CONSTRAINT ck_supply_item_numeric_finite
    CHECK (
        minimum_stock_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')
    );

ALTER TABLE supplies_supplydocumentline
    ADD CONSTRAINT ck_supply_line_numeric_finite
    CHECK (
        quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND (entered_unit_cost IS NULL OR entered_unit_cost::text NOT IN ('NaN', 'Infinity', '-Infinity'))
        AND (posted_unit_cost IS NULL OR posted_unit_cost::text NOT IN ('NaN', 'Infinity', '-Infinity'))
        AND (posted_amount IS NULL OR posted_amount::text NOT IN ('NaN', 'Infinity', '-Infinity'))
    );

ALTER TABLE supplies_supplystockbalance
    ADD CONSTRAINT ck_supply_balance_numeric_finite
    CHECK (
        quantity_on_hand::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND amount_on_hand::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND average_unit_cost::text NOT IN ('NaN', 'Infinity', '-Infinity')
    );

ALTER TABLE supplies_supplystockbalance
    ADD CONSTRAINT ck_supply_balance_average_reconciles
    CHECK (
        (
            quantity_on_hand = 0
            AND amount_on_hand = 0
            AND average_unit_cost = 0
        )
        OR (
            quantity_on_hand > 0
            AND average_unit_cost = round(amount_on_hand / quantity_on_hand, 6)
        )
    );

ALTER TABLE supplies_supplystockledger
    ADD CONSTRAINT ck_supply_ledger_numeric_finite
    CHECK (
        quantity_delta::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND amount_delta::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND unit_cost::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND quantity_before::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND quantity_after::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND amount_before::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND amount_after::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND average_unit_cost_before::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND average_unit_cost_after::text NOT IN ('NaN', 'Infinity', '-Infinity')
    );

ALTER TABLE supplies_supplycustody
    ADD CONSTRAINT ck_supply_custody_numeric_finite
    CHECK (
        current_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND current_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND unit_cost_snapshot::text NOT IN ('NaN', 'Infinity', '-Infinity')
    );

ALTER TABLE supplies_supplycustodymovement
    ADD CONSTRAINT ck_supply_custody_move_numeric_finite
    CHECK (
        quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND amount::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND unit_cost::text NOT IN ('NaN', 'Infinity', '-Infinity')
    );

ALTER TABLE supplies_supplycountline
    ADD CONSTRAINT ck_supply_count_line_numeric_finite
    CHECK (
        expected_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND expected_amount::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND expected_unit_cost::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND (counted_quantity IS NULL OR counted_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity'))
        AND (difference_quantity IS NULL OR difference_quantity::text NOT IN ('NaN', 'Infinity', '-Infinity'))
        AND (adjustment_unit_cost IS NULL OR adjustment_unit_cost::text NOT IN ('NaN', 'Infinity', '-Infinity'))
    );

ALTER TABLE supplies_employeesupplyclearanceitem
    ADD CONSTRAINT ck_supply_clearance_item_numeric_finite
    CHECK (
        quantity_snapshot::text NOT IN ('NaN', 'Infinity', '-Infinity')
        AND amount_snapshot::text NOT IN ('NaN', 'Infinity', '-Infinity')
    );

CREATE OR REPLACE FUNCTION supplies_guard_item_identity_i10()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (
        NEW.item_code IS DISTINCT FROM OLD.item_code
        OR NEW.normalized_item_code IS DISTINCT FROM OLD.normalized_item_code
        OR NEW.item_type IS DISTINCT FROM OLD.item_type
    ) AND (
        EXISTS (
            SELECT 1
              FROM supplies_supplystockledger ledger
             WHERE ledger.item_id = NEW.id
        )
        OR EXISTS (
            SELECT 1
              FROM supplies_supplycustodymovement movement
             WHERE movement.item_id = NEW.id
        )
        OR EXISTS (
            SELECT 1
              FROM supplies_supplydocumentline line
              JOIN supplies_supplydocument document
                ON document.id = line.document_id
             WHERE line.item_id = NEW.id
               AND document.status IN ('posted', 'reversed')
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'posted supply item code and management mode are immutable';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_assert_stock_ledger_i10(
    expected_ledger_id uuid,
    expected_company_id bigint,
    expected_warehouse_id uuid,
    expected_item_id uuid,
    expected_document_id uuid,
    expected_document_line_id uuid,
    expected_movement_type varchar,
    expected_quantity_delta numeric,
    expected_amount_delta numeric,
    expected_unit_cost numeric,
    expected_occurred_at timestamptz,
    expected_reverses_ledger_id uuid
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    ledger_row record;
    expected_average_before numeric;
    expected_average_after numeric;
BEGIN
    SELECT *
      INTO ledger_row
      FROM supplies_supplystockledger
     WHERE id = expected_ledger_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'required supply stock ledger is missing';
    END IF;

    expected_average_before := CASE
        WHEN ledger_row.quantity_before = 0 THEN 0::numeric
        ELSE round(ledger_row.amount_before / ledger_row.quantity_before, 6)
    END;
    expected_average_after := CASE
        WHEN ledger_row.quantity_after = 0 THEN 0::numeric
        ELSE round(ledger_row.amount_after / ledger_row.quantity_after, 6)
    END;

    IF ledger_row.company_id IS DISTINCT FROM expected_company_id
       OR ledger_row.warehouse_id IS DISTINCT FROM expected_warehouse_id
       OR ledger_row.item_id IS DISTINCT FROM expected_item_id
       OR ledger_row.document_id IS DISTINCT FROM expected_document_id
       OR ledger_row.document_line_id IS DISTINCT FROM expected_document_line_id
       OR ledger_row.movement_type IS DISTINCT FROM expected_movement_type
       OR ledger_row.quantity_delta IS DISTINCT FROM expected_quantity_delta
       OR ledger_row.amount_delta IS DISTINCT FROM expected_amount_delta
       OR ledger_row.unit_cost IS DISTINCT FROM expected_unit_cost
       OR ledger_row.occurred_at IS DISTINCT FROM expected_occurred_at
       OR ledger_row.reverses_ledger_id IS DISTINCT FROM expected_reverses_ledger_id
       OR ledger_row.quantity_after IS DISTINCT FROM (
            ledger_row.quantity_before + ledger_row.quantity_delta
       )
       OR ledger_row.amount_after IS DISTINCT FROM (
            ledger_row.amount_before + ledger_row.amount_delta
       )
       OR ledger_row.average_unit_cost_before IS DISTINCT FROM expected_average_before
       OR ledger_row.average_unit_cost_after IS DISTINCT FROM expected_average_after THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'supply document line and stock ledger do not reconcile';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_assert_document_line_i10(
    checked_line_id uuid
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    line_row record;
    original_line record;
    reversal_pair record;
    ledger_id uuid;
    source_ledger_id uuid;
    target_ledger_id uuid;
    expected_warehouse_id uuid;
    ledger_count bigint;
    original_ledger_count bigint;
BEGIN
    SELECT
        line.*,
        document.company_id AS document_company_id,
        document.document_type,
        document.status AS document_status,
        document.source_warehouse_id,
        document.target_warehouse_id,
        document.source_count_task_id,
        document.reversal_of_id,
        document.posted_at
      INTO line_row
      FROM supplies_supplydocumentline line
      JOIN supplies_supplydocument document
        ON document.id = line.document_id
     WHERE line.id = checked_line_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF line_row.document_type <> 'reversal'
       AND line_row.document_status NOT IN ('posted', 'reversed') THEN
        RETURN;
    END IF;
    IF line_row.document_type = 'reversal'
       AND line_row.document_status <> 'posted' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'a reversal supply document must be posted atomically';
    END IF;
    IF line_row.company_id IS DISTINCT FROM line_row.document_company_id
       OR line_row.posted_unit_cost IS NULL
       OR line_row.posted_amount IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'posted supply document line requires a same-company cost snapshot';
    END IF;

    SELECT count(*)
      INTO ledger_count
      FROM supplies_supplystockledger ledger
     WHERE ledger.document_line_id = checked_line_id;

    IF line_row.document_type IN ('opening', 'receipt') THEN
        IF ledger_count <> 1
           OR line_row.entered_unit_cost IS NULL
           OR line_row.entered_unit_cost IS DISTINCT FROM line_row.posted_unit_cost
           OR round(line_row.quantity * line_row.posted_unit_cost, 2)
                IS DISTINCT FROM line_row.posted_amount THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'posted receipt line must have one exact inbound ledger';
        END IF;
        SELECT id INTO ledger_id
          FROM supplies_supplystockledger
         WHERE document_line_id = checked_line_id;
        PERFORM supplies_assert_stock_ledger_i10(
            ledger_id,
            line_row.company_id,
            line_row.target_warehouse_id,
            line_row.item_id,
            line_row.document_id,
            line_row.id,
            CASE
                WHEN line_row.document_type = 'opening' THEN 'opening_in'
                ELSE 'receipt_in'
            END,
            line_row.quantity,
            line_row.posted_amount,
            line_row.posted_unit_cost,
            line_row.posted_at,
            NULL
        );
    ELSIF line_row.document_type = 'issue' THEN
        IF ledger_count <> 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'posted issue line must have one exact outbound ledger';
        END IF;
        SELECT id INTO ledger_id
          FROM supplies_supplystockledger
         WHERE document_line_id = checked_line_id;
        PERFORM supplies_assert_stock_ledger_i10(
            ledger_id,
            line_row.company_id,
            line_row.source_warehouse_id,
            line_row.item_id,
            line_row.document_id,
            line_row.id,
            'issue_out',
            -line_row.quantity,
            -line_row.posted_amount,
            line_row.posted_unit_cost,
            line_row.posted_at,
            NULL
        );
    ELSIF line_row.document_type = 'return' THEN
        IF ledger_count <> 1 THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'posted return line must have one exact inbound ledger';
        END IF;
        SELECT id INTO ledger_id
          FROM supplies_supplystockledger
         WHERE document_line_id = checked_line_id;
        PERFORM supplies_assert_stock_ledger_i10(
            ledger_id,
            line_row.company_id,
            line_row.target_warehouse_id,
            line_row.item_id,
            line_row.document_id,
            line_row.id,
            'return_in',
            line_row.quantity,
            line_row.posted_amount,
            line_row.posted_unit_cost,
            line_row.posted_at,
            NULL
        );
    ELSIF line_row.document_type = 'transfer' THEN
        SELECT id INTO source_ledger_id
          FROM supplies_supplystockledger
         WHERE document_line_id = checked_line_id
           AND warehouse_id = line_row.source_warehouse_id
           AND movement_type = 'transfer_out';
        SELECT id INTO target_ledger_id
          FROM supplies_supplystockledger
         WHERE document_line_id = checked_line_id
           AND warehouse_id = line_row.target_warehouse_id
           AND movement_type = 'transfer_in';
        IF ledger_count <> 2
           OR source_ledger_id IS NULL
           OR target_ledger_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'posted transfer line requires one equal outbound and inbound leg';
        END IF;
        PERFORM supplies_assert_stock_ledger_i10(
            source_ledger_id,
            line_row.company_id,
            line_row.source_warehouse_id,
            line_row.item_id,
            line_row.document_id,
            line_row.id,
            'transfer_out',
            -line_row.quantity,
            -line_row.posted_amount,
            line_row.posted_unit_cost,
            line_row.posted_at,
            NULL
        );
        PERFORM supplies_assert_stock_ledger_i10(
            target_ledger_id,
            line_row.company_id,
            line_row.target_warehouse_id,
            line_row.item_id,
            line_row.document_id,
            line_row.id,
            'transfer_in',
            line_row.quantity,
            line_row.posted_amount,
            line_row.posted_unit_cost,
            line_row.posted_at,
            NULL
        );
    ELSIF line_row.document_type = 'count_adjustment' THEN
        SELECT warehouse_id
          INTO expected_warehouse_id
          FROM supplies_supplycounttask
         WHERE id = line_row.source_count_task_id;
        IF ledger_count <> 1 OR expected_warehouse_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'posted count adjustment line requires one count-warehouse ledger';
        END IF;
        SELECT id INTO ledger_id
          FROM supplies_supplystockledger
         WHERE document_line_id = checked_line_id;
        IF line_row.adjustment_direction = 'increase' THEN
            IF line_row.entered_unit_cost IS NULL
               OR line_row.entered_unit_cost IS DISTINCT FROM line_row.posted_unit_cost
               OR round(line_row.quantity * line_row.posted_unit_cost, 2)
                    IS DISTINCT FROM line_row.posted_amount THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'count gain cost snapshot is inconsistent';
            END IF;
            PERFORM supplies_assert_stock_ledger_i10(
                ledger_id,
                line_row.company_id,
                expected_warehouse_id,
                line_row.item_id,
                line_row.document_id,
                line_row.id,
                'count_gain',
                line_row.quantity,
                line_row.posted_amount,
                line_row.posted_unit_cost,
                line_row.posted_at,
                NULL
            );
        ELSIF line_row.adjustment_direction = 'decrease' THEN
            PERFORM supplies_assert_stock_ledger_i10(
                ledger_id,
                line_row.company_id,
                expected_warehouse_id,
                line_row.item_id,
                line_row.document_id,
                line_row.id,
                'count_loss',
                -line_row.quantity,
                -line_row.posted_amount,
                line_row.posted_unit_cost,
                line_row.posted_at,
                NULL
            );
        ELSE
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'count adjustment direction is invalid';
        END IF;
    ELSIF line_row.document_type = 'reversal' THEN
        SELECT original.*
          INTO original_line
          FROM supplies_supplydocumentline original
         WHERE original.document_id = line_row.reversal_of_id
           AND original.line_no = line_row.line_no;
        IF NOT FOUND
           OR line_row.item_id IS DISTINCT FROM original_line.item_id
           OR line_row.quantity IS DISTINCT FROM original_line.quantity
           OR line_row.posted_unit_cost IS DISTINCT FROM original_line.posted_unit_cost
           OR line_row.posted_amount IS DISTINCT FROM original_line.posted_amount
           OR line_row.source_issue_line_id IS DISTINCT FROM original_line.source_issue_line_id
           OR line_row.source_custody_id IS DISTINCT FROM original_line.source_custody_id THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'reversal line does not match its original line snapshot';
        END IF;

        SELECT count(*)
          INTO original_ledger_count
          FROM supplies_supplystockledger original
         WHERE original.document_line_id = original_line.id;
        IF original_ledger_count = 0 OR ledger_count <> original_ledger_count THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'reversal line does not reverse every original ledger leg';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM supplies_supplystockledger reversal
              LEFT JOIN supplies_supplystockledger original
                ON original.id = reversal.reverses_ledger_id
             WHERE reversal.document_line_id = checked_line_id
               AND (
                    original.id IS NULL
                    OR original.document_line_id <> original_line.id
                    OR original.document_id <> line_row.reversal_of_id
               )
        ) OR EXISTS (
            SELECT 1
              FROM supplies_supplystockledger original
             WHERE original.document_line_id = original_line.id
               AND NOT EXISTS (
                    SELECT 1
                      FROM supplies_supplystockledger reversal
                     WHERE reversal.document_line_id = checked_line_id
                       AND reversal.reverses_ledger_id = original.id
               )
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'reversal ledger chain is incomplete or points outside the original line';
        END IF;

        FOR reversal_pair IN
            SELECT
                reversal.id AS reversal_id,
                original.id AS original_id,
                original.warehouse_id AS original_warehouse_id,
                original.quantity_delta AS original_quantity_delta,
                original.amount_delta AS original_amount_delta,
                original.unit_cost AS original_unit_cost,
                original.quantity_before AS original_quantity_before,
                original.quantity_after AS original_quantity_after,
                original.amount_before AS original_amount_before,
                original.amount_after AS original_amount_after,
                original.average_unit_cost_before AS original_average_before,
                original.average_unit_cost_after AS original_average_after,
                reversal.quantity_before AS reversal_quantity_before,
                reversal.quantity_after AS reversal_quantity_after,
                reversal.amount_before AS reversal_amount_before,
                reversal.amount_after AS reversal_amount_after,
                reversal.average_unit_cost_before AS reversal_average_before,
                reversal.average_unit_cost_after AS reversal_average_after
              FROM supplies_supplystockledger reversal
              JOIN supplies_supplystockledger original
                ON original.id = reversal.reverses_ledger_id
             WHERE reversal.document_line_id = checked_line_id
        LOOP
            PERFORM supplies_assert_stock_ledger_i10(
                reversal_pair.reversal_id,
                line_row.company_id,
                reversal_pair.original_warehouse_id,
                line_row.item_id,
                line_row.document_id,
                line_row.id,
                'reversal',
                -reversal_pair.original_quantity_delta,
                -reversal_pair.original_amount_delta,
                reversal_pair.original_unit_cost,
                line_row.posted_at,
                reversal_pair.original_id
            );
            IF reversal_pair.reversal_quantity_before
                    IS DISTINCT FROM reversal_pair.original_quantity_after
               OR reversal_pair.reversal_quantity_after
                    IS DISTINCT FROM reversal_pair.original_quantity_before
               OR reversal_pair.reversal_amount_before
                    IS DISTINCT FROM reversal_pair.original_amount_after
               OR reversal_pair.reversal_amount_after
                    IS DISTINCT FROM reversal_pair.original_amount_before
               OR reversal_pair.reversal_average_before
                    IS DISTINCT FROM reversal_pair.original_average_after
               OR reversal_pair.reversal_average_after
                    IS DISTINCT FROM reversal_pair.original_average_before THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'reversal ledger does not restore the original before snapshot';
            END IF;
        END LOOP;
    ELSE
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'posted supply document type has no accounting invariant';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_assert_document_i10(
    checked_document_id uuid
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    document_row record;
    original_document record;
    checked_line_id uuid;
    reversal_document_id uuid;
    line_count bigint;
    original_line_count bigint;
    ledger_count bigint;
    original_ledger_count bigint;
BEGIN
    SELECT *
      INTO document_row
      FROM supplies_supplydocument
     WHERE id = checked_document_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF document_row.document_type <> 'reversal'
       AND document_row.status NOT IN ('posted', 'reversed') THEN
        RETURN;
    END IF;
    IF document_row.document_type = 'reversal'
       AND document_row.status <> 'posted' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'a reversal supply document must be posted atomically';
    END IF;

    SELECT count(*)
      INTO line_count
      FROM supplies_supplydocumentline line
     WHERE line.document_id = checked_document_id;
    IF line_count = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'posted supply document requires at least one line';
    END IF;

    FOR checked_line_id IN
        SELECT id
          FROM supplies_supplydocumentline
         WHERE document_id = checked_document_id
         ORDER BY line_no, id
    LOOP
        PERFORM supplies_assert_document_line_i10(checked_line_id);
    END LOOP;

    IF document_row.document_type = 'reversal' THEN
        SELECT *
          INTO original_document
          FROM supplies_supplydocument
         WHERE id = document_row.reversal_of_id;
        IF NOT FOUND
           OR original_document.company_id IS DISTINCT FROM document_row.company_id
           OR original_document.document_type IN ('reversal', 'count_adjustment')
           OR original_document.status <> 'reversed'
           OR original_document.reversed_at IS DISTINCT FROM document_row.posted_at
           OR original_document.source_warehouse_id IS DISTINCT FROM document_row.source_warehouse_id
           OR original_document.target_warehouse_id IS DISTINCT FROM document_row.target_warehouse_id
           OR original_document.department_id IS DISTINCT FROM document_row.department_id
           OR original_document.employee_id IS DISTINCT FROM document_row.employee_id
           OR btrim(document_row.remark) = '' THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'reversal document does not form a complete audit chain';
        END IF;

        SELECT count(*)
          INTO original_line_count
          FROM supplies_supplydocumentline
         WHERE document_id = original_document.id;
        SELECT count(*)
          INTO ledger_count
          FROM supplies_supplystockledger
         WHERE document_id = checked_document_id;
        SELECT count(*)
          INTO original_ledger_count
          FROM supplies_supplystockledger
         WHERE document_id = original_document.id;
        IF line_count <> original_line_count
           OR ledger_count = 0
           OR ledger_count <> original_ledger_count
           OR EXISTS (
                SELECT 1
                  FROM supplies_supplydocumentline original_line
                 WHERE original_line.document_id = original_document.id
                   AND NOT EXISTS (
                        SELECT 1
                          FROM supplies_supplydocumentline reversal_line
                         WHERE reversal_line.document_id = checked_document_id
                           AND reversal_line.line_no = original_line.line_no
                   )
           )
           OR EXISTS (
                SELECT 1
                  FROM supplies_supplystockledger original_ledger
                 WHERE original_ledger.document_id = original_document.id
                   AND NOT EXISTS (
                        SELECT 1
                          FROM supplies_supplystockledger reversal_ledger
                         WHERE reversal_ledger.document_id = checked_document_id
                           AND reversal_ledger.reverses_ledger_id = original_ledger.id
                   )
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'reversal document does not fully reverse all original lines and ledgers';
        END IF;
    ELSIF document_row.status = 'reversed' THEN
        SELECT id
          INTO reversal_document_id
          FROM supplies_supplydocument
         WHERE reversal_of_id = checked_document_id;
        IF reversal_document_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'reversed supply document is missing its reversal document';
        END IF;
        PERFORM supplies_assert_document_i10(reversal_document_id);
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_document_i10()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM supplies_assert_document_i10(NEW.id);
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_document_line_i10()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM supplies_assert_document_i10(OLD.document_id);
    ELSE
        IF TG_OP = 'UPDATE'
           AND OLD.document_id IS DISTINCT FROM NEW.document_id THEN
            PERFORM supplies_assert_document_i10(OLD.document_id);
        END IF;
        PERFORM supplies_assert_document_line_i10(NEW.id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_stock_ledger_i10()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM supplies_assert_document_i10(OLD.document_id);
    ELSE
        IF TG_OP = 'UPDATE'
           AND (
                OLD.document_id IS DISTINCT FROM NEW.document_id
                OR OLD.document_line_id IS DISTINCT FROM NEW.document_line_id
           ) THEN
            PERFORM supplies_assert_document_i10(OLD.document_id);
        END IF;
        PERFORM supplies_assert_document_line_i10(NEW.document_line_id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_supply_item_identity_i10
AFTER UPDATE OF item_code, normalized_item_code, item_type
ON supplies_supplyitem
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION supplies_guard_item_identity_i10();

CREATE CONSTRAINT TRIGGER trg_supply_document_accounting_i10
AFTER INSERT OR UPDATE
ON supplies_supplydocument
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION supplies_validate_document_i10();

CREATE CONSTRAINT TRIGGER trg_supply_line_accounting_i10
AFTER INSERT OR UPDATE OR DELETE
ON supplies_supplydocumentline
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION supplies_validate_document_line_i10();

CREATE CONSTRAINT TRIGGER trg_supply_ledger_accounting_i10
AFTER INSERT OR UPDATE OR DELETE
ON supplies_supplystockledger
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION supplies_validate_stock_ledger_i10();

DO $$
DECLARE
    existing_document_id uuid;
BEGIN
    FOR existing_document_id IN
        SELECT id
          FROM supplies_supplydocument
         WHERE status IN ('posted', 'reversed')
            OR document_type = 'reversal'
         ORDER BY id
    LOOP
        PERFORM supplies_assert_document_i10(existing_document_id);
    END LOOP;
END;
$$;
"""


DROP_GUARDS = r"""
DROP TRIGGER IF EXISTS trg_supply_ledger_accounting_i10 ON supplies_supplystockledger;
DROP TRIGGER IF EXISTS trg_supply_line_accounting_i10 ON supplies_supplydocumentline;
DROP TRIGGER IF EXISTS trg_supply_document_accounting_i10 ON supplies_supplydocument;
DROP TRIGGER IF EXISTS trg_supply_item_identity_i10 ON supplies_supplyitem;

DROP FUNCTION IF EXISTS supplies_validate_stock_ledger_i10();
DROP FUNCTION IF EXISTS supplies_validate_document_line_i10();
DROP FUNCTION IF EXISTS supplies_validate_document_i10();
DROP FUNCTION IF EXISTS supplies_assert_document_i10(uuid);
DROP FUNCTION IF EXISTS supplies_assert_document_line_i10(uuid);
DROP FUNCTION IF EXISTS supplies_assert_stock_ledger_i10(
    uuid, bigint, uuid, uuid, uuid, uuid, varchar,
    numeric, numeric, numeric, timestamptz, uuid
);
DROP FUNCTION IF EXISTS supplies_guard_item_identity_i10();

ALTER TABLE supplies_employeesupplyclearanceitem
    DROP CONSTRAINT IF EXISTS ck_supply_clearance_item_numeric_finite;
ALTER TABLE supplies_supplycountline
    DROP CONSTRAINT IF EXISTS ck_supply_count_line_numeric_finite;
ALTER TABLE supplies_supplycustodymovement
    DROP CONSTRAINT IF EXISTS ck_supply_custody_move_numeric_finite;
ALTER TABLE supplies_supplycustody
    DROP CONSTRAINT IF EXISTS ck_supply_custody_numeric_finite;
ALTER TABLE supplies_supplystockledger
    DROP CONSTRAINT IF EXISTS ck_supply_ledger_numeric_finite;
ALTER TABLE supplies_supplystockbalance
    DROP CONSTRAINT IF EXISTS ck_supply_balance_average_reconciles;
ALTER TABLE supplies_supplystockbalance
    DROP CONSTRAINT IF EXISTS ck_supply_balance_numeric_finite;
ALTER TABLE supplies_supplydocumentline
    DROP CONSTRAINT IF EXISTS ck_supply_line_numeric_finite;
ALTER TABLE supplies_supplyitem
    DROP CONSTRAINT IF EXISTS ck_supply_item_numeric_finite;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(CREATE_GUARDS)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.connection.connection.execute(DROP_GUARDS)


class Migration(migrations.Migration):
    dependencies = [("supplies", "0009_sprint18_reporting_index")]

    operations = [migrations.RunPython(install, uninstall)]
