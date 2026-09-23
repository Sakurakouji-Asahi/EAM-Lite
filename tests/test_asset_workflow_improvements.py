from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.query import QuerySet
from django.urls import reverse
from django.utils import timezone

from apps.assets.bulk_registration import preview_bulk_registration, confirm_bulk_registration
from apps.assets.custody_services import return_custody_asset, reverse_custody_return
from apps.assets.models import (Asset, AssetCustodyReturn, AssetCustodyReturnReversal, AssetMovement,
                                AssetOriginLink, AssetOriginReversal, AssetRegistration)
from apps.assets.services import create_asset_draft
from apps.assets.trace_services import record_asset_origin, reverse_asset_origin
from apps.finance.models import AssetFinance, DepreciationEntry
from apps.maintenance.services import create_maintenance_plan
from apps.masterdata.models import IssuedCode
from apps.reports.queries import build_dashboard, build_report_dataset
from tests.test_unified_asset_identity import context, registered, physical_data
from tests.test_unified_asset_identification import confirm
from tests.test_sprint3_support import make_user

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def business_time(day):
    instant = datetime(2026, 9, day, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    original_now = timezone.now
    ticks = iter(range(1000000))
    def current_time():
        return instant + timedelta(microseconds=next(ticks))
    with ExitStack() as stack:
        for model in apps.get_models():
            for field in model._meta.concrete_fields:
                if field.default is original_now:
                    stack.enter_context(patch.object(field, "_get_default", current_time))
        stack.enter_context(patch("django.utils.timezone.now", side_effect=current_time))
        yield


def receipt(context, asset, *, returned_on=None, key="return-1"):
    return return_custody_asset(actor=context["equipment"], asset=asset,
        returned_on=returned_on or timezone.localdate(), counterparty="测试出租方", contract_reference="TEST-LEASE-01",
        acceptance_evidence="测试交接单 TEST-RETURN-01", reason="租期结束归还", idempotency_key=key)


def undo(context, item, key="return-reverse-1"):
    return reverse_custody_return(actor=context["equipment"], custody_return=item,
        reason="核对交接单发现登记错误，撤销后重新登记", idempotency_key=key)


def planned(context):
    asset = registered(context, management_attribute="LS", is_maintenance_required=True)
    confirm(context, asset, "alternative")
    asset.refresh_from_db()
    plan = create_maintenance_plan(actor=context["equipment"], company=context["company"], asset=asset,
        name="测试租入设备保养", cycle_value=1, cycle_unit="month", responsible_employee=context["employee"],
        advance_notice_days=3, standard_content="检查传动和电气", first_due_date=timezone.localdate())
    return asset, plan


def test_return_and_reversal_sync_plans_keep_identity_and_finance_empty(context):
    asset, plan = planned(context)
    code, qr = asset.asset_code, asset.qr_identities.get(status="active").pk
    item = receipt(context, asset)
    asset.refresh_from_db(); plan.refresh_from_db()
    dashboard = build_dashboard(actor=context["equipment"], company=context["company"])
    assert asset.get_asset_status_display() == "已归还" and plan.status == "ended"
    assert dashboard["pending"]["maintenance_upcoming"] == 0
    assert dashboard["pending"]["maintenance_overdue"] == 0
    reversal = undo(context, item)
    assert undo(context, item).pk == reversal.pk
    asset.refresh_from_db(); plan.refresh_from_db()
    assert asset.asset_status == "in_use" and plan.status == "active" and plan.ended_at is None
    assert asset.custody_return_record is None and asset.asset_code == code
    assert asset.qr_identities.get(status="active").pk == qr
    assert not AssetFinance.objects.exists() and not DepreciationEntry.objects.exists()
    second = receipt(context, asset, key="return-corrected")
    assert second.pk != item.pk and AssetCustodyReturn.objects.filter(asset=asset).count() == 2
    assert AssetCustodyReturnReversal.objects.count() == 1 and IssuedCode.objects.count() == 1


def test_backdated_return_and_void_use_business_date_in_history():
    with business_time(10):
        ctx = context.__wrapped__()
        asset = registered(ctx, management_attribute="LS")
        confirm(ctx, asset, "alternative")
    with business_time(17):
        item = receipt(ctx, asset, returned_on=date(2026, 9, 15))
        def report(day):
            return build_report_dataset(actor=ctx["equipment"], company=ctx["company"], report_key="asset_ledger",
                filters={"as_of_date":date(2026, 9, day), "asset_scope":"managed", "include_disposed":False})
        assert len(report(14).rows) == 1
        assert len(report(15).rows) == len(report(16).rows) == 0
        undo(ctx, item)
        assert len(report(16).rows) == 1
        receipt(ctx, asset, returned_on=date(2026, 9, 16), key="corrected-date")
        assert len(report(15).rows) == 1 and len(report(16).rows) == 0


def test_return_cannot_predate_latest_asset_operation(context):
    from datetime import timedelta
    asset = registered(context, management_attribute="LS")
    confirm(context, asset, "alternative")
    with pytest.raises(ValidationError, match="最近一次"):
        receipt(context, asset, returned_on=timezone.localdate()-timedelta(days=1))
    assert not AssetCustodyReturn.objects.exists()


@pytest.mark.parametrize("attribute,method,label", [("IA","electronic","电子台账已确认"), ("FA","alternative","替代标识已确认")])
def test_list_and_detail_show_same_identification(client, context, attribute, method, label):
    asset = registered(context, management_attribute=attribute)
    qr = confirm(context, asset, method)
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:asset-list"))
    assert response.status_code == 200
    row = next(item for item in response.context["page"] if item.pk == asset.pk)
    assert row.current_label_display == qr.get_label_status_display() == label


def test_returned_export_display_and_correction_http(client, context):
    asset = registered(context, management_attribute="LS")
    item = receipt(context, asset)
    dataset = build_report_dataset(actor=context["equipment"], company=context["company"], report_key="asset_ledger",
                                   filters={"asset_list_filters":{}})
    assert dataset.rows[0]["asset_status"] == "已归还"
    client.force_login(context["equipment"])
    url = reverse("assets:custody-return-reverse", args=[asset.pk, item.pk])
    assert client.get(url).status_code == 200
    data={"idempotency_key":"return-http-undo", "reason":"核对后更正", "confirmed":"on"}
    assert client.post(url,data).status_code == 302
    assert client.post(url,data).status_code == 302
    asset.refresh_from_db()
    assert asset.asset_status == "pending_label"
    page = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert page.status_code == 200 and "核对后更正" in page.content.decode()


def test_origin_reversal_retains_history_allows_corrected_edge(context):
    source, target = registered(context, "source"), registered(context, "target")
    values={"actor":context["equipment"], "source":source, "target":target, "relation_type":"split", "reason":"原拆分记录"}
    edge = record_asset_origin(**values, idempotency_key="origin-1")
    reversal = reverse_asset_origin(actor=context["equipment"], origin=edge, reason="原来源选择错误", idempotency_key="origin-undo")
    assert reverse_asset_origin(actor=context["equipment"], origin=edge, reason="原来源选择错误", idempotency_key="origin-undo").pk == reversal.pk
    corrected = record_asset_origin(**values, idempotency_key="origin-2")
    assert corrected.pk != edge.pk and AssetOriginLink.objects.count() == 2
    with pytest.raises(ValidationError, match="已经登记"):
        record_asset_origin(**values, idempotency_key="origin-3")
    assert AssetOriginReversal.objects.count() == 1


def test_reversal_permissions_and_append_only(context):
    asset=registered(context, management_attribute="LS")
    item=receipt(context,asset)
    outsider=make_user("review-unprivileged","employee")
    with pytest.raises(PermissionDenied):
        reverse_custody_return(actor=outsider,custody_return=item,reason="无权撤销",idempotency_key="no-right")
    reversal=undo(context,item)
    with pytest.raises(ValidationError):
        reversal.delete()
    if connection.vendor == "postgresql":
        with pytest.raises(IntegrityError), transaction.atomic():
            QuerySet.update(AssetCustodyReturnReversal.objects.filter(pk=reversal.pk), reason="覆盖历史")


def drafts(ctx):
    good=create_asset_draft(actor=ctx["equipment"],company=ctx["company"],data=physical_data(ctx,asset_name="完整草稿"))
    incomplete=create_asset_draft(actor=ctx["equipment"],company=ctx["company"],data=physical_data(ctx,asset_name="缺责任人",responsible_employee=None))
    return good,incomplete


def test_bulk_preview_skips_incomplete_rows_and_replays_without_new_codes(context):
    good,incomplete=drafts(context)
    preview=preview_bulk_registration(actor=context["equipment"],company=context["company"],asset_ids=[good.pk,incomplete.pk])
    assert preview["ready_count"] == preview["error_count"] == 1
    assert "责任人：" in preview["rows"][1]["error"]
    assert not IssuedCode.objects.exists() and not AssetRegistration.objects.exists()
    result=confirm_bulk_registration(actor=context["equipment"],company=context["company"],token=preview["token"])
    assert result["success_count"] == 1 and result["error_count"] == 0
    replay=confirm_bulk_registration(actor=context["equipment"],company=context["company"],token=preview["token"])
    assert replay["success_count"] == 1 and IssuedCode.objects.count() == AssetRegistration.objects.count() == 1
    incomplete.refresh_from_db()
    assert incomplete.asset_status == "draft" and not incomplete.asset_code
    assert not AssetFinance.objects.exists() and not DepreciationEntry.objects.exists()


def test_bulk_rechecks_changed_rows_and_preserves_other_success(context):
    first, _=drafts(context)
    second=create_asset_draft(actor=context["equipment"],company=context["company"],data=physical_data(context,asset_name="第二件"))
    preview=preview_bulk_registration(actor=context["equipment"],company=context["company"],asset_ids=[first.pk,second.pk])
    Asset.objects.filter(pk=first.pk).update(asset_name="预览后修改")
    result=confirm_bulk_registration(actor=context["equipment"],company=context["company"],token=preview["token"])
    assert result["success_count"] == result["error_count"] == 1
    assert "变更" in result["rows"][0]["error"]
    assert IssuedCode.objects.count() == 1


def test_bulk_token_is_bound_to_actor_and_rejects_tampering(context):
    good,_=drafts(context)
    preview=preview_bulk_registration(actor=context["equipment"],company=context["company"],asset_ids=[good.pk])
    for actor,token in [(context["finance"],preview["token"]),(context["equipment"],preview["token"]+"x")]:
        with pytest.raises(ValidationError,match="预览"):
            confirm_bulk_registration(actor=actor,company=context["company"],token=token)
    assert not IssuedCode.objects.exists()


def test_bulk_browser_form_preview_confirm_and_duplicate_post(client, context):
    good,incomplete=drafts(context)
    client.force_login(context["equipment"])
    url=reverse("assets:bulk-registration")
    assert client.get(url).status_code == 200
    page=client.post(url,{"action":"preview","assets":[str(good.pk),str(incomplete.pk)]})
    assert page.status_code == 200 and page.context["preview"]["ready_count"] == 1
    data={"action":"confirm","token":page.context["preview"]["token"]}
    result=client.post(url,data)
    assert result.status_code == 200 and result.context["result"]["success_count"] == 1
    assert client.post(url,data).context["result"]["success_count"] == 1
    assert IssuedCode.objects.count() == 1


def test_confirmed_excel_batch_opens_only_its_drafts(client, context, settings, tmp_path):
    from tests.test_project_improvements import validated, import_row
    from apps.imports.services import confirm_import_batch
    settings.MEDIA_ROOT=tmp_path/"media"
    batch=validated(context,import_row(context))
    confirm_import_batch(actor=context["equipment"],batch=batch)
    create_asset_draft(actor=context["equipment"],company=context["company"],data=physical_data(context,asset_name="另一批之外"))
    client.force_login(context["equipment"])
    page=client.get(reverse("assets:bulk-registration"),{"import_batch":str(batch.pk)})
    assert page.status_code == 200 and page.context["page"].paginator.count == 1
    asset=next(iter(page.context["page"]))
    assert asset.asset_name != "另一批之外"
    preview=client.post(reverse("assets:bulk-registration"),{"action":"preview","assets":[str(asset.pk)]})
    result=client.post(reverse("assets:bulk-registration"),{"action":"confirm","token":preview.context["preview"]["token"]})
    assert result.context["result"]["success_count"] == 1


def test_concurrent_bulk_replay_creates_one_identity(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL locks")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections
    good,_=drafts(context)
    preview=preview_bulk_registration(actor=context["equipment"],company=context["company"],asset_ids=[good.pk])
    barrier=Barrier(2)
    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return confirm_bulk_registration(actor=context["equipment"],company=context["company"],token=preview["token"])["success_count"]
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(submit) for _ in range(2)]
        assert [future.result(timeout=30) for future in futures] == [1,1]
    assert AssetRegistration.objects.count() == IssuedCode.objects.count() == 1


def test_component_cannot_reopen_under_returned_parent(context):
    parent=registered(context,"parent",management_attribute="LS")
    child=registered(context,"child",component_of=parent,management_attribute="")
    child_return=receipt(context,child,key="child-return")
    parent_return=receipt(context,parent,key="parent-return")
    with pytest.raises(ValidationError,match="主资产"):
        undo(context,child_return)
    undo(context,parent_return,key="parent-undo")
    undo(context,child_return,key="child-undo")
    child.refresh_from_db()
    assert child.asset_status == "pending_label"
