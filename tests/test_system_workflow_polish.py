"""Business-visible zero values and operational search/validation regressions."""

from datetime import date, timedelta
from decimal import Decimal
import re

import pytest
from django.urls import reverse
from django.utils import timezone
from django.template.loader import render_to_string
from django.db import transaction

from apps.supplies.services import create_supply_count_task, publish_supply_count_task, record_supply_count
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import seed_supply_stock
from tests.test_sprint7_support import active_fixed_asset_context
from tests.test_sprint7_disposal_services import _initiate, _record_and_lock
from tests.test_sprint3_support import make_user


@pytest.mark.django_db(transaction=True)
def test_disposal_snapshot_distinguishes_recorded_zero_and_missing(client):
    context, asset, *_ = active_fixed_asset_context('ALLZERO')
    disposal = _initiate(context, asset, 'all-zero')
    client.force_login(context['finance'])
    url = reverse('assets:disposal-detail', args=[disposal.pk])
    response = client.get(url)
    draft = response.content.decode()
    assert re.search(r'累计折旧</dt><dd[^>]*>\s*—', draft)
    # Construct zero snapshot values only for presentation; do not bypass the
    # financial service's date/confirmation rules or save financial records.
    for field in ('original_cost_snapshot','actual_accumulated_depreciation_snapshot','impairment_snapshot','book_value_snapshot','disposal_income'):
        setattr(disposal, field, Decimal('0.00'))
    html = render_to_string('assets/disposal_detail.html',
                            {'asset':asset,'disposal':disposal,'can_financial':True,'actions':{},'attachments':[]},
                            request=response.wsgi_request)
    for label in ('累计折旧', '减值', '处置收入'):
        assert re.search(label + r'</dt><dd[^>]*>\s*0\.00', html), label


@pytest.mark.django_db(transaction=True)
def test_zero_cost_document_and_zero_count_difference_are_visible(client):
    company, actor, department, employee, warehouse, _category, item, _ = supply_context()
    document = seed_supply_stock(actor=actor, company=company, warehouse=warehouse,
                                 item=item, quantity='5', unit_cost='0', key='all-zero-stock')
    client.force_login(actor)
    response = client.get(reverse('supplies:document-detail', args=[document.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert '0.000000' in html
    assert '0.00' in html
    task = create_supply_count_task(actor=actor,company=company,data={'name':'零差异盘点','count_domain':'warehouse_stock',
                                    'warehouse':warehouse,'planned_start':date(2026,8,26),'planned_end':date(2026,8,27),
                                    'idempotency_key':'all-zero-count'})
    publish_supply_count_task(actor=actor, task=task)
    line = task.lines.get(item=item)
    record_supply_count(actor=actor, line=line, counted_quantity=Decimal('5'), remark='')
    page = client.get(reverse('supplies:count-task-detail',args=[task.pk]))
    assert re.search(r'<td class="text-end ">0\.0000 ' + re.escape(item.unit) + r'</td>', page.content.decode())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('route', ['document-list','stock-ledger-list','count-task-list','custody-list'])
def test_invalid_operational_date_is_an_error_not_an_empty_success(client, route):
    _, actor, *_ = supply_context()
    client.force_login(actor)
    response = client.get(reverse('supplies:' + route), {'date_from':'2026-02-30'})
    assert response.status_code == 400
    assert response.context['filter_errors']
    assert '开始日期' in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_batch_search_uses_month_overlap_status_and_exact_item_counts(client):
    from apps.finance.models import DepreciationBatch
    from tests.test_sprint4_services import _profile_context, _confirmed_entry
    company, actor, *_rest, profile = _profile_context()
    with transaction.atomic():
        confirmed, _item, _entry = _confirmed_entry(profile=profile,start=date(2024,1,1),amount=Decimal('190.00'),actor=actor)
    draft = DepreciationBatch.objects.create(company=company,period_start=date(2024,2,1),period_end=date(2024,3,1),
                                             generation_no=1,batch_type='regular',status='draft',
                                             idempotency_key='sys-draft-batch',request_hash='a'*64,
                                             generated_by=actor,generated_at=timezone.now())
    client.force_login(actor)
    url = reverse('finance:batch-list')
    january = client.get(url,{'period':'2024-01','status':'confirmed','page_size':50})
    assert january.status_code == 200
    assert [item.pk for item in january.context['batches']] == [confirmed.pk]
    assert january.context['batches'][0].item_count == 1
    assert january.context['page_obj'].paginator.per_page == 50
    february = client.get(url,{'period':'2024-02','status':'draft'})
    assert [item.pk for item in february.context['batches']] == [draft.pk]
    assert february.context['batches'][0].item_count == 0
    assert client.get(url, {'period':'2024-13'}).status_code == 400
    client.force_login(make_user('sys-no-finance','equipment'))
    assert client.get(url,{'period':'2024-01'}).status_code == 403


@pytest.mark.django_db(transaction=True)
def test_maintenance_search_by_equipment_and_completion_date_keeps_scope(client):
    from tests.test_sprint9_support import maintenance_context
    from tests.test_sprint9_http import _complete
    context = maintenance_context('SYSRECORD')
    context['asset'].equipment_number = 'EQ-VERIFY-88'
    context['asset'].save(update_fields=['equipment_number'])
    record = _complete(context,'sys-record')
    client.force_login(context['equipment'])
    url = reverse('maintenance:record-list')
    result = client.get(url,{'q':'EQ-VERIFY-88','date_from':record.completed_date.isoformat(),
                             'date_to':record.completed_date.isoformat(),'status':'confirmed','page_size':50})
    assert result.status_code == 200
    assert [item.pk for item in result.context['records']] == [record.pk]
    assert result.context['page_obj'].paginator.per_page == 50
    assert client.get(url,{'date_to':(record.completed_date-timedelta(days=1)).isoformat()}).context['page_obj'].paginator.count == 0
    assert client.get(url,{'date_from':'2026-09-30','date_to':'2026-09-01'}).status_code == 400
    assert client.get(reverse('maintenance:plan-list'),{'q':'EQ-VERIFY-88'}).context['page_obj'].paginator.count == 1
    client.force_login(make_user('sys-record-outsider','employee'))
    denied_rows = client.get(url,{'q':'EQ-VERIFY-88'})
    assert denied_rows.status_code == 200 and denied_rows.context['page_obj'].paginator.count == 0


@pytest.mark.django_db(transaction=True)
def test_count_resolution_rejects_unrecorded_line_and_shows_positive_action_quantity(client):
    from tests.test_sprint17_services import issued_custody, make_count
    from apps.supplies.services import stop_supply_count_entry
    from apps.supplies.models import SupplyCustodyMovement
    company, _, department, _employee, _source, _target, _item, custody = issued_custody()
    actor = make_user('sys-count-equipment','equipment')
    task = make_count(actor=actor,company=company,domain='custody',department=department,key='sys-count-resolution')
    publish_supply_count_task(actor=actor,task=task)
    line = task.lines.get(custody=custody)
    client.force_login(actor)
    url = reverse('supplies:count-line-resolve',args=[task.pk,line.pk])
    data = {'resolution_type':'loss','business_date':timezone.localdate().isoformat(),'reason':'核验盘亏一把',
            'idempotency_key':'sys-count-loss'}
    before = SupplyCustodyMovement.objects.count()
    refused = client.post(url,data)
    assert refused.status_code == 200 and '尚未录入实盘数量' in refused.content.decode()
    assert SupplyCustodyMovement.objects.count() == before
    record_supply_count(actor=actor,line=line,counted_quantity=Decimal('2'),remark='少一把')
    stop_supply_count_entry(actor=actor,task=task)
    page = client.get(url)
    assert page.context['resolution_quantity'] == Decimal('1.0000')
    assert '本次处理数量：1.0000' in page.content.decode()
    assert client.post(url,data).status_code == 302
    custody.refresh_from_db()
    assert custody.current_quantity == Decimal('2.0000')
    movement = SupplyCustodyMovement.objects.get(idempotency_key='sys-count-loss')
    assert movement.quantity == Decimal('1.0000')
