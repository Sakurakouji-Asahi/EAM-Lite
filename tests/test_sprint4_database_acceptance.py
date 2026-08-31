from __future__ import annotations

from datetime import date
from decimal import Decimal
import uuid

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.assets.models import AssetQrIdentity
from apps.assets.services import submit_asset_for_finance
from apps.coding.services import set_default_scheme
from apps.finance.models import (
    AssetFinance,
    DepreciationBatch,
    FinanceFormalizationRequest,
)
from apps.finance.services import confirm_asset_finance
from apps.masterdata.models import InitializationSetting, IssuedCode, SequenceCounter
from tests.test_sprint3_support import (
    add_photo,
    make_asset,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_structurally_valid_active_scheme,
    make_user,
)


pytestmark = pytest.mark.django_db


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 4 database acceptance requires PostgreSQL")


def _pending_asset_context(*, prefix: str = "S4DB"):
    company = make_company(prefix)
    admin = make_user(f"{prefix.lower()}-admin", "system_admin")
    finance = make_user(f"{prefix.lower()}-finance", "finance")
    equipment = make_user(f"{prefix.lower()}-equipment", "equipment")
    department = make_department(company, f"{prefix}-D")
    employee = make_employee(company, department, f"{prefix}-E")
    category = make_category(company, f"{prefix}-CAT")
    _site, _area, location = make_location_tree(company, f"{prefix}-L")
    scheme = make_structurally_valid_active_scheme(
        company=company, actor=admin, key=f"{prefix}-CODE"
    )
    set_default_scheme(actor=admin, scheme=scheme)
    initialization = InitializationSetting.objects.get(company=company)
    initialization.initialization_completed = True
    initialization.company_configured = True
    initialization.departments_configured = True
    initialization.employees_configured = True
    initialization.categories_configured = True
    initialization.locations_configured = True
    initialization.coding_scheme_configured = True
    initialization.finance_rules_configured = True
    initialization.permissions_configured = True
    initialization.users_configured = True
    initialization.completed_by = admin
    initialization.completed_at = timezone.now()
    initialization.save()
    asset = make_asset(
        actor=equipment,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
        commissioning_date=date(2026, 7, 2),
    )
    add_photo(equipment, asset)
    asset = submit_asset_for_finance(actor=equipment, asset=asset)
    return {
        "company": company,
        "admin": admin,
        "finance": finance,
        "equipment": equipment,
        "scheme": scheme,
        "asset": asset,
    }


def _formalize_nonfixed(context, *, key=None):
    return confirm_asset_finance(
        actor=context["finance"],
        asset=context["asset"],
        finance_data={
            "accounting_treatment": "controlled_non_fixed",
            "original_cost": Decimal("100.00"),
        },
        code_effective_date=timezone.localdate(),
        idempotency_key=key or f"formalize-{uuid.uuid4()}",
        reason="Sprint 4 PostgreSQL database acceptance",
    )


def _force_deferred_constraints():
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def test_postgresql_18_6_and_sprint4_constraint_triggers_are_installed():
    require_postgresql()
    expected = {
        "trg_sprint4_formal_asset_commit",
        "trg_sprint4_finance_asset_commit",
        "trg_sprint4_qr_asset_commit",
        "trg_sprint4_profile_asset_commit",
        "trg_sprint4_entry_asset_commit",
        "trg_sprint4_adjustment_asset_commit",
        "trg_sprint4_formalization_asset_commit",
        "trg_sprint4_issued_asset_commit",
        "trg_sequence_counter_history_guard",
        "trg_finance_batch_commit",
        "trg_finance_batch_item_commit",
    }
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        assert cursor.fetchone()[0].startswith("18.6")
        cursor.execute(
            """
            SELECT tgname, tgdeferrable, tginitdeferred
              FROM pg_trigger
             WHERE tgname = ANY(%s) AND NOT tgisinternal
            """,
            [list(expected)],
        )
        installed = {name: (deferrable, initially_deferred) for name, deferrable, initially_deferred in cursor.fetchall()}
    assert expected <= installed.keys()
    for name in expected - {"trg_sequence_counter_history_guard"}:
        assert installed[name] == (True, True), name
    assert installed["trg_sequence_counter_history_guard"] == (False, False)


def test_formalization_deferred_consistency_accepts_only_complete_final_state():
    require_postgresql()
    context = _pending_asset_context(prefix="S4FINAL")

    asset = _formalize_nonfixed(context)
    _force_deferred_constraints()

    asset.refresh_from_db()
    finance = AssetFinance.objects.get(asset=asset)
    qr = AssetQrIdentity.objects.get(asset=asset, status="active")
    request = FinanceFormalizationRequest.objects.get(asset=asset)
    assert asset.asset_status == "pending_label"
    assert asset.current_issued_code_id == request.result_issued_code_id
    assert finance.pk == request.result_finance_id
    assert finance.finance_confirmed_at is not None
    assert qr.label_status == "ready_to_print"

    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM finance_assetfinance WHERE id = %s", [finance.pk])
        _force_deferred_constraints()
    assert AssetQrIdentity.objects.filter(pk=qr.pk).exists()


def test_sequence_counter_is_monotonic_non_deletable_and_only_controlled_increment_works():
    require_postgresql()
    context = _pending_asset_context(prefix="S4COUNTER")
    _formalize_nonfixed(context)
    counter = SequenceCounter.objects.get()

    for sql, params in (
        (
            "UPDATE masterdata_sequencecounter SET current_value = current_value - 1 WHERE id = %s",
            [counter.pk],
        ),
        (
            "UPDATE masterdata_sequencecounter SET current_value = current_value + 1 WHERE id = %s",
            [counter.pk],
        ),
        (
            "UPDATE masterdata_sequencecounter SET scope_key = scope_key || '-tampered' WHERE id = %s",
            [counter.pk],
        ),
        ("DELETE FROM masterdata_sequencecounter WHERE id = %s", [counter.pk]),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_sequence_counter_increment', 'on', true)"
            )
            cursor.execute(
                "UPDATE masterdata_sequencecounter SET current_value = current_value + 1 WHERE id = %s",
                [counter.pk],
            )
    counter.refresh_from_db()
    assert counter.current_value == context["scheme"].sequence_start + 1


def test_issued_qr_formalization_and_confirmed_finance_are_database_immutable():
    require_postgresql()
    context = _pending_asset_context(prefix="S4HISTORY")
    asset = _formalize_nonfixed(context)
    issued = IssuedCode.objects.get(pk=asset.current_issued_code_id)
    qr = AssetQrIdentity.objects.get(asset=asset)
    request = FinanceFormalizationRequest.objects.get(asset=asset)
    finance = AssetFinance.objects.get(asset=asset)

    forbidden = (
        ("UPDATE masterdata_issuedcode SET display_code = 'TAMPERED' WHERE id = %s", [issued.pk]),
        ("DELETE FROM masterdata_issuedcode WHERE id = %s", [issued.pk]),
        ("UPDATE assets_assetqridentity SET public_token = 'tampered' WHERE id = %s", [qr.pk]),
        ("DELETE FROM assets_assetqridentity WHERE id = %s", [qr.pk]),
        ("UPDATE finance_financeformalizationrequest SET request_hash = repeat('0', 64) WHERE id = %s", [request.pk]),
        ("DELETE FROM finance_financeformalizationrequest WHERE id = %s", [request.pk]),
        ("UPDATE finance_assetfinance SET finance_remark = 'tampered' WHERE id = %s", [finance.pk]),
        ("DELETE FROM finance_assetfinance WHERE id = %s", [finance.pk]),
    )
    for sql, params in forbidden:
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
            _force_deferred_constraints()


def test_database_rejects_cross_company_finance_and_qr_references():
    require_postgresql()
    context = _pending_asset_context(prefix="S4SCOPE")
    other = make_company("S4OTHER", active=False)

    with pytest.raises(IntegrityError), transaction.atomic():
        AssetFinance.objects.create(
            company=other,
            asset=context["asset"],
            accounting_treatment="controlled_non_fixed",
            original_cost=Decimal("1.00"),
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        AssetQrIdentity.objects.create(
            company=other,
            asset=context["asset"],
            public_token="x" * 43,
            status="active",
            label_status="ready_to_print",
            issued_at=timezone.now(),
            issued_by=context["finance"],
            version=1,
        )


def _draft_batch(*, company, actor, period_start, generation=1, **overrides):
    values = {
        "company": company,
        "period_start": period_start,
        "period_end": date(period_start.year + (period_start.month == 12), period_start.month % 12 + 1, 1),
        "generation_no": generation,
        "batch_type": "regular",
        "status": "draft",
        "idempotency_key": f"batch-{uuid.uuid4()}",
        "request_hash": uuid.uuid4().hex * 2,
        "generated_by": actor,
        "generated_at": timezone.now(),
    }
    values.update(overrides)
    return DepreciationBatch.objects.create(**values)


def _confirm_empty_batch(batch, actor):
    batch.status = "confirmed"
    batch.confirmed_by = actor
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    _force_deferred_constraints()
    return batch


def test_batch_supersedes_and_reversal_relations_are_strict_database_chains():
    require_postgresql()
    company = make_company("S4BATCH")
    finance = make_user("s4batch-finance", "finance")
    source = _confirm_empty_batch(
        _draft_batch(company=company, actor=finance, period_start=date(2026, 7, 1)),
        finance,
    )

    with transaction.atomic():
        reversal = _draft_batch(
            company=company,
            actor=finance,
            period_start=date(2026, 7, 1),
            generation=1,
            batch_type="reversal",
            reverses_batch=source,
            reversal_reason="database fixture reversal",
        )
        reversal.status = "confirmed"
        reversal.confirmed_by = finance
        reversal.confirmed_at = timezone.now()
        reversal.save(update_fields=["status", "confirmed_by", "confirmed_at"])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_finance_batch_reversal', 'on', true)"
            )
        source.status = "reversed"
        source.save(update_fields=["status"])
        _force_deferred_constraints()

    for sql in (
        "UPDATE finance_depreciationbatch SET request_hash = repeat('f', 64) WHERE id = %s",
        "DELETE FROM finance_depreciationbatch WHERE id = %s",
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, [source.pk])

    successor = _draft_batch(
        company=company,
        actor=finance,
        period_start=date(2026, 7, 1),
        generation=2,
        supersedes_batch=source,
    )
    assert successor.supersedes_batch_id == source.pk

    with pytest.raises(IntegrityError), transaction.atomic():
        _draft_batch(
            company=company,
            actor=finance,
            period_start=date(2026, 7, 1),
            generation=3,
            supersedes_batch=source,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        _draft_batch(
            company=company,
            actor=finance,
            period_start=date(2026, 8, 1),
            generation=1,
            batch_type="reversal",
            reverses_batch=source,
            reversal_reason="period mismatch must fail",
        )


def test_database_allows_draft_batch_to_record_confirming_actor():
    """Acceptance regression: normal batch confirmation must be possible."""

    require_postgresql()
    company = make_company("S4BATCHACTOR")
    finance = make_user("s4batchactor-finance", "finance")
    batch = _draft_batch(
        company=company, actor=finance, period_start=date(2026, 7, 1)
    )

    batch.status = "confirmed"
    batch.confirmed_by = finance
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    _force_deferred_constraints()


def test_formalization_failure_rolls_back_every_side_effect(monkeypatch):
    require_postgresql()
    context = _pending_asset_context(prefix="S4ROLLBACK")
    before = {
        "finance": AssetFinance.objects.count(),
        "counter": SequenceCounter.objects.count(),
        "issued": IssuedCode.objects.count(),
        "qr": AssetQrIdentity.objects.count(),
        "request": FinanceFormalizationRequest.objects.count(),
    }
    calls = 0

    def fail_second_audit(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced audit failure")

    monkeypatch.setattr("apps.finance.services._audit", fail_second_audit)
    with pytest.raises(RuntimeError, match="forced audit failure"):
        _formalize_nonfixed(context, key="rollback-all-side-effects")

    context["asset"].refresh_from_db()
    assert context["asset"].asset_status == "pending_finance"
    assert context["asset"].asset_code is None
    assert context["asset"].current_issued_code_id is None
    assert {
        "finance": AssetFinance.objects.count(),
        "counter": SequenceCounter.objects.count(),
        "issued": IssuedCode.objects.count(),
        "qr": AssetQrIdentity.objects.count(),
        "request": FinanceFormalizationRequest.objects.count(),
    } == before
