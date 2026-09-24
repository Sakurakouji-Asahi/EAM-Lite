"""Business rollback, number reuse and boundary tests against real services."""
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.assets.models import Asset, AssetIdentity, AssetQrIdentity, AssetRegistration
from apps.assets.registration import create_registered_asset, register_asset
from apps.assets.qr_services import generate_print_batch
from apps.audit.models import AuditLog, OperationUndo
from apps.audit.undo import preview_undo, undo_operation
from apps.finance.models import AssetFinance, AssetDepreciationProfile
from apps.finance.services import create_fixed_asset_category
from apps.imports.services import upload_and_validate_import, confirm_import_batch
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_unified_asset_identity import context, physical_data
from tests.test_sprint5_support import physical_row, add_finance_row, asset_workbook_upload
from tests.test_sprint3_support import make_company

pytestmark = pytest.mark.django_db(transaction=True)


def registered(ctx, key="first", **changes):
    return create_registered_asset(actor=ctx["finance"], company=ctx["company"],
        data=physical_data(ctx, **changes), idempotency_key=key)


def log_for(asset):
    return AuditLog.objects.filter(action="asset_register", object_id=str(asset.pk)).latest("pk")


def undo(ctx, log):
    preview = preview_undo(actor=ctx["finance"], log=log)
    return undo_operation(actor=ctx["finance"], log=log, reason="录入错误，重新整理", confirmation=preview["confirmation"])


def test_registration_returns_draft_and_reuses_exact_number(context):
    asset = registered(context)
    old_code, token, original = asset.asset_code, asset.qr_identities.get().public_token, log_for(asset)
    proof = preview_undo(actor=context["finance"], log=original)
    result = undo(context, original)
    asset.refresh_from_db()
    assert asset.asset_status == "draft" and asset.asset_code is None
    assert asset.submitted_at is None and asset.asset_name == "统一编码设备"
    assert not AssetIdentity.objects.exists() and not IssuedCode.objects.exists()
    assert not AssetQrIdentity.objects.filter(public_token=token).exists()
    assert AuditLog.objects.filter(pk=original.pk).exists()
    assert undo_operation(actor=context["finance"], log=original, reason="重复请求", confirmation=proof["confirmation"]).pk == result.pk
    assert SequenceCounter.objects.get().current_value == 0
    with pytest.raises(ValidationError, match="已撤销"):
        registered(context)
    asset = register_asset(actor=context["finance"], asset=asset, idempotency_key="new-request")
    assert asset.asset_code == old_code == "FA-02-2020-000001-00"
    assert asset.qr_identities.get().public_token != token
    assert preview_undo(actor=context["finance"], log=original)["already_undone"]


def test_cannot_release_number_before_later_asset(context):
    first = registered(context, "first")
    second = registered(context, "second")
    with pytest.raises(ValidationError, match="后续建档"):
        preview_undo(actor=context["finance"], log=log_for(first))
    undo(context, log_for(second))
    undo(context, log_for(first))
    assert SequenceCounter.objects.get().current_value == 0


def test_printed_or_in_print_batch_cannot_be_undone(context, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    asset = registered(context)
    generate_print_batch(actor=context["finance"], assets=[asset], idempotency_key="print")
    with pytest.raises(ValidationError, match="打印|引用"):
        preview_undo(actor=context["finance"], log=log_for(asset))
    assert IssuedCode.objects.count() == 1 and not OperationUndo.objects.exists()


def imported(ctx, rows=2, financial=True):
    company, actor = ctx["company"], ctx["finance"]
    entries = []
    if financial:
        policy = ctx["policy"]
        fixed = create_fixed_asset_category(actor=actor, company=company,
            data={"code": "UNDO-FIN", "name": "设备", "useful_life_months_default": 60})
    for i in range(rows):
        row = physical_row(company, ctx["category"], ctx["department"], ctx["employee"], ctx["location"], **{
            "资产名称": f"待撤销设备 {i}", "首次管理属性": "FA", "序列号": str(i), "历史参考编号": str(i),
        })
        if financial:
            add_finance_row(row, fixed_category=fixed, policy=policy, **{"起算规则": "specified_date"})
        entries.append(row)
    workbook = asset_workbook_upload(company, entries)
    original_bytes = workbook.read()
    workbook.seek(0)
    batch = upload_and_validate_import(actor=actor, company=company, import_type="asset_initialization",
        uploaded_file=workbook, idempotency_key="import")
    assert batch.status == "validated", [r.errors_json for r in batch.rows.all()]
    confirm_import_batch(actor=actor, batch=batch)
    batch._test_upload_bytes = original_bytes
    return batch, entries


@pytest.mark.parametrize("register", [False, True])
def test_import_undo_removes_only_its_assets_and_allows_same_file(context, settings, tmp_path, register):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "imports"
    unrelated = registered(context, "unrelated")
    batch, entries = imported(context)
    assets = list(Asset.objects.exclude(pk=unrelated.pk).order_by("pk"))
    codes = []
    if register:
        for asset in assets:
            register_asset(actor=context["finance"], asset=asset, idempotency_key=str(asset.pk))
            asset.refresh_from_db()
            codes.append(asset.asset_code)
    log = AuditLog.objects.get(action="import_confirm", object_id=str(batch.pk))
    undo(context, log)
    batch.refresh_from_db()
    assert batch.status == "reversed" and batch.rows.count() == 2
    assert list(Asset.objects.values_list("pk", flat=True)) == [unrelated.pk]
    assert not AssetFinance.objects.exists() and not AssetDepreciationProfile.objects.exists()
    assert IssuedCode.objects.count() == 1
    assert not IssuedCode.objects.filter(display_code__in=codes).exists()
    with pytest.raises(ValidationError):
        confirm_import_batch(actor=context["finance"], batch=batch)
    replacement = upload_and_validate_import(actor=context["finance"], company=context["company"],
        import_type="asset_initialization", uploaded_file=SimpleUploadedFile('same.xlsx',batch._test_upload_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'), idempotency_key="reimport")
    assert replacement.status == "validated" and replacement.pk != batch.pk
    assert replacement.file_sha256 == batch.file_sha256


def test_preview_changes_and_injected_payload_are_rejected(context):
    asset = registered(context)
    log = log_for(asset)
    preview = preview_undo(actor=context["finance"], log=log)
    registered(context, "later")
    with pytest.raises(ValidationError):
        undo_operation(actor=context["finance"], log=log, reason="撤销", confirmation=preview["confirmation"])
    with pytest.raises(ValidationError):
        undo_operation(actor=context["finance"], log=log, reason="撤销", confirmation="forged")
    assert IssuedCode.objects.count() == 2 and not OperationUndo.objects.exists()


def test_permissions_http_confirmation_and_csrf(context, client):
    asset = registered(context)
    log = log_for(asset)
    url = reverse("audit:operation-undo", args=[log.pk])
    client.force_login(context["admin"])
    assert client.get(url).status_code == 403
    with pytest.raises(PermissionDenied):
        preview_undo(actor=context["admin"], log=log)
    client.force_login(context["finance"])
    response = client.get(url)
    assert response.status_code == 200 and "确认撤销" in response.content.decode()
    token = response.context["confirmation"]
    assert AssetRegistration.objects.count() == 1  # GET is read-only.
    assert client.post(url, {"confirmation": token, "reason": ""}).status_code == 400
    assert client.post(url, {"confirmation": token, "reason": "测试撤销"}).status_code == 302
    assert client.get(reverse("audit:log-list")).status_code == 200
    assert "已撤销" in client.get(url).content.decode()


def test_postgres_direct_deletion_still_rejected(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL guard")
    asset = registered(context)
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM assets_assetregistration WHERE asset_id=%s", [asset.pk])
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("UPDATE masterdata_sequencecounter SET current_value=0")
    assert AssetRegistration.objects.count() == 1 and IssuedCode.objects.count() == 1


def test_midway_failure_rolls_back_numbers_assets_and_undo_log(context, monkeypatch):
    import apps.audit.undo as module
    asset = registered(context)
    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")
    monkeypatch.setattr(module, "_raw_delete", fail)
    with pytest.raises(RuntimeError):
        undo(context, log_for(asset))
    asset.refresh_from_db()
    assert asset.asset_status == "pending_label" and IssuedCode.objects.count() == 1
    assert not OperationUndo.objects.exists()
    assert not AuditLog.objects.filter(action="asset_registration_reverse").exists()


def test_one_printed_asset_blocks_entire_import_undo(context, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "imports"
    batch, _ = imported(context)
    for asset in Asset.objects.order_by("pk"):
        register_asset(actor=context["finance"], asset=asset, idempotency_key=str(asset.pk))
    generate_print_batch(actor=context["finance"], assets=[Asset.objects.first()], idempotency_key="print-one")
    log = AuditLog.objects.get(action="import_confirm", object_id=str(batch.pk))
    with pytest.raises(ValidationError, match="打印|引用"):
        undo(context, log)
    assert Asset.objects.count() == IssuedCode.objects.count() == 2
    assert not OperationUndo.objects.exists()
    batch.refresh_from_db()
    assert batch.status == "confirmed"


def test_foreign_company_log_cannot_be_undone(context):
    asset = registered(context)
    other = make_company("FOREIGN-UNDO", active=False)
    from apps.audit.services import write_business_audit_log
    foreign_log = write_business_audit_log(company=other, user=context["finance"], action="asset_register",
        object_type="Asset", object_id=str(asset.pk), new_data={"asset_code": asset.asset_code})
    with pytest.raises(PermissionDenied):
        preview_undo(actor=context["finance"], log=foreign_log)
    assert AssetRegistration.objects.count() == 1


def test_concurrent_undo_replays_one_result(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections
    asset = registered(context)
    log = log_for(asset)
    token = preview_undo(actor=context["finance"], log=log)["confirmation"]
    barrier = Barrier(2)
    def attempt():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return undo_operation(actor=context["finance"], log=log, reason="并发撤销", confirmation=token).pk
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results[0] == results[1]
    assert OperationUndo.objects.count() == 1 and SequenceCounter.objects.get().current_value == 0


def test_csrf_required_for_undo_post(context):
    from django.test import Client
    client = Client(enforce_csrf_checks=True)
    asset = registered(context)
    log = log_for(asset)
    client.force_login(context["finance"])
    preview = preview_undo(actor=context["finance"], log=log)
    response = client.post(reverse("audit:operation-undo", args=[log.pk]),
        {"reason": "撤销", "confirmation": preview["confirmation"]})
    assert response.status_code == 403 and not OperationUndo.objects.exists()


def test_confirmed_finance_blocks_registration_undo(context):
    from decimal import Decimal
    from apps.finance.services import confirm_asset_finance
    asset = registered(context)
    confirm_asset_finance(actor=context["finance"],asset=asset,
        finance_data={"accounting_treatment":"controlled_non_fixed", "original_cost":Decimal("1000.00")},
        idempotency_key="finance-confirm",reason="核对财务")
    with pytest.raises(ValidationError, match="财务|引用"):
        undo(context,log_for(asset))
    assert IssuedCode.objects.count()==1 and not OperationUndo.objects.exists()
