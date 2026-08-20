from django.db import migrations, models
from django.db.models import F, Q


PROFILE_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION finance_validate_profile()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    asset_company bigint; policy_company bigint; policy_status varchar;
    policy_from date; policy_to date; controlled_status boolean; actor_cleared boolean;
    disposal_restore_evidence boolean := false;
    continuation_reviewed boolean := false;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id=NEW.asset_id FOR SHARE;
    SELECT company_id,status,effective_from,effective_to
      INTO policy_company,policy_status,policy_from,policy_to
      FROM finance_depreciationpolicy WHERE id=NEW.depreciation_policy_id FOR SHARE;
    IF asset_company IS NULL OR asset_company<>NEW.company_id
       OR policy_company IS NULL OR policy_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='depreciation profile references must be in one company';
    END IF;
    IF TG_OP='INSERT' AND NEW.status<>'draft'
       AND (policy_status<>'active' OR policy_from>NEW.effective_from
            OR (policy_to IS NOT NULL AND policy_to<NEW.effective_from)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='confirmed profile requires an effective policy version';
    END IF;
    IF TG_OP='UPDATE' THEN
        actor_cleared := OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL;
        continuation_reviewed := OLD.actual_continuation_date IS NULL
            AND OLD.actual_continuation_review_required
            AND NEW.actual_continuation_date IS NOT NULL
            AND NOT NEW.actual_continuation_review_required
            AND (to_jsonb(NEW)-ARRAY[
                    'actual_continuation_date','actual_continuation_review_required'
                ]) IS NOT DISTINCT FROM (to_jsonb(OLD)-ARRAY[
                    'actual_continuation_date','actual_continuation_review_required'
                ]);
        IF NEW.created_by_id IS DISTINCT FROM OLD.created_by_id AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='depreciation profile creator is immutable';
        END IF;
        IF OLD.status<>'draft'
           AND NOT continuation_reviewed
           AND (to_jsonb(NEW)-ARRAY['status','effective_to','created_by_id'])
               IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','effective_to','created_by_id']) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='confirmed depreciation profile parameters are immutable';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           AND NOT (OLD.status='draft' AND NEW.status='active') THEN
            controlled_status := COALESCE(current_setting('eam_lite.controlled_finance_profile_status',true),'')='on';
            IF OLD.status='stopped' AND NEW.status IN ('active','suspended') THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM finance_depreciationprofileevent restore_event
                      JOIN finance_depreciationprofileevent stop_event
                        ON stop_event.id=restore_event.reverses_event_id
                     WHERE restore_event.id=(
                               SELECT latest.id
                                 FROM finance_depreciationprofileevent latest
                                WHERE latest.depreciation_profile_id=NEW.id
                                ORDER BY latest.created_at DESC,latest.id DESC
                                LIMIT 1
                           )
                       AND restore_event.depreciation_profile_id=NEW.id
                       AND restore_event.company_id=NEW.company_id
                       AND restore_event.asset_id=NEW.asset_id
                       AND restore_event.event_type='disposal_restore'
                       AND stop_event.event_type='disposal_stop'
                       AND stop_event.depreciation_profile_id=NEW.id
                       AND stop_event.company_id=NEW.company_id
                       AND stop_event.asset_id=NEW.asset_id
                       AND stop_event.source_disposal_id=restore_event.source_disposal_id
                       AND stop_event.effective_date=restore_event.effective_date
                       AND stop_event.previous_profile_status=NEW.status
                ) INTO disposal_restore_evidence;
            END IF;
            IF NOT controlled_status OR NOT (
                (OLD.status='active' AND NEW.status IN ('suspended','stopped','completed'))
                OR (OLD.status='suspended' AND NEW.status IN ('active','stopped','completed'))
                OR (OLD.status='stopped' AND NEW.status IN ('active','suspended')
                    AND disposal_restore_evidence)
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid or uncontrolled depreciation profile status transition';
            END IF;
            PERFORM set_config('eam_lite.controlled_finance_profile_status','off',true);
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""


PROFILE_GUARD_REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION finance_validate_profile()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    asset_company bigint; policy_company bigint; policy_status varchar;
    policy_from date; policy_to date; controlled_status boolean; actor_cleared boolean;
    disposal_restore_evidence boolean := false;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id=NEW.asset_id FOR SHARE;
    SELECT company_id,status,effective_from,effective_to
      INTO policy_company,policy_status,policy_from,policy_to
      FROM finance_depreciationpolicy WHERE id=NEW.depreciation_policy_id FOR SHARE;
    IF asset_company IS NULL OR asset_company<>NEW.company_id
       OR policy_company IS NULL OR policy_company<>NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='depreciation profile references must be in one company';
    END IF;
    IF TG_OP='INSERT' AND NEW.status<>'draft'
       AND (policy_status<>'active' OR policy_from>NEW.effective_from
            OR (policy_to IS NOT NULL AND policy_to<NEW.effective_from)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='confirmed profile requires an effective policy version';
    END IF;
    IF TG_OP='UPDATE' THEN
        actor_cleared := OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL;
        IF NEW.created_by_id IS DISTINCT FROM OLD.created_by_id AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='depreciation profile creator is immutable';
        END IF;
        IF OLD.status<>'draft'
           AND (to_jsonb(NEW)-ARRAY['status','effective_to','created_by_id'])
               IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','effective_to','created_by_id']) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='confirmed depreciation profile parameters are immutable';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           AND NOT (OLD.status='draft' AND NEW.status='active') THEN
            controlled_status := COALESCE(current_setting('eam_lite.controlled_finance_profile_status',true),'')='on';
            IF OLD.status='stopped' AND NEW.status IN ('active','suspended') THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM finance_depreciationprofileevent restore_event
                      JOIN finance_depreciationprofileevent stop_event
                        ON stop_event.id=restore_event.reverses_event_id
                     WHERE restore_event.id=(
                               SELECT latest.id
                                 FROM finance_depreciationprofileevent latest
                                WHERE latest.depreciation_profile_id=NEW.id
                                ORDER BY latest.created_at DESC,latest.id DESC
                                LIMIT 1
                           )
                       AND restore_event.depreciation_profile_id=NEW.id
                       AND restore_event.company_id=NEW.company_id
                       AND restore_event.asset_id=NEW.asset_id
                       AND restore_event.event_type='disposal_restore'
                       AND stop_event.event_type='disposal_stop'
                       AND stop_event.depreciation_profile_id=NEW.id
                       AND stop_event.company_id=NEW.company_id
                       AND stop_event.asset_id=NEW.asset_id
                       AND stop_event.source_disposal_id=restore_event.source_disposal_id
                       AND stop_event.effective_date=restore_event.effective_date
                       AND stop_event.previous_profile_status=NEW.status
                ) INTO disposal_restore_evidence;
            END IF;
            IF NOT controlled_status OR NOT (
                (OLD.status='active' AND NEW.status IN ('suspended','stopped','completed'))
                OR (OLD.status='suspended' AND NEW.status IN ('active','stopped','completed'))
                OR (OLD.status='stopped' AND NEW.status IN ('active','suspended')
                    AND disposal_restore_evidence)
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='invalid or uncontrolled depreciation profile status transition';
            END IF;
            PERFORM set_config('eam_lite.controlled_finance_profile_status','off',true);
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""


WORK_USAGE_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION finance_validate_work_usage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    profile_company bigint;
    profile_asset uuid;
    profile_method varchar;
    profile_unit varchar;
    expected_units numeric;
    previous_closing numeric;
    following_opening numeric;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, asset_id, method, work_unit, expected_total_units
      INTO profile_company, profile_asset, profile_method, profile_unit, expected_units
      FROM finance_assetdepreciationprofile
     WHERE id = NEW.depreciation_profile_id FOR UPDATE;
    IF asset_company IS NULL OR profile_company IS NULL
       OR asset_company <> NEW.company_id OR profile_company <> NEW.company_id
       OR profile_asset <> NEW.asset_id OR profile_method <> 'units_of_production'
       OR NEW.work_unit <> profile_unit
       OR NEW.closing_accumulated_units > expected_units THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage profile, unit or total is invalid';
    END IF;
    IF EXISTS (
        SELECT 1 FROM finance_assetworkusage other
         WHERE other.depreciation_profile_id = NEW.depreciation_profile_id
           AND other.id <> NEW.id
           AND daterange(other.period_start, other.period_end, '[)')
               && daterange(NEW.period_start, NEW.period_end, '[)')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage periods cannot overlap';
    END IF;
    SELECT previous.closing_accumulated_units
      INTO previous_closing
      FROM finance_assetworkusage previous
     WHERE previous.depreciation_profile_id = NEW.depreciation_profile_id
       AND previous.id <> NEW.id
       AND previous.period_end <= NEW.period_start
     ORDER BY previous.period_end DESC, previous.period_start DESC, previous.id DESC
     LIMIT 1;
    IF NEW.opening_accumulated_units <> COALESCE(previous_closing, 0) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage cumulative units cannot move backwards or disconnect';
    END IF;
    SELECT following.opening_accumulated_units
      INTO following_opening
      FROM finance_assetworkusage following
     WHERE following.depreciation_profile_id = NEW.depreciation_profile_id
       AND following.id <> NEW.id
       AND following.period_start >= NEW.period_end
     ORDER BY following.period_start, following.period_end, following.id
     LIMIT 1;
    IF following_opening IS NOT NULL
       AND following_opening <> NEW.closing_accumulated_units THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage cumulative units cannot move backwards or disconnect';
    END IF;
    RETURN NEW;
END;
$$;
"""


WORK_USAGE_GUARD_REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION finance_validate_work_usage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    profile_company bigint;
    profile_asset uuid;
    profile_method varchar;
    profile_unit varchar;
    expected_units numeric;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, asset_id, method, work_unit, expected_total_units
      INTO profile_company, profile_asset, profile_method, profile_unit, expected_units
      FROM finance_assetdepreciationprofile
     WHERE id = NEW.depreciation_profile_id FOR SHARE;
    IF asset_company IS NULL OR profile_company IS NULL
       OR asset_company <> NEW.company_id OR profile_company <> NEW.company_id
       OR profile_asset <> NEW.asset_id OR profile_method <> 'units_of_production'
       OR NEW.work_unit <> profile_unit
       OR NEW.closing_accumulated_units > expected_units THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage profile, unit or total is invalid';
    END IF;
    IF EXISTS (
        SELECT 1 FROM finance_assetworkusage other
         WHERE other.depreciation_profile_id = NEW.depreciation_profile_id
           AND other.id <> NEW.id
           AND daterange(other.period_start, other.period_end, '[)')
               && daterange(NEW.period_start, NEW.period_end, '[)')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage periods cannot overlap';
    END IF;
    IF EXISTS (
        SELECT 1 FROM finance_assetworkusage previous
         WHERE previous.depreciation_profile_id = NEW.depreciation_profile_id
           AND previous.period_end = NEW.period_start
           AND previous.closing_accumulated_units <> NEW.opening_accumulated_units
    ) OR EXISTS (
        SELECT 1 FROM finance_assetworkusage following
         WHERE following.depreciation_profile_id = NEW.depreciation_profile_id
           AND following.period_start = NEW.period_end
           AND following.opening_accumulated_units <> NEW.closing_accumulated_units
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage cumulative units cannot move backwards or disconnect';
    END IF;
    RETURN NEW;
END;
$$;
"""


WORK_USAGE_HISTORY_GUARD_SQL = r"""
CREATE OR REPLACE FUNCTION finance_guard_work_usage_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    locked_profile uuid;
    actor_cleared boolean := false;
    chain_changed boolean := false;
BEGIN
    SELECT id INTO locked_profile
      FROM finance_assetdepreciationprofile
     WHERE id = OLD.depreciation_profile_id
     FOR UPDATE;
    IF TG_OP = 'UPDATE' THEN
        actor_cleared := OLD.entered_by_id IS NOT NULL
            AND NEW.entered_by_id IS NULL
            AND (to_jsonb(NEW) - 'entered_by_id') IS NOT DISTINCT FROM
                (to_jsonb(OLD) - 'entered_by_id');
        chain_changed := NEW.depreciation_profile_id IS DISTINCT FROM
                OLD.depreciation_profile_id
            OR NEW.period_start IS DISTINCT FROM OLD.period_start
            OR NEW.period_end IS DISTINCT FROM OLD.period_end
            OR NEW.opening_accumulated_units IS DISTINCT FROM
                OLD.opening_accumulated_units
            OR NEW.current_units IS DISTINCT FROM OLD.current_units
            OR NEW.closing_accumulated_units IS DISTINCT FROM
                OLD.closing_accumulated_units;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM finance_depreciationbatchitem item
          JOIN finance_depreciationbatch batch ON batch.id = item.batch_id
         WHERE item.depreciation_profile_id = OLD.depreciation_profile_id
           AND batch.status IN ('confirmed', 'reversed')
           AND daterange(batch.period_start, batch.period_end, '[)')
               && daterange(OLD.period_start, OLD.period_end, '[)')
    ) AND NOT actor_cleared THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage used by confirmed depreciation is immutable';
    END IF;
    IF (TG_OP = 'DELETE' OR (chain_changed AND NOT actor_cleared)) AND EXISTS (
        SELECT 1
          FROM finance_assetworkusage following
         WHERE following.depreciation_profile_id = OLD.depreciation_profile_id
           AND following.id <> OLD.id
           AND following.period_start >= OLD.period_end
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage with following history cannot be deleted or changed';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
"""


WORK_USAGE_HISTORY_GUARD_REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION finance_guard_work_usage_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM finance_depreciationbatchitem item
          JOIN finance_depreciationbatch batch ON batch.id = item.batch_id
         WHERE item.depreciation_profile_id = OLD.depreciation_profile_id
           AND batch.status IN ('confirmed', 'reversed')
           AND daterange(batch.period_start, batch.period_end, '[)')
               && daterange(OLD.period_start, OLD.period_end, '[)')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'work usage used by confirmed depreciation is immutable';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;
"""


def mark_existing_profiles_for_continuation_review(apps, schema_editor):
    profile = apps.get_model("finance", "AssetDepreciationProfile")
    queryset = profile.objects.using(schema_editor.connection.alias).all()
    if schema_editor.connection.vendor != "postgresql":
        queryset.update(actual_continuation_review_required=True)
        return

    table = schema_editor.quote_name(profile._meta.db_table)
    trigger = schema_editor.quote_name("trg_finance_profile_validate")
    schema_editor.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    try:
        queryset.update(actual_continuation_review_required=True)
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    finally:
        schema_editor.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


def install_work_usage_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PROFILE_GUARD_SQL)
        schema_editor.execute(WORK_USAGE_GUARD_SQL)
        schema_editor.execute(WORK_USAGE_HISTORY_GUARD_SQL)


def restore_work_usage_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PROFILE_GUARD_REVERSE_SQL)
        schema_editor.execute(WORK_USAGE_GUARD_REVERSE_SQL)
        schema_editor.execute(WORK_USAGE_HISTORY_GUARD_REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [("finance", "0008_sprint11_confirmed_entry_source_guards")]

    operations = [
        migrations.AddField(
            model_name="assetdepreciationprofile",
            name="actual_continuation_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="assetdepreciationprofile",
            name="actual_continuation_review_required",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            mark_existing_profiles_for_continuation_review,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="assetdepreciationprofile",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        actual_continuation_review_required=True,
                        actual_continuation_date__isnull=True,
                    )
                    | Q(
                        actual_continuation_review_required=False,
                        actual_continuation_date__gte=F("start_date"),
                    )
                ),
                name="ck_depr_profile_continuation_date",
            ),
        ),
        migrations.RunPython(
            install_work_usage_guard,
            reverse_code=restore_work_usage_guard,
        ),
    ]
