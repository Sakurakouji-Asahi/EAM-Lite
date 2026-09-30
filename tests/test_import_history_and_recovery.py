"""Import follow-through, complete history and safe action retries."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.imports.services import upload_and_validate_import, confirm_import_batch
from apps.masterdata.models import Department
from apps.operations.models import BackupSet
from apps.supplies.models import SupplyDocument, SupplyStockBalance
from apps.supplies.services import post_supply_document, update_draft_document
from tests.test_sprint1_imports import setup_data, workbook_file
from tests.test_sprint14_imports import opening_workbook
from tests.test_sprint14_support import make_company, make_user, make_supply_category, make_supply_item, make_supply_warehouse
from tests.test_sprint7_support import active_asset_context
from tests.test_sprint7_lifecycle_services import _loan
from apps.assets.models import AssetLoan, AssetMovement

pytestmark = pytest.mark.django_db(transaction=True)


def test_import_errors_can_be_paged_without_changing_whole_batch_confirmation(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    company, actor = setup_data('system_admin')
    rows = [[f'GOOD{i}',f'正确部门{i}','','','是'] for i in range(6)]
    rows += [[f'BAD{i}',f'错误部门{i}','MISSING','','是'] for i in range(60)]
    batch = upload_and_validate_import(actor=actor,company=company,import_type='department',
                                       uploaded_file=workbook_file('department',rows),idempotency_key='history-invalid')
    client.force_login(actor)
    url = reverse('imports:batch_detail',args=[batch.pk])
    page = client.get(url,{'row_view':'errors','page':2})
    assert page.status_code == 200
    assert page.context['page_obj'].paginator.count == 60
    assert len(page.context['rows']) == 10
    assert all(row.errors_json and row.row_number >= 58 for row in page.context['rows'])
    assert 'row_view=errors' in page.context['pagination_query']
    assert page.context['batch'].total_rows == 66
    refused = client.post(reverse('imports:confirm',args=[batch.pk]),{'confirm':'1'},follow=True)
    batch.refresh_from_db()
    assert batch.status == 'invalid' and not Department.objects.filter(company=company).exists()
    missing_ack = client.post(reverse('imports:confirm',args=[batch.pk]),{},follow=True)
    assert missing_ack.content.decode().count('请勾选确认后再执行整批导入。') == 1


def test_opening_import_displays_current_posted_document_and_trace_link(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    company = make_company()
    actor = make_user('history-opening-finance','finance')
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company,'WH-HISTORY')
    item = make_supply_item(company,category,'HISTORY-ITEM')
    batch = upload_and_validate_import(actor=actor,company=company,import_type='opening_stock',
        uploaded_file=opening_workbook(company,[[company.code,warehouse.code,item.item_code,10,12,'','核验期初']]),
        idempotency_key='history-opening')
    confirm_import_batch(actor=actor,batch=batch)
    document = SupplyDocument.objects.get(company=company,document_type='opening')
    client.force_login(actor)
    url = reverse('imports:batch_detail',args=[batch.pk])
    original_line_id = document.lines.get().pk
    document = update_draft_document(actor=actor,document=document,data={'remark':'核对后补充说明'},
                                     lines=[{'item':item,'quantity':Decimal('10'),'entered_unit_cost':Decimal('12'),'line_remark':'已核对'}])
    assert document.lines.get().pk != original_line_id
    draft = client.get(url)
    assert draft.context['opening_stock_progress']['draft'] == 1
    assert draft.context['rows'][0].created_document.pk == document.pk
    post_supply_document(actor=actor,document=document)
    response = client.get(url)
    assert response.status_code == 200
    assert '当前库存仍未变化' not in response.content.decode()
    assert response.context['opening_stock_progress']['posted'] == 1
    assert response.context['opening_stock_progress']['draft'] == 0
    assert reverse('supplies:document-detail',args=[document.pk]) in response.content.decode()
    assert SupplyStockBalance.objects.get(item=item,warehouse=warehouse).quantity_on_hand == Decimal('10.0000')
    history = client.get(reverse('imports:home'),{'q':str(batch.pk),'import_type':'opening_stock','status':'confirmed'})
    assert history.status_code == 200
    assert [item.pk for item in history.context['recent_batches']] == [batch.pk]
    assert 'status=confirmed' in history.context['pagination_query']
    client.force_login(make_user('history-opening-equipment','equipment'))
    assert client.get(url).status_code == 403
    restricted = client.get(reverse('imports:home'),{'q':'opening-stock.xlsx'})
    assert restricted.status_code == 200 and not restricted.context['recent_batches']


def test_backup_history_keeps_records_beyond_first_hundred(client):
    company = make_company()
    actor = make_user('history-backup-admin','system_admin')
    now = timezone.now()
    BackupSet.objects.bulk_create([BackupSet(company=company,backup_set_id=f'HISTORY-{index:04d}',kind='manual',
        status='pending',request_hash='a'*64,idempotency_key=f'history-{index}',requested_by=actor,
        started_at=now-timedelta(minutes=index)) for index in range(105)])
    client.force_login(actor)
    response = client.get(reverse('operations:backup-list'),{'page':5})
    assert response.status_code == 200
    assert 'HISTORY-0104' in response.content.decode()
    assert response.context['page_obj'].paginator.count == 105
    assert len(response.context['backups']) == 5
    filtered = client.get(reverse('operations:backup-list'),{'q':'HISTORY-0104','kind':'manual','status':'pending'})
    assert filtered.context['page_obj'].paginator.count == 1
    client.force_login(make_user('history-backup-finance','finance'))
    assert client.get(reverse('operations:backup-list')).status_code == 403


def test_return_retry_does_not_duplicate_or_touch_a_new_loan(client):
    context, asset, _qr = active_asset_context('RETURNRETRY')
    first = _loan(context,asset,'return-retry-first')
    client.force_login(context['equipment'])
    url = reverse('assets:lifecycle-loan-return',args=[asset.pk])
    data = {'expected_status':'loaned','idempotency_key':'return-retry-submit',
            'returned_at':timezone.localdate().isoformat(),'received_by_employee':context['employee'].pk,
            'return_department':context['department'].pk,'return_responsible_employee':context['employee'].pk,
            'return_location':context['location'].pk,'return_asset_status':'in_use','remark':'完好归还'}
    rejected = client.post(url,{**data,'returned_at':(timezone.localdate()-timedelta(days=1)).isoformat()})
    assert rejected.status_code == 200 and rejected.context['form'].errors
    assert not AssetMovement.objects.filter(asset=asset,movement_type='loan_return').exists()
    initial = client.post(url,data)
    assert initial.status_code == 302, initial.context['form'].errors if initial.context else initial.content
    assert client.post(url,data).status_code == 302
    asset.refresh_from_db()
    second = _loan(context,asset,'return-retry-second')
    assert client.post(url,data).status_code == 302
    second.refresh_from_db()
    asset.refresh_from_db()
    assert second.status == 'active' and asset.asset_status == 'loaned'
    assert AssetMovement.objects.filter(asset=asset,movement_type='loan_return').count() == 1
    changed = client.post(url,{**data,'return_asset_status':'idle'})
    assert changed.status_code == 200 and changed.context['form'].errors
    assert AssetLoan.objects.filter(asset=asset,status='active').count() == 1
