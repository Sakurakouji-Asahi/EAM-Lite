from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.supplies.models import SupplyStockBalance, SupplyStockLedger
from apps.supplies.posting_preview import document_posting_preview
from apps.supplies.services import post_supply_document, publish_supply_count_task, record_supply_count
from tests.test_sprint15_services import supply_context, make_return
from tests.test_sprint15_support import seed_supply_stock, make_issue_document, make_user
from tests.test_sprint17_services import make_count

pytestmark = pytest.mark.django_db(transaction=True)


def test_preview_matches_posting_including_zero_cent_partial_return_and_does_not_write():
    company,actor,department,employee,source,target,item,_ = supply_context()
    seed_supply_stock(actor=actor,company=company,warehouse=source,item=item,quantity='5',unit_cost='0.006')
    issue = make_issue_document(actor=actor,company=company,warehouse=source,item=item,department=department,quantity='5')
    before = list(SupplyStockBalance.objects.values())
    count = SupplyStockLedger.objects.count()
    preview = document_posting_preview(actor=actor,document=issue)
    assert preview['total_amount'] == Decimal('0.03')
    assert list(SupplyStockBalance.objects.values()) == before and SupplyStockLedger.objects.count() == count
    post_supply_document(actor=actor,document=issue)
    for index,expected in enumerate(('0.01','0.00')):
        document = make_return(actor=actor,company=company,target=target,source_line=issue.lines.get(),quantity='1',key=f'preview-return-{index}')
        preview = document_posting_preview(actor=actor,document=document)
        assert preview['total_amount'] == Decimal(expected)
        post_supply_document(actor=actor,document=document)
        assert document.lines.get().posted_amount == Decimal(expected)
    assert document_posting_preview(actor=make_user('preview-employee','employee'),document=document) is None


def test_bulk_count_keeps_blank_distinct_from_zero_and_rejects_stale_or_outside_scope(client):
    company,actor,department,employee,source,target,first,second = supply_context()
    for index,item in enumerate((first,second)):
        seed_supply_stock(actor=actor,company=company,warehouse=source,item=item,quantity='5',unit_cost='10',key=f'bulk-seed-{index}')
    task = make_count(actor=actor,company=company,domain='warehouse_stock',warehouse=source,key='bulk-count')
    publish_supply_count_task(actor=actor,task=task)
    client.force_login(actor)
    url = reverse('supplies:count-task-bulk-entry',args=[task.pk])
    page = client.get(url)
    assert page.status_code == 200
    row_forms = page.context['row_forms']
    first_line, second_line = (row.line for row in row_forms)
    data = {'line_manifest':page.context['line_manifest'],f'line-{first_line.pk}-counted_quantity':'0',
            f'line-{first_line.pk}-remark':'现场已无实物',f'line-{first_line.pk}-expected_counted_at':''}
    assert client.post(url,data).status_code == 302
    first_line.refresh_from_db(); second_line.refresh_from_db()
    assert first_line.counted_quantity == Decimal('0') and second_line.counted_quantity is None
    audit_count = AuditLog.objects.filter(action='supply_count_record').count()
    assert client.post(url,data).status_code == 302
    assert AuditLog.objects.filter(action='supply_count_record').count() == audit_count
    stale = client.post(url,{**data,f'line-{first_line.pk}-counted_quantity':'1'})
    assert stale.status_code == 200 and '已被更新' in stale.content.decode()
    first_line.refresh_from_db(); assert first_line.counted_quantity == Decimal('0')
    fresh = client.get(url)
    versions = {str(row.line.pk):row['expected_counted_at'].value() for row in fresh.context['row_forms']}
    record_supply_count(actor=actor,line=second_line,counted_quantity=Decimal('5'),remark='')
    before_failed_save = AuditLog.objects.filter(action='supply_count_record').count()
    conflict = client.post(url,{'line_manifest':fresh.context['line_manifest'],
        f'line-{first_line.pk}-counted_quantity':'1',f'line-{first_line.pk}-remark':'重新清点',
        f'line-{first_line.pk}-expected_counted_at':versions[str(first_line.pk)],
        f'line-{second_line.pk}-counted_quantity':'3',f'line-{second_line.pk}-remark':'差异说明',
        f'line-{second_line.pk}-expected_counted_at':versions[str(second_line.pk)]})
    assert conflict.status_code == 200 and '已被更新' in conflict.content.decode()
    first_line.refresh_from_db(); second_line.refresh_from_db()
    assert first_line.counted_quantity == Decimal('0') and second_line.counted_quantity == Decimal('5')
    assert AuditLog.objects.filter(action='supply_count_record').count() == before_failed_save
    filtered = client.get(reverse('supplies:count-task-detail',args=[task.pk]),{'row_view':'unrecorded'})
    assert filtered.context['page_obj'].paginator.count == 0
    assert filtered.context['count_summary']['total'] == 2
    client.force_login(make_user('count-outside','employee'))
    assert client.post(url,data).status_code in (403,404)


def test_single_count_save_next_preserves_filter_and_allows_explicit_zero(client):
    company,actor,department,employee,source,target,first,second = supply_context()
    for index,item in enumerate((first,second)):
        seed_supply_stock(actor=actor,company=company,warehouse=source,item=item,quantity='5',unit_cost='10',key=f'next-seed-{index}')
    task = make_count(actor=actor,company=company,domain='warehouse_stock',warehouse=source,key='next-count')
    publish_supply_count_task(actor=actor,task=task)
    rows = list(task.lines.order_by('item_code_snapshot'))
    client.force_login(actor)
    response = client.post(reverse('supplies:count-line-record',args=[task.pk,rows[0].pk]),{
        'counted_quantity':'0','remark':'盘亏','next_action':'next','return_query':'row_view=unrecorded','expected_counted_at':''})
    assert response.status_code == 302
    assert reverse('supplies:count-line-record',args=[task.pk,rows[1].pk]) in response.url
    assert 'return_query=row_view%3Dunrecorded' in response.url
