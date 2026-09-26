from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError, PermissionDenied
from django.urls import reverse

from apps.finance.models import AssetFinance, AssetDepreciationProfile, DepreciationEntry, DepreciationSchedule
from apps.finance.services import save_asset_finance_draft, create_fixed_asset_category
from apps.finance.confirmation_initial import finance_confirmation_initial
from apps.finance.bulk_confirmation import preview_bulk_finance, confirm_bulk_finance
from apps.assets.registration import create_registered_asset
from apps.audit.models import AuditLog
from tests.test_unified_asset_identity import context, physical_data
from tests.test_bulk_finance_confirmation import imported_assets

pytestmark = pytest.mark.django_db(transaction=True)


def values(ctx):
    fixed = create_fixed_asset_category(actor=ctx["finance"], company=ctx["company"],
        data={"code":"USAB","name":"机器设备","useful_life_months_default":60})
    finance = {"accounting_treatment":"fixed_asset","original_cost":Decimal("12000.00"),
        "fixed_asset_category":fixed,"capitalization_date":date(2026,8,1)}
    profile = {"depreciation_policy":ctx["policy"],"useful_life_months":72,"salvage_mode":"amount",
        "salvage_amount":Decimal("600.00"),"start_rule":"specified_date","specified_start":date(2026,9,1),
        "actual_continuation_date":date(2026,9,1),"opening_actual_accumulated_depreciation":Decimal("0.00"),
        "opening_impairment":Decimal("0.00")}
    asset = create_registered_asset(actor=ctx["equipment"], company=ctx["company"],
        data=physical_data(ctx,commissioning_date=date(2026,8,1)),idempotency_key="usability-asset")
    return asset,finance,profile


def test_save_retains_parameters_and_creates_no_posted_data(context):
    asset,finance,profile = values(context)
    code = asset.asset_code
    save_asset_finance_draft(actor=context["finance"],asset=asset,data=finance,profile_data=profile)
    saved = AssetDepreciationProfile.objects.get(asset=asset)
    assert saved.status=="draft" and saved.useful_life_months==72
    assert saved.salvage_amount==Decimal("600.00")
    assert saved.opening_book_value==Decimal("12000.00")
    assert not DepreciationEntry.objects.exists() and not DepreciationSchedule.objects.exists()
    assert AssetFinance.objects.get(asset=asset).finance_confirmed_at is None
    profile["useful_life_months"] = 84
    save_asset_finance_draft(actor=context["finance"],asset=asset,data=finance,profile_data=profile)
    saved.refresh_from_db()
    assert saved.useful_life_months==84 and AssetDepreciationProfile.objects.count()==1
    assert finance_confirmation_initial(asset)["useful_life_months"]==84
    p = preview_bulk_finance(actor=context["finance"],company=context["company"],asset_ids=[asset.pk])
    assert p["ready_count"]==1 and p["rows"][0]["details"]["life"]==84
    assert confirm_bulk_finance(actor=context["finance"],company=context["company"],token=p["token"],acknowledged=True)["success_count"]==1
    saved.refresh_from_db()
    assert saved.status=="active" and saved.useful_life_months==84
    asset.refresh_from_db()
    assert asset.asset_code==code
    with pytest.raises(ValidationError):
        save_asset_finance_draft(actor=context["finance"],asset=asset,data=finance,profile_data=profile)


def test_invalid_profile_rolls_back_finance_change(context):
    asset,finance,profile=values(context)
    save_asset_finance_draft(actor=context["finance"],asset=asset,data=finance,profile_data=profile)
    finance["original_cost"]=Decimal("15000.00")
    profile["salvage_amount"]=Decimal("20000.00")
    audit_count=AuditLog.objects.count()
    with pytest.raises((ValidationError,ValueError)):
        save_asset_finance_draft(actor=context["finance"],asset=asset,data=finance,profile_data=profile)
    assert AssetFinance.objects.get(asset=asset).original_cost==Decimal("12000.00")
    assert AssetDepreciationProfile.objects.get(asset=asset).salvage_amount==Decimal("600.00")
    assert AuditLog.objects.count()==audit_count


def test_unconfirmed_recognition_can_be_corrected_with_audit(context):
    asset,finance,profile=values(context)
    save_asset_finance_draft(actor=context["finance"],asset=asset,data=finance,profile_data=profile)
    finance.update(accounting_treatment="controlled_non_fixed",fixed_asset_category=None,capitalization_date=None,
                   accounting_treatment_reason="按实际用途更正未确认认定")
    save_asset_finance_draft(actor=context["finance"],asset=asset,data=finance,profile_data={})
    assert not AssetDepreciationProfile.objects.exists() and not DepreciationEntry.objects.exists()
    assert AuditLog.objects.filter(action="depreciation_profile_draft_remove").exists()
    with pytest.raises(PermissionDenied):
        save_asset_finance_draft(actor=context["equipment"],asset=asset,data=finance,profile_data={})


def test_http_save_and_manual_preview(context,client):
    asset,finance,profile=values(context)
    client.force_login(context["finance"])
    data={"action":"save","accounting_treatment":"fixed_asset","original_cost":"12000.00",
        "fixed_asset_category":finance["fixed_asset_category"].pk,"capitalization_date":"2026-08-01",
        "depreciation_policy":context["policy"].pk,"useful_life_months":"72","salvage_mode":"amount",
        "salvage_amount":"600.00","method":"manual","start_rule":"specified_date",
        "specified_start_date":"2026-09-01","actual_continuation_date":"2026-09-01",
        "opening_actual_accumulated_depreciation":"0.00","opening_impairment":"0.00"}
    url=reverse("finance:finance-confirm",args=[asset.pk])
    response=client.post(url,data)
    assert response.status_code==302
    page=client.get(url)
    assert page.status_code==200 and page.context["form"].initial["useful_life_months"]==72
    assert "会计认定与金额" in page.content.decode() and "期初余额与接续" in page.content.decode()
    trial=client.post(reverse("finance:finance-preview",args=[asset.pk]),data)
    assert trial.status_code==200 and not trial.context["form"].errors
    assert "后续按期录入" in trial.content.decode()
    assert not DepreciationEntry.objects.exists()


def test_import_page_reflects_real_progress_without_raw_internal_fields(context,settings,tmp_path,client):
    batch,assets=imported_assets(context,settings,tmp_path)
    client.force_login(context["finance"])
    response=client.get(reverse("imports:batch_detail",args=[batch.pk]))
    assert response.status_code==200
    progress=response.context["import_progress"]
    assert progress["drafts"]==0 and progress["registered"]==3 and progress["finance_pending"]==3
    content=response.content.decode()
    assert "所有导入记录仍为草稿" not in content
    assert "批量财务确认" in content and "查看原行" in content
    assert "normalized_data_json" not in content and "profile_data" not in content
    home=client.get(reverse("imports:home"))
    assert home.status_code==200 and batch.pk in [item.pk for item in home.context["recent_batches"]]
    assert reverse("imports:batch_detail",args=[batch.pk]) in home.content.decode()


def test_page_size_and_menu_search_do_not_expand_permissions(context,client):
    asset,finance,profile=values(context)
    client.force_login(context["finance"])
    page=client.get(reverse("assets:asset-list"),{"page_size":"100"})
    assert page.context["page"].paginator.per_page==100
    assert "data-menu-search" in page.content.decode()
    financial=client.get(reverse("finance:pending-list"),{"page_size":"200"})
    assert financial.context["page_obj"].paginator.per_page==200
    client.force_login(context["equipment"])
    assert client.get(reverse("finance:pending-list")).status_code==403


def test_parent_department_filter_includes_child_group(context,client):
    from tests.test_sprint3_support import make_department,make_employee
    child=make_department(context["company"],"TEAM",parent=context["department"])
    employee=make_employee(context["company"],child,"TEAM-E")
    asset=create_registered_asset(actor=context["equipment"],company=context["company"],
        data=physical_data(context,department=child,responsible_employee=employee),idempotency_key="child-asset")
    client.force_login(context["finance"])
    response=client.get(reverse("assets:asset-list"),{"department":context["department"].pk})
    assert response.status_code==200 and list(response.context["page"].object_list)==[asset]
    assert context["department"].pk in [dept.pk for dept in response.context["departments"]]
    response=client.get(reverse("finance:pending-list"),{"department":context["department"].pk})
    assert response.status_code==200 and response.context["page_obj"].paginator.count==1


def test_import_history_respects_existing_batch_visibility(context,settings,tmp_path,client):
    from tests.test_sprint3_support import make_user
    batch,_=imported_assets(context,settings,tmp_path)
    outsider=make_user('other-importer','equipment')
    client.force_login(outsider)
    response=client.get(reverse('imports:home'))
    assert response.status_code==200
    assert batch.pk not in [item.pk for item in response.context['recent_batches']]
    assert client.get(reverse('imports:batch_detail',args=[batch.pk])).status_code==403


def test_basic_finance_can_still_be_saved_before_policy_setup():
    from tests.test_sprint4_acceptance import _base_context
    from tests.test_asset_registration_finance_separation import registered
    ctx=_base_context('BASIC-SAVE',include_policy=False)
    asset=registered(ctx)
    fixed=create_fixed_asset_category(actor=ctx['finance'],company=ctx['company'],
        data={'code':'BASIC','name':'设备','useful_life_months_default':60})
    save_asset_finance_draft(actor=ctx['finance'],asset=asset,data={'accounting_treatment':'fixed_asset',
        'original_cost':Decimal('12000.00'),'fixed_asset_category':fixed,'capitalization_date':date(2026,8,1)},
        profile_data={'opening_actual_accumulated_depreciation':Decimal('0.00'),'opening_impairment':Decimal('0.00')})
    assert AssetFinance.objects.get(asset=asset).original_cost==Decimal('12000.00')
    assert not AssetDepreciationProfile.objects.exists() and not DepreciationEntry.objects.exists()
