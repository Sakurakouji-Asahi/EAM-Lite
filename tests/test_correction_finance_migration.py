from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.finance.models import AssetDepreciationProfile
from apps.finance.services import review_profile_actual_continuation_date
from tests.test_correction_finance_services import _custom_profile_context


@pytest.mark.django_db(transaction=True)
def test_profile_migration_marks_existing_rows_pending_without_guessing_date():
    _company, actor, _management, _admin, _asset, _finance, profile = (
        _custom_profile_context(
            method="straight_line",
            opening_ad=4560,
            opening_book=7440,
        )
    )
    profile_id = profile.pk
    target_old = ("finance", "0008_sprint11_confirmed_entry_source_guards")
    target_new = ("finance", "0009_profile_actual_continuation")

    try:
        executor = MigrationExecutor(connection)
        executor.migrate([target_old])
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_get_functiondef(%s::regprocedure)",
                    ["finance_guard_work_usage_history()"],
                )
                restored_guard = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT pg_get_functiondef(%s::regprocedure)",
                    ["finance_validate_profile()"],
                )
                restored_profile_guard = cursor.fetchone()[0]
            assert "FOR UPDATE" not in restored_guard
            assert "following.period_start" not in restored_guard
            assert "continuation_reviewed" not in restored_profile_guard
            assert "disposal_restore_evidence" in restored_profile_guard

        executor = MigrationExecutor(connection)
        executor.migrate([target_new])
        migrated_apps = executor.loader.project_state([target_new]).apps
        migrated = migrated_apps.get_model(
            "finance", "AssetDepreciationProfile"
        ).objects.get(pk=profile_id)

        assert migrated.actual_continuation_date is None
        assert migrated.actual_continuation_review_required is True
        assert migrated.opening_actual_accumulated_depreciation == 4560
        assert migrated.opening_book_value == 7440
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tgenabled
                      FROM pg_trigger
                     WHERE tgname = %s
                       AND tgrelid = 'finance_assetdepreciationprofile'::regclass
                    """,
                    ["trg_finance_profile_validate"],
                )
                assert cursor.fetchone() == ("O",)
                cursor.execute(
                    "SELECT pg_get_functiondef(%s::regprocedure)",
                    ["finance_validate_profile()"],
                )
                installed_profile_guard = cursor.fetchone()[0]
                assert "continuation_reviewed" in installed_profile_guard
                assert "disposal_restore_evidence" in installed_profile_guard
            assert migrated.status == "active"
            with pytest.raises(
                IntegrityError,
                match="confirmed depreciation profile parameters are immutable",
            ), transaction.atomic():
                type(migrated).objects.filter(pk=profile_id).update(
                    actual_continuation_date=migrated.start_date,
                    actual_continuation_review_required=False,
                    useful_life_months=migrated.useful_life_months + 12,
                )
        reviewed = review_profile_actual_continuation_date(
            actor=actor,
            profile=AssetDepreciationProfile.objects.get(pk=profile_id),
            actual_continuation_date=migrated.start_date,
            reason="升级后逐项核对原始台账",
        )
        assert reviewed.actual_continuation_date == migrated.start_date
        assert reviewed.actual_continuation_review_required is False
        if connection.vendor == "postgresql":
            with pytest.raises(
                IntegrityError,
                match="confirmed depreciation profile parameters are immutable",
            ), transaction.atomic():
                AssetDepreciationProfile.objects.filter(pk=profile_id).update(
                    actual_continuation_date=migrated.start_date + timedelta(days=1)
                )
    finally:
        MigrationExecutor(connection).migrate([target_new])


@pytest.mark.django_db(transaction=True)
def test_profile_migration_fresh_database_keeps_new_profiles_reviewed():
    target_old = ("finance", "0008_sprint11_confirmed_entry_source_guards")
    target_new = ("finance", "0009_profile_actual_continuation")
    try:
        MigrationExecutor(connection).migrate([target_old])
        MigrationExecutor(connection).migrate([target_new])
        _company, _actor, _management, _admin, _asset, _finance, profile = (
            _custom_profile_context(method="straight_line")
        )
        assert profile.actual_continuation_date == profile.start_date
        assert profile.actual_continuation_review_required is False
    finally:
        MigrationExecutor(connection).migrate([target_new])
