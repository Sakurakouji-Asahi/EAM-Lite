import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import activate_asset, set_asset_idle
from apps.assets.trace_services import record_asset_composition
from apps.reports.models import ExportLog
from apps.reports.services import create_or_correct_external_reference
from apps.offboarding.services import initiate_clearance
from tests.test_sprint7_support import active_asset_context
from tests.test_sprint8_support import add_active_asset
from tests.test_unified_asset_identity import context, registered
from tests.test_sprint10_support import offboarding_context, formal_asset

pytestmark = pytest.mark.django_db(transaction=True)


def test_asset_movement_history_pages_past_25_without_losing_old_records(client):
    ctx,asset,_ = active_asset_context('FULLHISTORY')
    original_count = asset.movements.count()
    for index in range(28):
        action = set_asset_idle if index % 2 == 0 else activate_asset
        action(actor=ctx['equipment'],asset=asset,effective_at=timezone.now(),reason=f'历史核验 {index}',idempotency_key=f'movement-history-{index}')
        asset.refresh_from_db()
    client.force_login(ctx['equipment'])
    url = reverse('assets:asset-detail',args=[asset.pk])
    page1 = client.get(url)
    page2 = client.get(url,{'movement_page':2})
    assert page1.status_code == page2.status_code == 200
    assert page1.context['movement_page'].paginator.count == original_count + 28
    assert len(page2.context['movements']) == original_count + 3
    assert 'movement_page=2#movement-history' in page1.content.decode()
    assert '历史核验 0' in page2.content.decode()


def test_combination_history_keeps_all_versions_and_their_member_contents(client,context):
    asset = registered(context,unit='组',management_attribute='LV')
    for index in range(12):
        record_asset_composition(actor=context['equipment'],asset=asset,members=[{'name':f'第 {index} 版成员','quantity':'2','unit':'把'}],
                                 reason=f'版本 {index}',idempotency_key=f'composition-all-{index}')
    client.force_login(context['equipment'])
    page = client.get(reverse('assets:asset-detail',args=[asset.pk]),{'composition_page':2})
    assert page.status_code == 200
    assert page.context['composition_page'].paginator.count == 12
    assert '第 0 版成员' in page.content.decode()
    assert 'composition_page=1#composition-history' in page.content.decode()


def test_tplus_history_pagination_is_independent_of_preview_and_mapping_filter(client):
    ctx,first,_ = active_asset_context('TPLUSALL')
    second,_ = add_active_asset(ctx,'TPLUSALL-SECOND')
    second.equipment_number = 'EQ-NEEDS-CARD'
    second.save(update_fields=['equipment_number'])
    create_or_correct_external_reference(actor=ctx['finance'],asset=first,reference_value='CARD-ONE',reason='人工对账')
    ExportLog.objects.bulk_create([ExportLog(company=ctx['company'],export_type='tplus_reconciliation',
        filters_json={'period':'2026-09' if index%2 else '2026-08'},status='pending',requested_by=ctx['finance'],
        request_hash='a'*64,idempotency_key=f'history-full-{index}') for index in range(56)])
    client.force_login(ctx['finance'])
    history_url = reverse('reports:tplus-export')
    page = client.get(history_url,{'history_page':3})
    assert page.status_code == 200
    assert page.context['history_page_obj'].paginator.count == 56
    assert len(page.context['history']) == 6 and page.context['dataset'] is None
    filtered = client.get(history_url,{'history-period':'2026-08','history-status':'pending','history_page':2})
    assert filtered.status_code == 200 and filtered.context['history_page_obj'].paginator.count == 28
    assert len(filtered.context['history']) == 3
    assert 'history-period=2026-08' in filtered.context['history_pagination_query']
    preview = client.post(history_url,{'action':'preview','period':'2026-09','idempotency_key':'keep-preview-on-history'})
    assert preview.status_code == 200 and preview.context['dataset'] is not None
    preserved = dict(preview.context['history_preserved'])
    assert preserved['period'] == '2026-09'
    separate = client.get(history_url,{**preserved,'history-period':'2026-08'})
    assert separate.status_code == 200 and separate.context['period'] == '2026-09'
    assert separate.context['history_page_obj'].paginator.count == 28

    assert client.get(history_url,{'history-date_from':'2026-02-30'}).status_code == 400
    mapping = client.get(reverse('reports:external-reference-list'),{'reference_state':'missing','q':'EQ-NEEDS-CARD'})
    assert mapping.status_code == 200
    assert [row['asset'].pk for row in mapping.context['page_obj']] == [second.pk]
    assert 'reference_state=missing' in mapping.context['pagination_query']


def test_clearance_search_does_not_shrink_whole_document_progress(client):
    ctx = offboarding_context('CLEARQUERY')
    first,_ = formal_asset(ctx,'CLEARQUERY-FIRST')
    formal_asset(ctx,'CLEARQUERY-SECOND')
    clearance = initiate_clearance(actor=ctx['hr'],employee=ctx['employee'],idempotency_key='clearance-query')
    client.force_login(ctx['equipment'])
    page = client.get(reverse('offboarding:clearance-detail',args=[clearance.pk]),{'q':first.asset_code,'resolution':'pending'})
    assert page.status_code == 200
    assert page.context['asset_page'].paginator.count == 1
    assert page.context['clearance'].unresolved_assets == 2
    assert '完成清退' not in page.content.decode()
