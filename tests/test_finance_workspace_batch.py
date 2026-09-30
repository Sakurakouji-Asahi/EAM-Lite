from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.finance.models import DepreciationBatch, DepreciationEntry, TheoreticalDepreciationRun
from apps.finance.services import generate_depreciation_batch, confirm_depreciation_batch, reverse_depreciation_batch
from tests.test_correction_finance_services import _custom_profile_context

pytestmark = pytest.mark.django_db(transaction=True)


def test_manual_amount_grid_keeps_zero_explicit_and_trial_does_not_post(client):
    company, actor, _, _, asset, _, _ = _custom_profile_context(method='manual', start_date=date(2026, 9, 1))
    client.force_login(actor)
    url = reverse('finance:batch-generate')
    page = client.get(url)
    assert '计算参数 JSON' not in page.content.decode() and '手工折旧输入（JSON）' not in page.content.decode()
    data = {'period_start':'2026-09-01','period_end':'2026-09-30','idempotency_key':'manual-grid-case',
            f'manual-{asset.pk}-amount':'0.00', f'manual-{asset.pk}-reason':''}
    invalid = client.post(url,data)
    assert invalid.status_code == 200 and not DepreciationBatch.objects.exists()
    data[f'manual-{asset.pk}-reason'] = '本期批准不计提'
    result = client.post(url,data)
    assert result.status_code == 302
    batch = DepreciationBatch.objects.get(company=company)
    assert batch.items.get().planned_amount == Decimal('0.00')
    assert not DepreciationEntry.objects.exists()
    review = client.get(result.url, {'q':asset.asset_name,'status':'ready'})
    assert review.context['batch_totals']['amount'] == Decimal('0.00')
    assert review.context['batch_totals']['count'] == 1
    assert reverse('finance:asset-finance-detail',args=[asset.pk]) in review.content.decode()
    assert client.get(result.url, {'status':'invented'}).status_code == 400


def test_error_filter_does_not_allow_partial_confirmation(client):
    company, actor, _, _, asset, _, _ = _custom_profile_context(method='manual', start_date=date(2026, 9, 1))
    client.force_login(actor)
    result = client.post(reverse('finance:batch-generate'),{
        'period_start':'2026-09-01','period_end':'2026-09-30','idempotency_key':'manual-empty-case'})
    batch = DepreciationBatch.objects.get(company=company)
    review = client.get(result.url, {'status':'ready'})
    assert review.context['page_obj'].paginator.count == 0
    assert review.context['batch_totals']['errors'] == 1
    assert '确认批次并过账' in review.content.decode() and 'disabled' in review.content.decode()
    client.post(reverse('finance:batch-confirm',args=[batch.pk]),{'reason':'不可部分确认','confirm':'on'})
    assert not DepreciationEntry.objects.exists()


def test_theoretical_business_fields_reproduce_190_without_mutating_actual_finance(client):
    _, actor, _, _, asset, finance, _ = _custom_profile_context(method='straight_line', start_date=date(2026, 9, 1))
    before = type(finance).objects.filter(pk=finance.pk).values().get()
    client.force_login(actor)
    url = reverse('finance:theoretical-run',args=[asset.pk])
    page = client.get(url)
    assert '计算参数 JSON' not in page.content.decode()
    result = client.post(url, {'as_of_date':'2026-09-30','idempotency_key':'theory-business-grid',
        'original_cost':'12000','method':'straight_line','posting_period':'monthly','commissioning_date':'2026-09-01',
        'start_rule':'specified_date','specified_start':'2026-09-01','useful_life_months':'60',
        'salvage_mode':'rate','salvage_percent':'5','opening_actual_accumulated_depreciation':'0',
        'opening_impairment':'0','opening_book_value':'12000','actual_continuation_date':'2026-09-01'})
    assert result.status_code == 302, result.context['form'].errors if result.context else result.content
    run = TheoreticalDepreciationRun.objects.get(asset=asset)
    assert run.lines.get().theoretical_amount == Decimal('190.00')
    assert type(finance).objects.filter(pk=finance.pk).values().get() == before
    assert not DepreciationEntry.objects.exists()


def test_reversal_review_shows_negative_effect_and_keeps_original_trial_amount(client):
    company, actor, _, _, asset, _, _ = _custom_profile_context(method='straight_line', start_date=date(2026,9,1))
    batch = generate_depreciation_batch(actor=actor,company=company,period_start=date(2026,9,1),period_end=date(2026,10,1),idempotency_key='signed-review')
    confirm_depreciation_batch(actor=actor,batch=batch,reason='核对')
    reversal = reverse_depreciation_batch(actor=actor,batch=batch,reason='冲销核验',idempotency_key='signed-reverse')
    client.force_login(actor)
    page = client.get(reverse('finance:batch-detail',args=[reversal.pk]))
    assert page.context['batch_totals']['effect_amount'] == Decimal('-190.00')
    assert '-190.00' in page.content.decode() and '冲销金额' in page.content.decode()
    assert batch.items.get().planned_amount == Decimal('190.00')
