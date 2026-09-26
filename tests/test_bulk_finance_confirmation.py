from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection,close_old_connections
from django.urls import reverse
from django.test import Client
from django.utils import timezone

from apps.assets.models import Asset,AssetQrIdentity
from apps.assets.registration import create_registered_asset,register_asset
from apps.audit.models import AuditLog
from apps.finance.bulk_confirmation import preview_bulk_finance,confirm_bulk_finance,SALT
from apps.finance.models import AssetFinance,AssetDepreciationProfile,DepreciationEntry,FinanceFormalizationRequest,DepreciationSchedule
from apps.finance.services import save_asset_finance_draft,create_fixed_asset_category,generate_depreciation_batch,confirm_depreciation_batch,get_asset_depreciation_status
from apps.masterdata.models import IssuedCode,SequenceCounter
from apps.imports.services import upload_and_validate_import,confirm_import_batch
from tests.test_unified_asset_identity import context,physical_data
from tests.test_sprint3_support import make_user,make_company
from tests.test_sprint5_support import physical_row,add_finance_row,asset_workbook_upload

pytestmark=pytest.mark.django_db(transaction=True)


def simple_asset(ctx,name="设备",cost="1000.00",treatment="controlled_non_fixed"):
    asset=create_registered_asset(actor=ctx["equipment"],company=ctx["company"],
        data=physical_data(ctx,asset_name=name),idempotency_key=name)
    save_asset_finance_draft(actor=ctx["finance"],asset=asset,
        data={"accounting_treatment":treatment,"original_cost":Decimal(cost)})
    return asset


def preview(ctx,assets):
    return preview_bulk_finance(actor=ctx["finance"],company=ctx["company"],asset_ids=[a.pk for a in assets])


def confirm(ctx,p):
    return confirm_bulk_finance(actor=ctx["finance"],company=ctx["company"],token=p["token"],acknowledged=True)


def test_preview_is_read_only_and_repeat_confirmation_does_not_reissue(context):
    assets=[simple_asset(context,"零原值","0.00"),simple_asset(context,"非固定设备")]
    identity=[(a.pk,a.asset_code,a.asset_status,a.qr_identities.get().pk) for a in assets]
    models=(AuditLog,FinanceFormalizationRequest,DepreciationEntry,DepreciationSchedule)
    before=[m.objects.count() for m in models]
    p=preview(context,assets)
    assert p["ready_count"]==2 and p["totals"]["cost"]==Decimal("1000.00")
    assert before==[m.objects.count() for m in models]
    first=confirm(context,p)
    assert first["success_count"]==2 and first["error_count"]==0
    assert FinanceFormalizationRequest.objects.count()==2
    assert not DepreciationEntry.objects.exists() and not AssetDepreciationProfile.objects.exists()
    logs=AuditLog.objects.count()
    second=confirm(context,p)
    assert second["success_count"]==2 and all(r["replayed"] for r in second["rows"])
    assert AuditLog.objects.count()==logs
    assert [(a.pk,a.asset_code,a.asset_status,a.qr_identities.get().pk) for a in Asset.objects.order_by("created_at")]==identity
    assert IssuedCode.objects.count()==2


def test_missing_values_are_listed_and_only_ready_rows_confirm(context):
    ready=simple_asset(context,"ready")
    missing=create_registered_asset(actor=context["equipment"],company=context["company"],data=physical_data(context),idempotency_key="missing")
    p=preview(context,[ready,missing])
    assert (p["ready_count"],p["error_count"])==(1,1)
    assert "尚未保存" in p["rows"][1]["error"]
    assert confirm(context,p)["success_count"]==1
    assert not AssetFinance.objects.filter(asset=missing).exists()


def test_changed_finance_is_skipped_but_other_row_completes(context):
    first,second=simple_asset(context,"first"),simple_asset(context,"second")
    p=preview(context,[first,second])
    save_asset_finance_draft(actor=context["finance"],asset=first,data={"original_cost":Decimal("1234.56")})
    result=confirm(context,p)
    assert (result["success_count"],result["error_count"])==(1,1)
    first.finance.refresh_from_db()
    assert first.finance.finance_confirmed_at is None and first.finance.original_cost==Decimal("1234.56")
    assert "变化" in result["rows"][0]["error"]
    assert AssetFinance.objects.get(asset=second).finance_confirmed_at


def test_signature_actor_scope_and_acknowledgement(context):
    asset=simple_asset(context)
    p=preview(context,[asset])
    with pytest.raises(ValidationError):
        confirm_bulk_finance(actor=context["finance"],company=context["company"],token=p["token"],acknowledged=False)
    with pytest.raises(ValidationError):
        confirm(context,{"token":p["token"]+"tamper"})
    other=make_user("other-finance","finance")
    with pytest.raises(ValidationError):
        confirm_bulk_finance(actor=other,company=context["company"],token=p["token"],acknowledged=True)
    with pytest.raises(PermissionDenied):
        preview_bulk_finance(actor=context["equipment"],company=context["company"],asset_ids=[asset.pk])
    company=make_company("other-company",active=False)
    with pytest.raises(PermissionDenied):
        preview_bulk_finance(actor=context["finance"],company=company,asset_ids=[asset.pk])
    assert not FinanceFormalizationRequest.objects.exists()


def imported_assets(ctx,settings,tmp_path):
    settings.MEDIA_ROOT=tmp_path/"media"
    settings.IMPORT_TEMP_ROOT=tmp_path/"imports"
    fixed=create_fixed_asset_category(actor=ctx["finance"],company=ctx["company"],
        data={"code":"BFIN","name":"设备","useful_life_months_default":60})
    cases=[("settled-residual","2005-07-01","2880.00","2736.00","144.00","144.00"),
           ("settled-zero","1995-05-01","20160.00","20160.00","0.00","0.00"),
           ("continuing","2021-10-01","12000.00","11210.00","790.00","600.00")]
    rows=[]
    for name,start,cost,ad,book,salvage in cases:
        row=physical_row(ctx["company"],ctx["category"],ctx["department"],ctx["employee"],ctx["location"],
            **{"资产名称":name,"序列号":name,"首次管理属性":"FA","购置日期":start,"达到可使用状态日期":start})
        add_finance_row(row,fixed_category=fixed,policy=ctx["policy"],cost=cost,opening_ad=ad,opening_book=book,
            **{"起算规则":"specified_date","指定起算日期":start,"资本化日期":start,"实际接续日":"2026-09-01",
               "残值方式":"amount","残值率":"","残值金额":salvage})
        rows.append(row)
    batch=upload_and_validate_import(actor=ctx["finance"],company=ctx["company"],import_type="asset_initialization",
        uploaded_file=asset_workbook_upload(ctx["company"],rows),idempotency_key="bulk-finance-import")
    assert batch.status=="validated",[r.errors_json for r in batch.rows.all()]
    confirm_import_batch(actor=ctx["finance"],batch=batch)
    assets=list(Asset.objects.order_by("asset_name"))
    for asset in assets:
        register_asset(actor=ctx["finance"],asset=asset,idempotency_key=str(asset.pk))
        asset.refresh_from_db()
    return batch,assets


def test_mixed_import_keeps_opening_balances_and_continues_depreciation(context,settings,tmp_path):
    batch,assets=imported_assets(context,settings,tmp_path)
    before=list(AssetDepreciationProfile.objects.order_by("pk").values("id","useful_life_months","opening_book_value","opening_actual_accumulated_depreciation","actual_continuation_date","salvage_amount"))
    qr=list(AssetQrIdentity.objects.order_by("pk").values())
    counters=list(SequenceCounter.objects.order_by("pk").values())
    p=preview(context,assets)
    assert p["ready_count"]==3
    assert {r["asset"].asset_name:r["details"]["progress"] for r in p["rows"]}=={
        "continuing":"未提足折旧","settled-residual":"已提足折旧","settled-zero":"已提足折旧"}
    assert p["totals"]["accumulated"]==Decimal("34106.00")
    assert confirm(context,p)["success_count"]==3
    assert before==list(AssetDepreciationProfile.objects.order_by("pk").values("id","useful_life_months","opening_book_value","opening_actual_accumulated_depreciation","actual_continuation_date","salvage_amount"))
    assert qr==list(AssetQrIdentity.objects.order_by("pk").values())
    assert counters==list(SequenceCounter.objects.order_by("pk").values())
    assert DepreciationEntry.objects.count()==3
    d=generate_depreciation_batch(actor=context["finance"],company=context["company"],period_start=date(2026,9,1),period_end=date(2026,10,1),idempotency_key="september")
    assert sum(x.planned_amount for x in d.items.all())==Decimal("190.00")
    assert d.items.filter(status="skipped").count()==2
    confirm_depreciation_batch(actor=context["finance"],batch=d,reason="确认9月折旧")
    assert DepreciationEntry.objects.filter(batch_item__batch=d).count()==1
    confirm(context,p)
    assert DepreciationEntry.objects.count()==4 and FinanceFormalizationRequest.objects.count()==3


def test_filters_all_matching_and_post_only_confirmation(context,settings,tmp_path,client):
    batch,assets=imported_assets(context,settings,tmp_path)
    unrelated=simple_asset(context,"unrelated")
    client.force_login(context["finance"])
    page=client.get(reverse("finance:pending-list"),{"import_batch":batch.pk})
    assert page.status_code==200 and page.context["page_obj"].paginator.count==3
    assert "核对全部筛选结果（3 项）" in page.content.decode()
    url=reverse("finance:bulk-confirmation")
    p=client.post(url,{"action":"preview","selection_scope":"filtered","import_batch":batch.pk})
    assert p.status_code==200 and p.context["preview"]["ready_count"]==3
    assert not FinanceFormalizationRequest.objects.exists()
    token=p.context["preview"]["token"]
    assert client.get(url,{"action":"confirm","token":token}).status_code==302
    assert client.post(url,{"action":"confirm","token":token}).status_code==400
    result=client.post(url,{"action":"confirm","token":token,"acknowledge":"on"})
    assert result.status_code==200 and result.context["result"]["success_count"]==3
    assert AssetFinance.objects.get(asset=unrelated).finance_confirmed_at is None
    assert client.get(reverse("finance:pending-list"),{"import_batch":batch.pk}).context["page_obj"].paginator.count==0
    client.force_login(context["equipment"])
    assert client.post(url,{"action":"preview","assets":[str(unrelated.pk)]}).status_code==403


def test_csrf_and_size_limit(context):
    asset=simple_asset(context)
    client=Client(enforce_csrf_checks=True)
    client.force_login(context["finance"])
    assert client.post(reverse("finance:bulk-confirmation"),{"action":"preview","assets":[str(asset.pk)]}).status_code==403
    import uuid
    with pytest.raises(ValidationError,match="200"):
        preview_bulk_finance(actor=context["finance"],company=context["company"],asset_ids=[uuid.uuid4() for _ in range(201)])


def test_two_simultaneous_confirmations_post_once(context):
    if connection.vendor!="postgresql": pytest.skip("PostgreSQL row locking")
    asset=simple_asset(context)
    p=preview(context,[asset])
    barrier=Barrier(2)
    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return confirm(context,p)
        finally: close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:run(),range(2)))
    assert all(r["success_count"]==1 for r in results)
    assert FinanceFormalizationRequest.objects.count()==1
    assert AuditLog.objects.filter(action="asset_finance_confirm").count()==1


def test_saved_new_fixed_asset_uses_policy_and_shows_resolved_dates(context):
    fixed=create_fixed_asset_category(actor=context["finance"],company=context["company"],
        data={"code":"NEW-FIXED","name":"设备","useful_life_months_default":60})
    asset=create_registered_asset(actor=context["equipment"],company=context["company"],
        data=physical_data(context,commissioning_date=date(2026,8,10)),idempotency_key="new-fixed")
    save_asset_finance_draft(actor=context["finance"],asset=asset,data={"accounting_treatment":"fixed_asset",
        "original_cost":Decimal("12000.00"),"fixed_asset_category":fixed,"capitalization_date":date(2026,8,10)})
    p=preview(context,[asset])
    assert p["ready_count"]==1,p["rows"]
    d=p["rows"][0]["details"]
    assert d["start"]==d["continuation"]==date(2026,9,1)
    assert d["life"]==60 and d["salvage"]==Decimal("600.00")
    assert confirm(context,p)["success_count"]==1
    profile=AssetDepreciationProfile.objects.get(asset=asset)
    assert profile.schedules.first().planned_amount==Decimal("190.00")


def test_policy_change_after_preview_requires_new_review(context,settings,tmp_path):
    batch,assets=imported_assets(context,settings,tmp_path)
    p=preview(context,assets)
    # A maintenance change to the referenced policy is part of the preview snapshot.
    type(context["policy"]).objects.filter(pk=context["policy"].pk).update(updated_at=timezone.now())
    result=confirm(context,p)
    assert result["success_count"]==0 and result["error_count"]==3
    assert all("变化" in row["error"] for row in result["rows"])
    assert not FinanceFormalizationRequest.objects.exists()
