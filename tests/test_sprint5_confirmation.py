from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset, AssetQrIdentity
from apps.assets.services import submit_asset_for_finance
from apps.audit.models import AuditLog
from apps.finance.models import (
    AssetDepreciationProfile,
    AssetFinance,
    AssetValueAdjustment,
    DepreciationEntry,
    DepreciationSchedule,
    TheoreticalDepreciationRun,
)
from apps.finance.services import confirm_asset_finance
from apps.imports import services as import_services
from apps.imports.services import confirm_import_batch, upload_and_validate_import
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_sprint3_support import (
    add_photo,
    make_structurally_valid_active_scheme,
    make_user,
)
from tests.test_sprint5_support import (
    add_finance_row,
    asset_workbook_upload,
    finance_configuration,
    physical_row,
    sprint5_context,
)


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_import_media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


def _validated_batch(actor, company, rows, key, *, filename="asset-initialization.xlsx"):
    batch = upload_and_validate_import(
        actor=actor,
        company=company,
        import_type="asset_initialization",
        uploaded_file=asset_workbook_upload(company, rows, filename=filename),
        idempotency_key=key,
    )
    assert batch.status == "validated", [row.errors_json for row in batch.rows.all()]
    return batch


def test_confirmation_atomically_creates_only_drafts_and_never_issues_or_labels():
    company, actor, category, department, employee, location = sprint5_context(
        prefix="S5CONFIRM"
    )
    rows = [
        physical_row(
            company,
            category,
            department,
            employee,
            location,
            **{"资产名称": f"盘点设备 {index}", "序列号": f"CONF-SN-{index}"},
        )
        for index in range(1, 4)
    ]
    batch = _validated_batch(actor, company, rows, "confirm-three-drafts")

    assert Asset.objects.filter(company=company).count() == 0
    assert AssetFinance.objects.filter(company=company).count() == 0
    confirm_import_batch(actor=actor, batch=batch)
    batch.refresh_from_db()

    assets = list(Asset.objects.filter(company=company).order_by("asset_name"))
    assert len(assets) == 3
    assert {asset.asset_status for asset in assets} == {"draft"}
    assert {asset.initialization_source for asset in assets} == {"excel_import"}
    assert all(asset.asset_code is None and asset.current_issued_code_id is None for asset in assets)
    assert batch.status == "confirmed"
    assert batch.rows.filter(
        validation_status="created", created_object_type="Asset"
    ).count() == 3
    assert {row.created_object_id for row in batch.rows.all()} == {
        str(asset.pk) for asset in assets
    }
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetQrIdentity.objects.count() == 0
    assert DepreciationSchedule.objects.count() == 0
    assert DepreciationEntry.objects.count() == 0
    assert AuditLog.objects.filter(
        company=company, action="import_confirm", object_id=str(batch.pk)
    ).exists()


def test_confirmation_rollback_and_retry_idempotency():
    company, actor, category, department, employee, location = sprint5_context(
        prefix="S5ROLLBACK"
    )
    rows = [
        physical_row(
            company,
            category,
            department,
            employee,
            location,
            **{"资产名称": f"回滚设备 {index}", "序列号": f"ROLL-SN-{index}"},
        )
        for index in (1, 2)
    ]
    batch = _validated_batch(actor, company, rows, "rollback-all-or-none")
    real_create = import_services.create_asset_draft
    calls = 0

    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated Sprint 5 write failure")
        return real_create(**kwargs)

    with patch("apps.imports.services.create_asset_draft", side_effect=fail_second):
        with pytest.raises(RuntimeError, match="simulated Sprint 5"):
            confirm_import_batch(actor=actor, batch=batch)

    batch.refresh_from_db()
    assert batch.status == "validated"
    assert not Asset.objects.filter(company=company).exists()
    assert not batch.rows.filter(validation_status="created").exists()
    assert not AuditLog.objects.filter(
        action="import_confirm", object_id=str(batch.pk)
    ).exists()
    assert AuditLog.objects.filter(
        action="import_confirm_failed", object_id=str(batch.pk)
    ).exists()

    confirm_import_batch(actor=actor, batch=batch)
    confirm_import_batch(actor=actor, batch=batch)
    assert Asset.objects.filter(company=company).count() == 2
    assert batch.rows.filter(validation_status="created").count() == 2
    assert AuditLog.objects.filter(
        action="import_confirm", object_id=str(batch.pk)
    ).count() == 1


def test_finance_confirmation_creates_unconfirmed_finance_and_draft_profile_only():
    company, finance, category, department, employee, location = sprint5_context(
        role="finance", prefix="S5FINCONF"
    )
    fixed, policy = finance_configuration(company, finance)
    row = physical_row(company, category, department, employee, location)
    add_finance_row(
        row,
        fixed_category=fixed,
        policy=policy,
        opening_ad="1234.56",
        opening_impairment="500.00",
        opening_book="10265.44",
    )
    batch = _validated_batch(finance, company, [row], "finance-draft-profile")
    preview = batch.rows.get().normalized_data_json
    actual_opening = preview["profile_data"][
        "opening_actual_accumulated_depreciation"
    ]
    theoretical = preview["theoretical_reference"][
        "planned_accumulated_depreciation"
    ]

    confirm_import_batch(actor=finance, batch=batch)
    asset = Asset.objects.get(company=company)
    finance_row = AssetFinance.objects.get(asset=asset)
    profile = AssetDepreciationProfile.objects.get(asset=asset)

    assert asset.asset_status == "draft"
    assert asset.asset_code is None
    assert finance_row.finance_confirmed_by_id is None
    assert finance_row.finance_confirmed_at is None
    assert str(finance_row.original_cost) == "12000.00"
    assert profile.version == 1
    assert profile.status == "draft"
    assert str(profile.opening_actual_accumulated_depreciation) == "1234.56"
    assert str(finance_row.impairment_balance_cache) == "0.00"
    assert str(profile.opening_book_value) == "10265.44"
    assert actual_opening == "1234.56"
    assert theoretical != actual_opening
    assert not profile.schedules.exists()
    assert DepreciationEntry.objects.count() == 0
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetQrIdentity.objects.count() == 0
    actions = set(
        AuditLog.objects.filter(company=company).values_list("action", flat=True)
    )
    assert {
        "asset_finance_import_draft_create",
        "depreciation_profile_import_draft_create",
    } <= actions


def test_import_audit_has_state_events_but_no_source_name_or_attachment_instruction():
    company, actor, category, department, employee, location = sprint5_context(
        prefix="S5AUDIT"
    )
    marker = "DO-NOT-LOG-SOURCE-MARKER-998877"
    row = physical_row(
        company,
        category,
        department,
        employee,
        location,
        **{"附件后续上传说明": marker},
    )
    source_name = "secret-ledger-name.xlsx"
    batch = _validated_batch(
        actor,
        company,
        [row],
        "audit-no-source-leak",
        filename=source_name,
    )
    confirm_import_batch(actor=actor, batch=batch)

    logs = AuditLog.objects.filter(company=company).order_by("created_at")
    import_actions = set(logs.values_list("action", flat=True))
    assert {"import_upload", "import_validate", "import_confirm"} <= import_actions
    import_logs = logs.filter(
        action__in={"import_upload", "import_validate", "import_confirm"}
    )
    serialized = json.dumps(
        list(import_logs.values("old_data_json", "new_data_json")), ensure_ascii=False
    )
    assert marker not in serialized
    assert source_name not in serialized


def test_original_source_download_is_object_authorized(client):
    company, actor, category, department, employee, location = sprint5_context(
        prefix="S5DOWNLOAD"
    )
    batch = _validated_batch(
        actor,
        company,
        [physical_row(company, category, department, employee, location)],
        "download-object-permission",
    )
    source_url = reverse("imports:source", kwargs={"pk": batch.pk})

    client.force_login(actor)
    allowed = client.get(source_url)
    assert allowed.status_code == 200
    assert allowed["X-Content-Type-Options"] == "nosniff"
    assert "attachment" in allowed["Content-Disposition"]

    system_admin = make_user("s5-source-system-admin", "system_admin")
    client.force_login(system_admin)
    assert client.get(source_url).status_code == 403


def test_sprint4_formalization_consumes_imported_draft_profile_without_duplicate():
    """The imported v1 draft is the future confirmed profile, not a second source."""

    from apps.masterdata.services import set_system_setting

    company, finance, category, department, employee, location = sprint5_context(
        role="finance", prefix="S5FORMAL"
    )
    admin = make_user("s5-formal-admin", "system_admin")
    scheme = make_structurally_valid_active_scheme(
        actor=admin, company=company, key="S5-FORMAL-CODE"
    )
    type(scheme).objects.filter(pk=scheme.pk).update(is_default=True)
    set_system_setting(
        actor=finance,
        company=company,
        key="fixed_asset_warning_amount",
        value="5000.00",
    )
    fixed, policy = finance_configuration(company, finance, key="S5-FORMAL-POLICY")
    row = physical_row(company, category, department, employee, location)
    add_finance_row(
        row,
        fixed_category=fixed,
        policy=policy,
        opening_impairment="500.00",
        opening_book="9100.00",
    )
    batch = _validated_batch(finance, company, [row], "import-before-formalize")
    confirm_import_batch(actor=finance, batch=batch)
    asset = Asset.objects.get(company=company)
    imported_profile = AssetDepreciationProfile.objects.get(asset=asset)

    add_photo(finance, asset)
    asset = submit_asset_for_finance(actor=finance, asset=asset)
    asset = confirm_asset_finance(
        actor=finance,
        asset=asset,
        finance_data={
            "accounting_treatment": "fixed_asset",
            "fixed_asset_category": fixed,
            "original_cost": "12000.00",
            "capitalization_date": asset.commissioning_date,
        },
        profile_data={
            "depreciation_policy": policy,
            "method": imported_profile.method,
            "posting_period": imported_profile.posting_period,
            "start_rule": imported_profile.start_rule,
            "stop_rule": imported_profile.stop_rule,
            "specified_start": imported_profile.start_date,
            "useful_life_months": imported_profile.useful_life_months,
            "salvage_mode": imported_profile.salvage_mode,
            "salvage_rate": imported_profile.salvage_rate,
            "opening_actual_accumulated_depreciation": "2400.00",
            "opening_impairment": "500.00",
            "opening_book_value": "9100.00",
            "allow_historical_start": True,
            "change_reason": "旧资产初始化承接",
        },
        code_effective_date=timezone.localdate(),
        code_effective_reason="",
        idempotency_key="formalize-imported-draft-profile",
        reason="财务复核导入草稿后正式化",
    )

    assert asset.asset_status == "pending_label"
    assert AssetDepreciationProfile.objects.filter(asset=asset).count() == 1
    imported_profile.refresh_from_db()
    assert imported_profile.status == "active"
    assert imported_profile.version == 1
    assert DepreciationEntry.objects.filter(
        asset=asset, opening_profile=imported_profile, source_type="opening"
    ).count() == 1
    finance_row = AssetFinance.objects.get(asset=asset)
    assert finance_row.impairment_balance_cache == 500
    assert AssetValueAdjustment.objects.filter(
        asset=asset,
        adjustment_type="opening_impairment",
        amount="500.00",
        status="confirmed",
    ).count() == 1
