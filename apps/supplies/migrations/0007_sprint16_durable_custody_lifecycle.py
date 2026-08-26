import importlib

import django.db.models.deletion
from django.db import migrations, models


SPRINT16_GUARDS = r"""
CREATE OR REPLACE FUNCTION supplies_validate_line_refs_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ref_company bigint;
    ref_document uuid;
    ref_item uuid;
    ref_document_type varchar;
    ref_item_type varchar;
    custody_origin_issue uuid;
BEGIN
    SELECT company_id INTO ref_company FROM supplies_supplydocument WHERE id=NEW.document_id;
    IF ref_company IS NULL OR ref_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='supply line document belongs to another company';
    END IF;
    SELECT company_id, item_type INTO ref_company, ref_item_type
      FROM supplies_supplyitem WHERE id=NEW.item_id;
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
        SELECT company_id, item_id, origin_issue_line_id
          INTO ref_company, ref_item, custody_origin_issue
          FROM supplies_supplycustody WHERE id=NEW.source_custody_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='source custody is invalid';
        END IF;
        IF NEW.source_issue_line_id IS NOT NULL
           AND custody_origin_issue IS DISTINCT FROM NEW.source_issue_line_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='source issue line is not the direct custody origin';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION supplies_validate_line_state_s14()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    current_document_id uuid;
    current_item_id uuid;
    current_entered_cost numeric;
    current_posted_cost numeric;
    current_posted_amount numeric;
    current_direction varchar;
    current_source_issue uuid;
    current_source_custody uuid;
    document_status varchar;
    document_type varchar;
    item_type varchar;
BEGIN
    SELECT line.document_id, line.item_id, line.entered_unit_cost,
           line.posted_unit_cost, line.posted_amount, line.adjustment_direction,
           line.source_issue_line_id, line.source_custody_id
      INTO current_document_id, current_item_id, current_entered_cost,
           current_posted_cost, current_posted_amount, current_direction,
           current_source_issue, current_source_custody
      FROM supplies_supplydocumentline line WHERE line.id=NEW.id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT document.status, document.document_type INTO document_status, document_type
      FROM supplies_supplydocument document WHERE document.id=current_document_id;
    SELECT item.item_type INTO item_type FROM supplies_supplyitem item WHERE item.id=current_item_id;
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
        IF current_entered_cost IS NOT NULL OR current_direction IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='return cost must be system calculated';
        END IF;
        IF item_type='consumable' AND (current_source_issue IS NULL OR current_source_custody IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='consumable return source fields are invalid';
        ELSIF item_type='durable_quantity' AND current_source_custody IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='durable return requires source custody';
        ELSIF item_type NOT IN ('consumable','durable_quantity') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='return item type is invalid';
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

CREATE OR REPLACE FUNCTION supplies_validate_custody_s15()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ref_company bigint;
    ref_item uuid;
    ref_type varchar;
    ref_document_type varchar;
    ref_document_status varchar;
    ref_import_type varchar;
    ref_import_status varchar;
    ref_row_status varchar;
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
    IF NEW.parent_custody_id IS NULL THEN
        IF (NEW.origin_issue_line_id IS NULL) = (NEW.origin_import_row_id IS NULL) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='root custody requires exactly one root origin';
        END IF;
    ELSE
        IF NEW.parent_custody_id=NEW.id
           OR NEW.origin_issue_line_id IS NOT NULL OR NEW.origin_import_row_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='child custody source shape is invalid';
        END IF;
        SELECT company_id, item_id INTO ref_company, ref_item
          FROM supplies_supplycustody WHERE id=NEW.parent_custody_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='parent custody must use the same company and item';
        END IF;
    END IF;
    IF NEW.origin_issue_line_id IS NOT NULL THEN
        SELECT line.company_id, line.item_id, document.document_type, document.status
          INTO ref_company, ref_item, ref_document_type, ref_document_status
          FROM supplies_supplydocumentline line
          JOIN supplies_supplydocument document ON document.id=line.document_id
         WHERE line.id=NEW.origin_issue_line_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id
           OR ref_document_type<>'issue' OR ref_document_status NOT IN ('posted','reversed') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody origin issue line is invalid';
        END IF;
    END IF;
    IF NEW.origin_import_row_id IS NOT NULL THEN
        SELECT batch.company_id, batch.import_type, batch.status, row.validation_status
          INTO ref_company, ref_import_type, ref_import_status, ref_row_status
          FROM masterdata_importrow row
          JOIN masterdata_importbatch batch ON batch.id=row.batch_id
         WHERE row.id=NEW.origin_import_row_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id
           OR ref_import_type<>'opening_custody'
           OR ref_import_status<>'confirmed' OR ref_row_status<>'created' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody origin import row is invalid';
        END IF;
    END IF;
    SELECT company_id, is_active INTO ref_company, department_active
      FROM masterdata_department WHERE id=NEW.department_id;
    IF ref_company IS NULL OR ref_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody department belongs to another company';
    END IF;
    IF TG_OP='INSERT' AND NOT department_active THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='new custody department must be active';
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
    source_document_type varchar;
    source_document_status varchar;
    source_line_custody uuid;
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
    IF NEW.action IN ('issue','opening') THEN
        IF NEW.from_custody_id IS NOT NULL OR NEW.to_custody_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody opening direction is invalid';
        END IF;
    ELSIF NEW.action IN ('return','loss','scrap') THEN
        IF NEW.from_custody_id IS NULL OR NEW.to_custody_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody outgoing direction is invalid';
        END IF;
    ELSIF NEW.action='transfer' THEN
        IF NEW.from_custody_id IS NULL OR NEW.to_custody_id IS NULL
           OR NEW.from_custody_id=NEW.to_custody_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody transfer direction is invalid';
        END IF;
    ELSIF NEW.action='reversal' THEN
        IF NEW.reverses_movement_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody reversal requires original movement';
        END IF;
    ELSIF NEW.action<>'correction' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody movement action is invalid';
    END IF;
    IF NEW.action<>'reversal' AND NEW.reverses_movement_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='ordinary custody movement cannot reverse another movement';
    END IF;
    IF NEW.source_document_line_id IS NOT NULL THEN
        SELECT line.company_id, line.item_id, line.source_custody_id,
               document.document_type, document.status
          INTO ref_company, ref_item, source_line_custody,
               source_document_type, source_document_status
          FROM supplies_supplydocumentline line
          JOIN supplies_supplydocument document ON document.id=line.document_id
         WHERE line.id=NEW.source_document_line_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id
           OR source_document_status NOT IN ('posted','reversed') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody movement source line is invalid';
        END IF;
        IF NEW.action='return' AND (source_document_type<>'return' OR source_line_custody<>NEW.from_custody_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody return source line is invalid';
        END IF;
        IF NEW.action='issue' AND source_document_type<>'issue' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody issue source line is invalid';
        END IF;
    END IF;
    IF NEW.action='reversal' THEN
        SELECT company_id, item_id, action, from_custody_id, to_custody_id,
               quantity, amount, unit_cost
          INTO ref_company, ref_item, original_action, ref_from, ref_to,
               original_quantity, original_amount, original_cost
          FROM supplies_supplycustodymovement WHERE id=NEW.reverses_movement_id;
        IF ref_company IS NULL OR ref_company<>NEW.company_id OR ref_item<>NEW.item_id
           OR original_action='reversal'
           OR NEW.from_custody_id IS DISTINCT FROM ref_to
           OR NEW.to_custody_id IS DISTINCT FROM ref_from
           OR NEW.quantity<>original_quantity OR NEW.amount<>original_amount
           OR NEW.unit_cost<>original_cost THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='custody reversal movement is invalid';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
"""


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(SPRINT16_GUARDS)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        previous = importlib.import_module(
            "apps.supplies.migrations.0006_sprint15_postgresql_guards"
        )
        schema_editor.execute(previous.CREATE_GUARDS)


class Migration(migrations.Migration):
    dependencies = [
        ("masterdata", "0012_sprint16_opening_custody_import"),
        ("supplies", "0006_sprint15_postgresql_guards"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="supplycustodymovement",
            name="ck_supply_custody_movement_shape",
        ),
        migrations.RemoveConstraint(
            model_name="supplycustodymovement",
            name="ck_supply_custody_movement_reversal",
        ),
        migrations.AlterField(
            model_name="supplycustody",
            name="origin_issue_line",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="supply_custody",
                to="supplies.supplydocumentline",
                verbose_name="来源领用明细",
            ),
        ),
        migrations.AddField(
            model_name="supplycustody",
            name="origin_import_row",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="supply_custody",
                to="masterdata.importrow",
                verbose_name="来源期初保管导入行",
            ),
        ),
        migrations.AddField(
            model_name="supplycustody",
            name="parent_custody",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="child_custodies",
                to="supplies.supplycustody",
                verbose_name="来源父保管",
            ),
        ),
        migrations.AddField(
            model_name="supplycustodymovement",
            name="idempotency_key",
            field=models.CharField(
                blank=True,
                max_length=128,
                null=True,
                verbose_name="动作幂等键",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustody",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        parent_custody__isnull=True,
                        origin_issue_line__isnull=False,
                        origin_import_row__isnull=True,
                    )
                    | models.Q(
                        parent_custody__isnull=True,
                        origin_issue_line__isnull=True,
                        origin_import_row__isnull=False,
                    )
                    | models.Q(
                        parent_custody__isnull=False,
                        origin_issue_line__isnull=True,
                        origin_import_row__isnull=True,
                    )
                ),
                name="ck_supply_custody_source_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustody",
            constraint=models.CheckConstraint(
                condition=~models.Q(id=models.F("parent_custody")),
                name="ck_supply_custody_parent_not_self",
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
                    | models.Q(action="correction")
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
                                & ~models.Q(
                                    from_custody=models.F("to_custody")
                                )
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
                condition=(
                    models.Q(action="reversal", reverses_movement__isnull=False)
                    | (
                        ~models.Q(action="reversal")
                        & models.Q(reverses_movement__isnull=True)
                    )
                ),
                name="ck_supply_custody_movement_reversal",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.UniqueConstraint(
                condition=models.Q(idempotency_key__isnull=False),
                fields=("company", "idempotency_key"),
                name="uq_supply_custody_move_company_idem",
            ),
        ),
        migrations.RunPython(install, uninstall),
    ]
