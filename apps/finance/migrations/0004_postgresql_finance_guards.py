from django.db import migrations


CREATE_SQL = r"""
CREATE OR REPLACE FUNCTION finance_validate_asset_finance()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    asset_state varchar;
    commissioning date;
    category_company bigint;
    category_active boolean;
    controlled_balance boolean;
    actor_cleared boolean;
BEGIN
    SELECT company_id, asset_status, commissioning_date
      INTO asset_company, asset_state, commissioning
      FROM assets_asset WHERE id = NEW.asset_id FOR SHARE;
    IF asset_company IS NULL OR asset_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'asset finance must be in the asset company';
    END IF;
    IF NEW.fixed_asset_category_id IS NOT NULL THEN
        SELECT company_id, is_active INTO category_company, category_active
          FROM masterdata_fixedassetcategory
         WHERE id = NEW.fixed_asset_category_id FOR SHARE;
        IF category_company IS NULL OR category_company <> NEW.company_id THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'fixed asset category must be in the finance company';
        END IF;
        IF (TG_OP = 'INSERT'
            OR NEW.fixed_asset_category_id IS DISTINCT FROM OLD.fixed_asset_category_id
            OR NEW.finance_confirmed_at IS DISTINCT FROM OLD.finance_confirmed_at)
           AND NOT category_active THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'finance confirmation requires an active fixed asset category';
        END IF;
    END IF;
    IF NEW.finance_confirmed_at IS NOT NULL THEN
        IF asset_state NOT IN ('pending_finance', 'pending_label') THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'confirmed finance requires a formalization transaction';
        END IF;
        IF NEW.accounting_treatment = 'fixed_asset'
           AND commissioning IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'fixed asset confirmation requires commissioning date';
        END IF;
        IF NEW.accounting_treatment = 'controlled_non_fixed'
           AND btrim(NEW.accounting_treatment_reason) = ''
           AND NEW.original_cost >= NEW.recognition_threshold_snapshot THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'controlled non-fixed treatment at or above threshold requires reason';
        END IF;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.finance_confirmed_at IS NOT NULL
       AND NEW.finance_confirmed_by_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'finance confirmation requires a current finance actor';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        actor_cleared := (
            OLD.finance_confirmed_by_id IS NOT NULL
            AND NEW.finance_confirmed_by_id IS NULL
        ) OR (
            OLD.finance_confirmed_at IS NULL
            AND NEW.finance_confirmed_at IS NOT NULL
            AND OLD.finance_confirmed_by_id IS NULL
            AND NEW.finance_confirmed_by_id IS NOT NULL
        );
        IF NEW.finance_confirmed_by_id IS DISTINCT FROM OLD.finance_confirmed_by_id
           AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'finance confirmation actor is immutable';
        END IF;
        IF OLD.finance_confirmed_at IS NOT NULL THEN
            IF (to_jsonb(NEW) - ARRAY[
                    'original_cost', 'impairment_balance_cache',
                    'finance_confirmed_by_id', 'updated_at'
                ]) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY[
                    'original_cost', 'impairment_balance_cache',
                    'finance_confirmed_by_id', 'updated_at'
                ]) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'confirmed asset finance identity and confirmation snapshot are immutable';
            END IF;
            IF ROW(NEW.original_cost, NEW.impairment_balance_cache)
               IS DISTINCT FROM ROW(OLD.original_cost, OLD.impairment_balance_cache) THEN
                controlled_balance := COALESCE(
                    current_setting('eam_lite.controlled_finance_balance_mutation', true), ''
                ) = 'on';
                IF NOT controlled_balance THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'finance balances must be changed by the controlled service';
                END IF;
                PERFORM set_config(
                    'eam_lite.controlled_finance_balance_mutation', 'off', true
                );
            END IF;
        ELSIF NEW.finance_confirmed_at IS NOT NULL
              AND NEW.finance_confirmed_by_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'finance confirmation requires a current finance actor';
        END IF;
        IF OLD.finance_confirmed_at IS NOT NULL
           AND NEW.finance_confirmed_at IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'confirmed asset finance cannot return to draft';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    previous_company bigint;
    previous_key varchar;
    previous_version integer;
    actor_cleared boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.company_id::text || ':' || NEW.policy_key, 0)
    );
    IF NEW.previous_version_id IS NOT NULL THEN
        SELECT company_id, policy_key, version
          INTO previous_company, previous_key, previous_version
          FROM finance_depreciationpolicy
         WHERE id = NEW.previous_version_id FOR SHARE;
        IF previous_company IS NULL OR previous_company <> NEW.company_id
           OR previous_key <> NEW.policy_key OR previous_version >= NEW.version THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'depreciation policy previous version is invalid';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        actor_cleared := OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL;
        IF NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
           AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'depreciation policy creator is immutable';
        END IF;
        IF OLD.status IN ('active', 'retired')
           AND (to_jsonb(NEW) - ARRAY[
                'status', 'is_default', 'effective_to', 'created_by_id', 'updated_at'
           ]) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY[
                'status', 'is_default', 'effective_to', 'created_by_id', 'updated_at'
           ]) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'active or retired depreciation policy rules are immutable';
        END IF;
        IF OLD.status = 'retired' AND NEW.status <> 'retired' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'retired depreciation policy cannot be reactivated';
        END IF;
        IF OLD.status = 'active' AND NEW.status = 'retired'
           AND EXISTS (
                SELECT 1 FROM masterdata_assetcategory
                 WHERE default_depreciation_policy_id = OLD.id
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'clear category defaults before retiring a policy';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_policy_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_company bigint;
    target_key varchar;
BEGIN
    target_company := CASE WHEN TG_OP = 'DELETE' THEN OLD.company_id ELSE NEW.company_id END;
    target_key := CASE WHEN TG_OP = 'DELETE' THEN OLD.policy_key ELSE NEW.policy_key END;
    IF EXISTS (
        SELECT 1
          FROM finance_depreciationpolicy left_policy
          JOIN finance_depreciationpolicy right_policy
            ON right_policy.company_id = left_policy.company_id
           AND right_policy.policy_key = left_policy.policy_key
           AND right_policy.id > left_policy.id
           AND daterange(
                right_policy.effective_from,
                COALESCE(right_policy.effective_to, 'infinity'::date), '[]'
           ) && daterange(
                left_policy.effective_from,
                COALESCE(left_policy.effective_to, 'infinity'::date), '[]'
           )
         WHERE left_policy.company_id = target_company
           AND left_policy.policy_key = target_key
           AND left_policy.status = 'active'
           AND right_policy.status = 'active'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'active depreciation policy periods cannot overlap';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_policy_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status <> 'draft'
       OR EXISTS (SELECT 1 FROM finance_assetdepreciationprofile
                   WHERE depreciation_policy_id = OLD.id)
       OR EXISTS (SELECT 1 FROM masterdata_assetcategory
                   WHERE default_depreciation_policy_id = OLD.id)
       OR EXISTS (SELECT 1 FROM finance_depreciationpolicy
                   WHERE previous_version_id = OLD.id) THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'used or effective depreciation policy cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_category_default_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    policy_company bigint;
    policy_status varchar;
    policy_from date;
    policy_to date;
    today_shanghai date;
BEGIN
    IF NEW.default_depreciation_policy_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT company_id, status, effective_from, effective_to
      INTO policy_company, policy_status, policy_from, policy_to
      FROM finance_depreciationpolicy
     WHERE id = NEW.default_depreciation_policy_id FOR SHARE;
    today_shanghai := (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date;
    IF policy_company IS NULL OR policy_company <> NEW.company_id
       OR policy_status <> 'active' OR policy_from > today_shanghai
       OR (policy_to IS NOT NULL AND policy_to < today_shanghai) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'category depreciation policy must be current and in the same company';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_fixed_category_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM finance_assetfinance WHERE fixed_asset_category_id = OLD.id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'referenced fixed asset category cannot be deleted';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.company_id IS DISTINCT FROM OLD.company_id
       AND EXISTS (
            SELECT 1 FROM finance_assetfinance WHERE fixed_asset_category_id = OLD.id
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'referenced fixed asset category cannot change company';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_profile()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    policy_company bigint;
    policy_status varchar;
    policy_from date;
    policy_to date;
    controlled_status boolean;
    actor_cleared boolean;
BEGIN
    SELECT company_id INTO asset_company
      FROM assets_asset WHERE id = NEW.asset_id FOR SHARE;
    SELECT company_id, status, effective_from, effective_to
      INTO policy_company, policy_status, policy_from, policy_to
      FROM finance_depreciationpolicy
     WHERE id = NEW.depreciation_policy_id FOR SHARE;
    IF asset_company IS NULL OR asset_company <> NEW.company_id
       OR policy_company IS NULL OR policy_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation profile references must be in one company';
    END IF;
    IF TG_OP = 'INSERT' AND NEW.status <> 'draft'
       AND (policy_status <> 'active' OR policy_from > NEW.effective_from
            OR (policy_to IS NOT NULL AND policy_to < NEW.effective_from)) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'confirmed profile requires an effective policy version';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        actor_cleared := OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL;
        IF NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
           AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'depreciation profile creator is immutable';
        END IF;
        IF OLD.status <> 'draft'
           AND (to_jsonb(NEW) - ARRAY[
                'status', 'effective_to', 'created_by_id'
           ]) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY[
                'status', 'effective_to', 'created_by_id'
           ]) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'confirmed depreciation profile parameters are immutable';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           AND NOT (OLD.status = 'draft' AND NEW.status = 'active') THEN
            controlled_status := COALESCE(
                current_setting('eam_lite.controlled_finance_profile_status', true), ''
            ) = 'on';
            IF NOT controlled_status OR NOT (
                (OLD.status = 'active' AND NEW.status IN ('suspended', 'stopped', 'completed'))
                OR (OLD.status = 'suspended' AND NEW.status IN ('active', 'stopped', 'completed'))
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'invalid or uncontrolled depreciation profile status transition';
            END IF;
            PERFORM set_config(
                'eam_lite.controlled_finance_profile_status', 'off', true
            );
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_profile_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_asset uuid;
    current_status varchar;
    latest_event varchar;
BEGIN
    target_asset := CASE WHEN TG_OP = 'DELETE' THEN OLD.asset_id ELSE NEW.asset_id END;
    IF EXISTS (
        SELECT 1
          FROM finance_assetdepreciationprofile left_profile
          JOIN finance_assetdepreciationprofile right_profile
            ON right_profile.asset_id = left_profile.asset_id
           AND right_profile.id > left_profile.id
           AND daterange(
                right_profile.effective_from,
                COALESCE(right_profile.effective_to, 'infinity'::date), '[]'
           ) && daterange(
                left_profile.effective_from,
                COALESCE(left_profile.effective_to, 'infinity'::date), '[]'
           )
         WHERE left_profile.asset_id = target_asset
           AND left_profile.status <> 'draft'
           AND right_profile.status <> 'draft'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation profile effective periods cannot overlap';
    END IF;
    IF (SELECT count(*) FROM finance_assetdepreciationprofile
         WHERE asset_id = target_asset AND status IN ('active', 'suspended')) > 1 THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'asset can have only one current depreciation profile';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        SELECT status INTO current_status
          FROM finance_assetdepreciationprofile WHERE id = NEW.id;
        SELECT event_type INTO latest_event
          FROM finance_depreciationprofileevent
         WHERE depreciation_profile_id = NEW.id
         ORDER BY effective_date DESC, created_at DESC, id DESC LIMIT 1;
        IF current_status = 'suspended' AND latest_event <> 'suspend'
           OR current_status = 'stopped' AND latest_event <> 'stop'
           OR current_status = 'active' AND OLD.status = 'suspended'
              AND latest_event <> 'resume' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'profile status transition requires its append-only event';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_profile_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status <> 'draft'
       OR EXISTS (SELECT 1 FROM finance_depreciationschedule
                   WHERE depreciation_profile_id = OLD.id)
       OR EXISTS (SELECT 1 FROM finance_depreciationentry
                   WHERE depreciation_profile_id = OLD.id)
       OR EXISTS (SELECT 1 FROM finance_depreciationprofileevent
                   WHERE depreciation_profile_id = OLD.id) THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'confirmed or referenced depreciation profile cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_schedule()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    profile_company bigint;
    profile_asset uuid;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, asset_id INTO profile_company, profile_asset
      FROM finance_assetdepreciationprofile WHERE id = NEW.depreciation_profile_id;
    IF asset_company IS NULL OR profile_company IS NULL
       OR asset_company <> NEW.company_id OR profile_company <> NEW.company_id
       OR profile_asset <> NEW.asset_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation schedule references are inconsistent';
    END IF;
    IF TG_OP = 'UPDATE'
       AND (to_jsonb(NEW) - 'status') IS DISTINCT FROM
           (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation schedule values are immutable';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NOT (OLD.status = NEW.status
                OR (OLD.status = 'planned' AND NEW.status = 'superseded')) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'invalid depreciation schedule status transition';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_schedule_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    profile_status varchar;
BEGIN
    SELECT status INTO profile_status FROM finance_assetdepreciationprofile
     WHERE id = OLD.depreciation_profile_id;
    IF profile_status <> 'draft' THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'confirmed depreciation schedule cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_profile_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    profile_company bigint;
    profile_asset uuid;
    profile_status varchar;
    last_date date;
BEGIN
    SELECT company_id, asset_id, status
      INTO profile_company, profile_asset, profile_status
      FROM finance_assetdepreciationprofile
     WHERE id = NEW.depreciation_profile_id FOR SHARE;
    IF profile_company IS NULL OR profile_company <> NEW.company_id
       OR profile_asset <> NEW.asset_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation event references are inconsistent';
    END IF;
    SELECT max(effective_date) INTO last_date
      FROM finance_depreciationprofileevent
     WHERE depreciation_profile_id = NEW.depreciation_profile_id;
    IF last_date IS NOT NULL AND NEW.effective_date <= last_date THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation event dates must move forward';
    END IF;
    IF (NEW.event_type = 'suspend' AND profile_status <> 'active')
       OR (NEW.event_type = 'resume' AND profile_status <> 'suspended')
       OR (NEW.event_type = 'stop' AND profile_status NOT IN ('active', 'suspended')) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation event does not match current profile status';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_profile_event_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    profile_status varchar;
BEGIN
    SELECT status INTO profile_status FROM finance_assetdepreciationprofile
     WHERE id = NEW.depreciation_profile_id;
    IF (NEW.event_type = 'suspend' AND profile_status <> 'suspended')
       OR (NEW.event_type = 'resume' AND profile_status <> 'active')
       OR (NEW.event_type = 'stop' AND profile_status <> 'stopped') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation event and profile status must commit together';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_profile_event_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL
       AND (to_jsonb(NEW) - 'created_by_id') =
           (to_jsonb(OLD) - 'created_by_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514',
        MESSAGE = 'depreciation profile events are append-only';
END;
$$;

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

CREATE OR REPLACE FUNCTION finance_validate_batch()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_company bigint;
    source_type varchar;
    source_status varchar;
    source_period_start date;
    source_period_end date;
    source_generation integer;
    superseded_company bigint;
    superseded_type varchar;
    superseded_status varchar;
    superseded_period_start date;
    superseded_period_end date;
    superseded_generation integer;
    controlled_reversal boolean;
    actors_cleared boolean;
BEGIN
    IF date_trunc('month', NEW.period_start)::date <> NEW.period_start
       OR (NEW.period_start + interval '1 month')::date <> NEW.period_end THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation batch must cover one complete natural month';
    END IF;
    IF NEW.reverses_batch_id IS NOT NULL THEN
        SELECT company_id, batch_type, status, period_start, period_end, generation_no
          INTO source_company, source_type, source_status,
               source_period_start, source_period_end, source_generation
          FROM finance_depreciationbatch
         WHERE id = NEW.reverses_batch_id FOR SHARE;
        IF source_company IS NULL OR source_company <> NEW.company_id
           OR source_type <> 'regular' OR source_status NOT IN ('confirmed', 'reversed')
           OR NEW.batch_type <> 'reversal'
           OR NEW.period_start <> source_period_start
           OR NEW.period_end <> source_period_end
           OR NEW.generation_no <> source_generation THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'reversal batch source is invalid';
        END IF;
    END IF;
    IF NEW.supersedes_batch_id IS NOT NULL THEN
        SELECT company_id, batch_type, status, period_start, period_end, generation_no
          INTO superseded_company, superseded_type, superseded_status,
               superseded_period_start, superseded_period_end, superseded_generation
          FROM finance_depreciationbatch
         WHERE id = NEW.supersedes_batch_id FOR SHARE;
        IF superseded_company IS NULL
           OR superseded_company <> NEW.company_id
           OR superseded_type <> 'regular'
           OR superseded_status <> 'reversed'
           OR superseded_period_start <> NEW.period_start
           OR superseded_period_end <> NEW.period_end
           OR NEW.batch_type <> 'regular'
           OR NEW.generation_no <> superseded_generation + 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'superseding depreciation batch source is invalid';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        actors_cleared := (NEW.generated_by_id IS NOT DISTINCT FROM OLD.generated_by_id
                           OR (OLD.generated_by_id IS NOT NULL AND NEW.generated_by_id IS NULL))
            AND (NEW.confirmed_by_id IS NOT DISTINCT FROM OLD.confirmed_by_id
                 OR (OLD.confirmed_by_id IS NOT NULL AND NEW.confirmed_by_id IS NULL)
                 OR (
                    OLD.status = 'draft'
                    AND NEW.status = 'confirmed'
                    AND OLD.confirmed_by_id IS NULL
                    AND NEW.confirmed_by_id IS NOT NULL
                 ));
        IF NOT actors_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'depreciation batch actors are immutable';
        END IF;
        IF OLD.status IN ('confirmed', 'reversed') THEN
            IF (to_jsonb(NEW) - ARRAY['status', 'generated_by_id', 'confirmed_by_id'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status', 'generated_by_id', 'confirmed_by_id']) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'confirmed depreciation batch snapshot is immutable';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                controlled_reversal := COALESCE(
                    current_setting('eam_lite.controlled_finance_batch_reversal', true), ''
                ) = 'on';
                IF NOT controlled_reversal
                   OR NOT (OLD.status = 'confirmed' AND NEW.status = 'reversed') THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'confirmed batch can only be reversed by controlled service';
                END IF;
                PERFORM set_config(
                    'eam_lite.controlled_finance_batch_reversal', 'off', true
                );
            END IF;
        ELSIF OLD.status = 'cancelled' AND NEW.status <> 'cancelled' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'cancelled depreciation batch is immutable';
        ELSIF OLD.status = 'draft'
              AND NEW.status NOT IN ('draft', 'confirmed', 'cancelled') THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'invalid depreciation batch status transition';
        END IF;
    ELSIF NEW.status <> 'draft' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation batch must be created as draft';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_batch_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_batch uuid;
    target_status varchar;
    target_type varchar;
    target_source uuid;
    changed_row jsonb;
BEGIN
    -- This deferred function is shared by Batch and BatchItem triggers.  A
    -- PL/pgSQL CASE that directly mentions both ``OLD.batch_id`` and
    -- ``OLD.id`` still resolves the non-selected record field for the actual
    -- trigger row type.  PostgreSQL then raises (for example) "OLD has no
    -- field batch_id" on an ordinary Batch INSERT.  Convert the active row to
    -- JSON first so the table-specific identifier can be selected safely.
    changed_row := CASE
        WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD)
        ELSE to_jsonb(NEW)
    END;
    target_batch := CASE
        WHEN TG_TABLE_NAME = 'finance_depreciationbatchitem'
            THEN (changed_row ->> 'batch_id')::uuid
        ELSE (changed_row ->> 'id')::uuid
    END;
    SELECT status, batch_type, reverses_batch_id
      INTO target_status, target_type, target_source
      FROM finance_depreciationbatch WHERE id = target_batch;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF target_status = 'confirmed' THEN
        IF EXISTS (SELECT 1 FROM finance_depreciationbatchitem
                    WHERE batch_id = target_batch AND status = 'error')
           OR EXISTS (
                SELECT 1 FROM finance_depreciationbatchitem item
                 WHERE item.batch_id = target_batch AND item.status = 'ready'
                   AND NOT EXISTS (
                        SELECT 1 FROM finance_depreciationentry entry
                         WHERE entry.batch_item_id = item.id
                   )
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'confirmed depreciation batch requires complete ready entries';
        END IF;
        IF target_type = 'reversal'
           AND NOT EXISTS (
                SELECT 1 FROM finance_depreciationbatch source
                 WHERE source.id = target_source AND source.status = 'reversed'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'confirmed reversal and source batch status must commit together';
        END IF;
    ELSIF target_status = 'reversed' AND NOT EXISTS (
        SELECT 1 FROM finance_depreciationbatch reversal
         WHERE reversal.reverses_batch_id = target_batch
           AND reversal.status = 'confirmed'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'reversed batch requires a confirmed reversal batch';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_batch_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status NOT IN ('draft', 'cancelled') THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'confirmed depreciation batch cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_batch_item()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    batch_company bigint;
    batch_status varchar;
    asset_company bigint;
    profile_company bigint;
    profile_asset uuid;
    schedule_company bigint;
    schedule_asset uuid;
    schedule_profile uuid;
BEGIN
    SELECT company_id, status INTO batch_company, batch_status
      FROM finance_depreciationbatch WHERE id = NEW.batch_id FOR SHARE;
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, asset_id INTO profile_company, profile_asset
      FROM finance_assetdepreciationprofile WHERE id = NEW.depreciation_profile_id;
    IF NEW.depreciation_schedule_id IS NOT NULL THEN
        SELECT company_id, asset_id, depreciation_profile_id
          INTO schedule_company, schedule_asset, schedule_profile
          FROM finance_depreciationschedule WHERE id = NEW.depreciation_schedule_id;
    END IF;
    IF batch_company IS NULL OR asset_company IS NULL OR profile_company IS NULL
       OR batch_company <> NEW.company_id OR asset_company <> NEW.company_id
       OR profile_company <> NEW.company_id OR profile_asset <> NEW.asset_id
       OR (NEW.depreciation_schedule_id IS NOT NULL
           AND (schedule_company <> NEW.company_id OR schedule_asset <> NEW.asset_id
                OR schedule_profile <> NEW.depreciation_profile_id)) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation batch item references are inconsistent';
    END IF;
    IF TG_OP = 'INSERT' AND batch_status IN ('confirmed', 'reversed') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'confirmed depreciation batch cannot receive new items';
    END IF;
    IF TG_OP = 'UPDATE' AND batch_status IN ('confirmed', 'reversed') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'confirmed depreciation batch items are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_batch_item_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    batch_status varchar;
BEGIN
    SELECT status INTO batch_status FROM finance_depreciationbatch WHERE id = OLD.batch_id;
    IF batch_status NOT IN ('draft', 'cancelled') THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'confirmed depreciation batch items cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_adjustment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    original_company bigint;
    original_asset uuid;
    original_type varchar;
    original_amount numeric;
    original_status varchar;
    original_reversal uuid;
    controlled_reversal boolean;
    actors_cleared boolean;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    IF asset_company IS NULL OR asset_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'asset value adjustment must be in the asset company';
    END IF;
    IF NEW.reversal_of_id IS NOT NULL THEN
        SELECT company_id, asset_id, adjustment_type, amount, status, reversal_of_id
          INTO original_company, original_asset, original_type, original_amount,
               original_status, original_reversal
          FROM finance_assetvalueadjustment
         WHERE id = NEW.reversal_of_id FOR SHARE;
        IF original_company IS NULL OR original_company <> NEW.company_id
           OR original_asset <> NEW.asset_id OR original_status NOT IN ('confirmed', 'reversed')
           OR original_reversal IS NOT NULL
           OR NOT (
                (original_type IN ('opening_impairment', 'impairment')
                 AND NEW.adjustment_type = 'impairment_reversal'
                 AND NEW.amount = original_amount)
                OR (original_type = 'impairment_reversal'
                    AND NEW.adjustment_type = 'impairment'
                    AND NEW.amount = original_amount)
                OR (original_type IN ('cost_correction', 'depreciation_adjustment')
                    AND NEW.adjustment_type = original_type
                    AND NEW.amount = -original_amount)
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'asset value adjustment reversal is invalid';
        END IF;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.status = 'confirmed'
       AND NEW.confirmed_by_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'adjustment confirmation requires a current finance actor';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        actors_cleared := (NEW.created_by_id IS NOT DISTINCT FROM OLD.created_by_id
                           OR (OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL))
            AND (NEW.confirmed_by_id IS NOT DISTINCT FROM OLD.confirmed_by_id
                 OR (OLD.confirmed_by_id IS NOT NULL AND NEW.confirmed_by_id IS NULL));
        IF NOT actors_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'adjustment actors are immutable';
        END IF;
        IF OLD.status IN ('confirmed', 'reversed') THEN
            IF (to_jsonb(NEW) - ARRAY['status', 'created_by_id', 'confirmed_by_id'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status', 'created_by_id', 'confirmed_by_id']) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'confirmed adjustment is immutable';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                controlled_reversal := COALESCE(
                    current_setting('eam_lite.controlled_finance_adjustment_reversal', true), ''
                ) = 'on';
                IF NOT controlled_reversal
                   OR NOT (OLD.status = 'confirmed' AND NEW.status = 'reversed') THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'confirmed adjustment can only be reversed by controlled service';
                END IF;
                PERFORM set_config(
                    'eam_lite.controlled_finance_adjustment_reversal', 'off', true
                );
            END IF;
        ELSIF OLD.status = 'draft' AND NEW.status = 'confirmed'
              AND NEW.confirmed_by_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'adjustment confirmation requires a current finance actor';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_adjustment_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION USING ERRCODE = '23503',
            MESSAGE = 'confirmed asset value adjustment cannot be deleted';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_entry()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    profile_company bigint;
    profile_asset uuid;
    source_company bigint;
    source_asset uuid;
    source_profile uuid;
    adjustment_type varchar;
    original_company bigint;
    original_asset uuid;
    original_amount numeric;
    original_reversal uuid;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, asset_id INTO profile_company, profile_asset
      FROM finance_assetdepreciationprofile WHERE id = NEW.depreciation_profile_id;
    IF asset_company IS NULL OR profile_company IS NULL
       OR asset_company <> NEW.company_id OR profile_company <> NEW.company_id
       OR profile_asset <> NEW.asset_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation entry asset and profile are inconsistent';
    END IF;
    IF NEW.source_type = 'batch' THEN
        SELECT item.company_id, item.asset_id, item.depreciation_profile_id
          INTO source_company, source_asset, source_profile
          FROM finance_depreciationbatchitem item WHERE item.id = NEW.batch_item_id;
    ELSIF NEW.source_type = 'opening' THEN
        SELECT company_id, asset_id, id
          INTO source_company, source_asset, source_profile
          FROM finance_assetdepreciationprofile WHERE id = NEW.opening_profile_id;
        IF NEW.opening_profile_id <> NEW.depreciation_profile_id
           OR (SELECT version FROM finance_assetdepreciationprofile
                WHERE id = NEW.opening_profile_id) <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'opening entry requires the first matching profile';
        END IF;
    ELSE
        SELECT adjustment.company_id, adjustment.asset_id,
               adjustment.adjustment_type
          INTO source_company, source_asset, adjustment_type
          FROM finance_assetvalueadjustment AS adjustment
         WHERE adjustment.id = NEW.value_adjustment_id;
        source_profile := NEW.depreciation_profile_id;
        IF adjustment_type <> 'depreciation_adjustment' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'only depreciation adjustments can create depreciation entries';
        END IF;
    END IF;
    IF source_company IS NULL OR source_company <> NEW.company_id
       OR source_asset <> NEW.asset_id OR source_profile <> NEW.depreciation_profile_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'depreciation entry source is inconsistent';
    END IF;
    IF NEW.reversal_of_id IS NOT NULL THEN
        SELECT company_id, asset_id, amount, reversal_of_id
          INTO original_company, original_asset, original_amount, original_reversal
          FROM finance_depreciationentry WHERE id = NEW.reversal_of_id FOR SHARE;
        IF original_company IS NULL OR original_company <> NEW.company_id
           OR original_asset <> NEW.asset_id OR original_reversal IS NOT NULL
           OR NEW.amount <> -original_amount THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'depreciation entry reversal must be exact and in scope';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_entry_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.posted_by_id IS NOT NULL AND NEW.posted_by_id IS NULL
       AND (to_jsonb(NEW) - 'posted_by_id') = (to_jsonb(OLD) - 'posted_by_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514',
        MESSAGE = 'posted depreciation entries are append-only';
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_theoretical_run()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    actor_cleared boolean;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    IF asset_company IS NULL OR asset_company <> NEW.company_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'theoretical run must be in the asset company';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        actor_cleared := OLD.requested_by_id IS NOT NULL AND NEW.requested_by_id IS NULL;
        IF NEW.requested_by_id IS DISTINCT FROM OLD.requested_by_id
           AND NOT actor_cleared THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'theoretical run requester is immutable';
        END IF;
        IF OLD.status IN ('completed', 'failed')
           AND (to_jsonb(NEW) - 'requested_by_id') IS DISTINCT FROM
               (to_jsonb(OLD) - 'requested_by_id') THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'completed theoretical run is immutable';
        END IF;
        IF OLD.status = 'draft' AND NEW.status NOT IN ('draft', 'completed', 'failed') THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'invalid theoretical run transition';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_theoretical_line()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    run_status varchar;
BEGIN
    SELECT status INTO run_status
      FROM finance_theoreticaldepreciationrun
     WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.run_id ELSE NEW.run_id END;
    IF run_status <> 'draft' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'completed theoretical depreciation lines are immutable';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_formalization_request()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    asset_company bigint;
    finance_company bigint;
    finance_asset uuid;
    issued_company bigint;
BEGIN
    SELECT company_id INTO asset_company FROM assets_asset WHERE id = NEW.asset_id;
    SELECT company_id, asset_id INTO finance_company, finance_asset
      FROM finance_assetfinance WHERE id = NEW.result_finance_id;
    SELECT company_id INTO issued_company
      FROM masterdata_issuedcode WHERE id = NEW.result_issued_code_id;
    IF asset_company IS NULL OR finance_company IS NULL OR issued_company IS NULL
       OR asset_company <> NEW.company_id OR finance_company <> NEW.company_id
       OR issued_company <> NEW.company_id OR finance_asset <> NEW.asset_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'formalization idempotency result references are inconsistent';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_formalization_request()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.created_by_id IS NOT NULL AND NEW.created_by_id IS NULL
       AND (to_jsonb(NEW) - 'created_by_id') = (to_jsonb(OLD) - 'created_by_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514',
        MESSAGE = 'completed formalization idempotency records are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_finance_asset_finance_validate ON finance_assetfinance;
CREATE TRIGGER trg_finance_asset_finance_validate
BEFORE INSERT OR UPDATE ON finance_assetfinance
FOR EACH ROW EXECUTE FUNCTION finance_validate_asset_finance();

DROP TRIGGER IF EXISTS trg_finance_policy_validate ON finance_depreciationpolicy;
CREATE TRIGGER trg_finance_policy_validate
BEFORE INSERT OR UPDATE ON finance_depreciationpolicy
FOR EACH ROW EXECUTE FUNCTION finance_validate_policy();
DROP TRIGGER IF EXISTS trg_finance_policy_commit ON finance_depreciationpolicy;
CREATE CONSTRAINT TRIGGER trg_finance_policy_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_depreciationpolicy
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_validate_policy_commit();
DROP TRIGGER IF EXISTS trg_finance_policy_delete ON finance_depreciationpolicy;
CREATE TRIGGER trg_finance_policy_delete
BEFORE DELETE ON finance_depreciationpolicy
FOR EACH ROW EXECUTE FUNCTION finance_guard_policy_delete();

DROP TRIGGER IF EXISTS trg_finance_category_policy ON masterdata_assetcategory;
CREATE TRIGGER trg_finance_category_policy
BEFORE INSERT OR UPDATE OF company_id, default_depreciation_policy_id
ON masterdata_assetcategory
FOR EACH ROW EXECUTE FUNCTION finance_validate_category_default_policy();
DROP TRIGGER IF EXISTS trg_finance_fixed_category_guard ON masterdata_fixedassetcategory;
CREATE TRIGGER trg_finance_fixed_category_guard
BEFORE UPDATE OF company_id OR DELETE ON masterdata_fixedassetcategory
FOR EACH ROW EXECUTE FUNCTION finance_guard_fixed_category_change();

DROP TRIGGER IF EXISTS trg_finance_profile_validate ON finance_assetdepreciationprofile;
CREATE TRIGGER trg_finance_profile_validate
BEFORE INSERT OR UPDATE ON finance_assetdepreciationprofile
FOR EACH ROW EXECUTE FUNCTION finance_validate_profile();
DROP TRIGGER IF EXISTS trg_finance_profile_commit ON finance_assetdepreciationprofile;
CREATE CONSTRAINT TRIGGER trg_finance_profile_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_assetdepreciationprofile
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_validate_profile_commit();
DROP TRIGGER IF EXISTS trg_finance_profile_delete ON finance_assetdepreciationprofile;
CREATE TRIGGER trg_finance_profile_delete
BEFORE DELETE ON finance_assetdepreciationprofile
FOR EACH ROW EXECUTE FUNCTION finance_guard_profile_delete();

DROP TRIGGER IF EXISTS trg_finance_schedule_validate ON finance_depreciationschedule;
CREATE TRIGGER trg_finance_schedule_validate
BEFORE INSERT OR UPDATE ON finance_depreciationschedule
FOR EACH ROW EXECUTE FUNCTION finance_validate_schedule();
DROP TRIGGER IF EXISTS trg_finance_schedule_delete ON finance_depreciationschedule;
CREATE TRIGGER trg_finance_schedule_delete
BEFORE DELETE ON finance_depreciationschedule
FOR EACH ROW EXECUTE FUNCTION finance_guard_schedule_delete();

DROP TRIGGER IF EXISTS trg_finance_profile_event_validate ON finance_depreciationprofileevent;
CREATE TRIGGER trg_finance_profile_event_validate
BEFORE INSERT ON finance_depreciationprofileevent
FOR EACH ROW EXECUTE FUNCTION finance_validate_profile_event();
DROP TRIGGER IF EXISTS trg_finance_profile_event_commit ON finance_depreciationprofileevent;
CREATE CONSTRAINT TRIGGER trg_finance_profile_event_commit
AFTER INSERT ON finance_depreciationprofileevent
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_validate_profile_event_commit();
DROP TRIGGER IF EXISTS trg_finance_profile_event_history ON finance_depreciationprofileevent;
CREATE TRIGGER trg_finance_profile_event_history
BEFORE UPDATE OR DELETE ON finance_depreciationprofileevent
FOR EACH ROW EXECUTE FUNCTION finance_guard_profile_event_history();

DROP TRIGGER IF EXISTS trg_finance_work_usage_validate ON finance_assetworkusage;
CREATE TRIGGER trg_finance_work_usage_validate
BEFORE INSERT OR UPDATE ON finance_assetworkusage
FOR EACH ROW EXECUTE FUNCTION finance_validate_work_usage();
DROP TRIGGER IF EXISTS trg_finance_work_usage_history ON finance_assetworkusage;
CREATE TRIGGER trg_finance_work_usage_history
BEFORE UPDATE OR DELETE ON finance_assetworkusage
FOR EACH ROW EXECUTE FUNCTION finance_guard_work_usage_history();

DROP TRIGGER IF EXISTS trg_finance_batch_validate ON finance_depreciationbatch;
CREATE TRIGGER trg_finance_batch_validate
BEFORE INSERT OR UPDATE ON finance_depreciationbatch
FOR EACH ROW EXECUTE FUNCTION finance_validate_batch();
DROP TRIGGER IF EXISTS trg_finance_batch_commit ON finance_depreciationbatch;
CREATE CONSTRAINT TRIGGER trg_finance_batch_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_depreciationbatch
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_validate_batch_commit();
DROP TRIGGER IF EXISTS trg_finance_batch_delete ON finance_depreciationbatch;
CREATE TRIGGER trg_finance_batch_delete
BEFORE DELETE ON finance_depreciationbatch
FOR EACH ROW EXECUTE FUNCTION finance_guard_batch_delete();

DROP TRIGGER IF EXISTS trg_finance_batch_item_validate ON finance_depreciationbatchitem;
CREATE TRIGGER trg_finance_batch_item_validate
BEFORE INSERT OR UPDATE ON finance_depreciationbatchitem
FOR EACH ROW EXECUTE FUNCTION finance_validate_batch_item();
DROP TRIGGER IF EXISTS trg_finance_batch_item_commit ON finance_depreciationbatchitem;
CREATE CONSTRAINT TRIGGER trg_finance_batch_item_commit
AFTER INSERT OR UPDATE OR DELETE ON finance_depreciationbatchitem
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION finance_validate_batch_commit();
DROP TRIGGER IF EXISTS trg_finance_batch_item_delete ON finance_depreciationbatchitem;
CREATE TRIGGER trg_finance_batch_item_delete
BEFORE DELETE ON finance_depreciationbatchitem
FOR EACH ROW EXECUTE FUNCTION finance_guard_batch_item_delete();

DROP TRIGGER IF EXISTS trg_finance_adjustment_validate ON finance_assetvalueadjustment;
CREATE TRIGGER trg_finance_adjustment_validate
BEFORE INSERT OR UPDATE ON finance_assetvalueadjustment
FOR EACH ROW EXECUTE FUNCTION finance_validate_adjustment();
DROP TRIGGER IF EXISTS trg_finance_adjustment_delete ON finance_assetvalueadjustment;
CREATE TRIGGER trg_finance_adjustment_delete
BEFORE DELETE ON finance_assetvalueadjustment
FOR EACH ROW EXECUTE FUNCTION finance_guard_adjustment_delete();

DROP TRIGGER IF EXISTS trg_finance_entry_validate ON finance_depreciationentry;
CREATE TRIGGER trg_finance_entry_validate
BEFORE INSERT ON finance_depreciationentry
FOR EACH ROW EXECUTE FUNCTION finance_validate_entry();
DROP TRIGGER IF EXISTS trg_finance_entry_history ON finance_depreciationentry;
CREATE TRIGGER trg_finance_entry_history
BEFORE UPDATE OR DELETE ON finance_depreciationentry
FOR EACH ROW EXECUTE FUNCTION finance_guard_entry_history();

DROP TRIGGER IF EXISTS trg_finance_theoretical_run_validate ON finance_theoreticaldepreciationrun;
CREATE TRIGGER trg_finance_theoretical_run_validate
BEFORE INSERT OR UPDATE ON finance_theoreticaldepreciationrun
FOR EACH ROW EXECUTE FUNCTION finance_validate_theoretical_run();
DROP TRIGGER IF EXISTS trg_finance_theoretical_line_guard ON finance_theoreticaldepreciationline;
CREATE TRIGGER trg_finance_theoretical_line_guard
BEFORE INSERT OR UPDATE OR DELETE ON finance_theoreticaldepreciationline
FOR EACH ROW EXECUTE FUNCTION finance_guard_theoretical_line();

DROP TRIGGER IF EXISTS trg_finance_formalization_validate ON finance_financeformalizationrequest;
CREATE TRIGGER trg_finance_formalization_validate
BEFORE INSERT ON finance_financeformalizationrequest
FOR EACH ROW EXECUTE FUNCTION finance_validate_formalization_request();
DROP TRIGGER IF EXISTS trg_finance_formalization_history ON finance_financeformalizationrequest;
CREATE TRIGGER trg_finance_formalization_history
BEFORE UPDATE OR DELETE ON finance_financeformalizationrequest
FOR EACH ROW EXECUTE FUNCTION finance_guard_formalization_request();
"""


DROP_SQL = r"""
DROP TRIGGER IF EXISTS trg_finance_formalization_history ON finance_financeformalizationrequest;
DROP TRIGGER IF EXISTS trg_finance_formalization_validate ON finance_financeformalizationrequest;
DROP TRIGGER IF EXISTS trg_finance_theoretical_line_guard ON finance_theoreticaldepreciationline;
DROP TRIGGER IF EXISTS trg_finance_theoretical_run_validate ON finance_theoreticaldepreciationrun;
DROP TRIGGER IF EXISTS trg_finance_entry_history ON finance_depreciationentry;
DROP TRIGGER IF EXISTS trg_finance_entry_validate ON finance_depreciationentry;
DROP TRIGGER IF EXISTS trg_finance_adjustment_delete ON finance_assetvalueadjustment;
DROP TRIGGER IF EXISTS trg_finance_adjustment_validate ON finance_assetvalueadjustment;
DROP TRIGGER IF EXISTS trg_finance_batch_item_delete ON finance_depreciationbatchitem;
DROP TRIGGER IF EXISTS trg_finance_batch_item_commit ON finance_depreciationbatchitem;
DROP TRIGGER IF EXISTS trg_finance_batch_item_validate ON finance_depreciationbatchitem;
DROP TRIGGER IF EXISTS trg_finance_batch_delete ON finance_depreciationbatch;
DROP TRIGGER IF EXISTS trg_finance_batch_commit ON finance_depreciationbatch;
DROP TRIGGER IF EXISTS trg_finance_batch_validate ON finance_depreciationbatch;
DROP TRIGGER IF EXISTS trg_finance_work_usage_history ON finance_assetworkusage;
DROP TRIGGER IF EXISTS trg_finance_work_usage_validate ON finance_assetworkusage;
DROP TRIGGER IF EXISTS trg_finance_profile_event_history ON finance_depreciationprofileevent;
DROP TRIGGER IF EXISTS trg_finance_profile_event_commit ON finance_depreciationprofileevent;
DROP TRIGGER IF EXISTS trg_finance_profile_event_validate ON finance_depreciationprofileevent;
DROP TRIGGER IF EXISTS trg_finance_schedule_delete ON finance_depreciationschedule;
DROP TRIGGER IF EXISTS trg_finance_schedule_validate ON finance_depreciationschedule;
DROP TRIGGER IF EXISTS trg_finance_profile_delete ON finance_assetdepreciationprofile;
DROP TRIGGER IF EXISTS trg_finance_profile_commit ON finance_assetdepreciationprofile;
DROP TRIGGER IF EXISTS trg_finance_profile_validate ON finance_assetdepreciationprofile;
DROP TRIGGER IF EXISTS trg_finance_fixed_category_guard ON masterdata_fixedassetcategory;
DROP TRIGGER IF EXISTS trg_finance_category_policy ON masterdata_assetcategory;
DROP TRIGGER IF EXISTS trg_finance_policy_delete ON finance_depreciationpolicy;
DROP TRIGGER IF EXISTS trg_finance_policy_commit ON finance_depreciationpolicy;
DROP TRIGGER IF EXISTS trg_finance_policy_validate ON finance_depreciationpolicy;
DROP TRIGGER IF EXISTS trg_finance_asset_finance_validate ON finance_assetfinance;
DROP FUNCTION IF EXISTS finance_guard_formalization_request();
DROP FUNCTION IF EXISTS finance_validate_formalization_request();
DROP FUNCTION IF EXISTS finance_guard_theoretical_line();
DROP FUNCTION IF EXISTS finance_validate_theoretical_run();
DROP FUNCTION IF EXISTS finance_guard_entry_history();
DROP FUNCTION IF EXISTS finance_validate_entry();
DROP FUNCTION IF EXISTS finance_guard_adjustment_delete();
DROP FUNCTION IF EXISTS finance_validate_adjustment();
DROP FUNCTION IF EXISTS finance_guard_batch_item_delete();
DROP FUNCTION IF EXISTS finance_validate_batch_item();
DROP FUNCTION IF EXISTS finance_guard_batch_delete();
DROP FUNCTION IF EXISTS finance_validate_batch_commit();
DROP FUNCTION IF EXISTS finance_validate_batch();
DROP FUNCTION IF EXISTS finance_guard_work_usage_history();
DROP FUNCTION IF EXISTS finance_validate_work_usage();
DROP FUNCTION IF EXISTS finance_guard_profile_event_history();
DROP FUNCTION IF EXISTS finance_validate_profile_event_commit();
DROP FUNCTION IF EXISTS finance_validate_profile_event();
DROP FUNCTION IF EXISTS finance_guard_schedule_delete();
DROP FUNCTION IF EXISTS finance_validate_schedule();
DROP FUNCTION IF EXISTS finance_guard_profile_delete();
DROP FUNCTION IF EXISTS finance_validate_profile_commit();
DROP FUNCTION IF EXISTS finance_validate_profile();
DROP FUNCTION IF EXISTS finance_guard_fixed_category_change();
DROP FUNCTION IF EXISTS finance_validate_category_default_policy();
DROP FUNCTION IF EXISTS finance_guard_policy_delete();
DROP FUNCTION IF EXISTS finance_validate_policy_commit();
DROP FUNCTION IF EXISTS finance_validate_policy();
DROP FUNCTION IF EXISTS finance_validate_asset_finance();
"""


def install_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE_SQL)


def remove_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0003_remove_assetfinance_ck_asset_finance_confirmation_fields_and_more"),
        ("masterdata", "0008_sprint4_counter_guards"),
    ]

    operations = [
        migrations.RunPython(
            install_postgresql_guards,
            reverse_code=remove_postgresql_guards,
        )
    ]
