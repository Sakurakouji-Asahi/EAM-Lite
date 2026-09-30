"""Operational display checks with constructed finance and attachment data."""

from datetime import date
from decimal import Decimal
from urllib.parse import quote

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.finance.models import DepreciationEntry, TheoreticalDepreciationLine
from apps.finance.services import generate_depreciation_batch, confirm_depreciation_batch
from apps.maintenance.services import upload_maintenance_attachment
from tests.test_correction_finance_services import _custom_profile_context
from tests.test_sprint3_support import PDF_BYTES
from tests.test_sprint9_http_attachments import _problem_record
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize('model', [DepreciationEntry, TheoreticalDepreciationLine])
@pytest.mark.parametrize('start,end,last_day', [
    (date(2026, 9, 1), date(2026, 10, 1), date(2026, 9, 30)),
    (date(2028, 2, 1), date(2028, 3, 1), date(2028, 2, 29)),
    (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 1)),
])
def test_period_display_uses_the_last_included_day_without_changing_boundaries(model, start, end, last_day):
    row = model(period_start=start, period_end=end)
    assert row.period_end_inclusive == last_day
    assert row.period_start == start and row.period_end == end


def test_confirmed_batch_and_asset_details_show_actual_posting_state_and_calendar_period(client):
    company, actor, _, _, asset, _, _ = _custom_profile_context(method='straight_line', start_date=date(2026, 9, 1))
    batch = generate_depreciation_batch(
        actor=actor, company=company, period_start=date(2026, 9, 1), period_end=date(2026, 10, 1),
        idempotency_key='display-confirmed-batch',
    )
    confirm_depreciation_batch(actor=actor, batch=batch, reason='核对本月折旧')
    before = list(DepreciationEntry.objects.order_by('pk').values())
    client.force_login(actor)
    page = client.get(reverse('finance:asset-finance-detail', args=[asset.pk]))
    assert '2026-09-01 至 2026-09-30' in page.content.decode()
    batch_page = client.get(reverse('finance:batch-detail', args=[batch.pk]))
    assert '正式折旧分录已生成' in batch_page.content.decode()
    assert '请先核对本期试算明细' not in batch_page.content.decode()
    assert DepreciationEntry.objects.get(asset=asset).amount == Decimal('190.00')
    assert list(DepreciationEntry.objects.order_by('pk').values()) == before


@pytest.mark.parametrize('method,enabled', [('straight_line', False), ('units_of_production', True)])
def test_work_usage_entry_matches_the_current_depreciation_method(client, method, enabled):
    _, actor, _, _, asset, _, _ = _custom_profile_context(method=method, start_date=date(2026, 9, 1))
    client.force_login(actor)
    detail_url = reverse('finance:asset-finance-detail', args=[asset.pk])
    work_url = reverse('finance:work-usage', args=[asset.pk])
    detail = client.get(detail_url)
    assert (work_url in detail.content.decode()) is enabled
    direct = client.get(work_url)
    assert direct.status_code == (200 if enabled else 302)
    if not enabled:
        assert direct.url == detail_url


def test_maintenance_evidence_download_uses_the_safe_original_filename(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    context = maintenance_context('EVIDENCENAME')
    record = _problem_record(context, 'evidence-name-record')
    link = upload_maintenance_attachment(
        actor=context['equipment'], target=record,
        uploaded_file=SimpleUploadedFile('保养复查记录.pdf', PDF_BYTES, content_type='application/pdf'),
        security_class='A0',
    )
    client.force_login(context['equipment'])
    response = client.get(reverse('maintenance:attachment-download', args=[link.pk]))
    try:
        assert response.status_code == 200
        assert "filename*=UTF-8''" + quote(link.attachment.safe_filename) in response['Content-Disposition']
        assert response['Cache-Control'] == 'private, no-store'
        assert b''.join(response.streaming_content) == PDF_BYTES
    finally:
        response.close()
