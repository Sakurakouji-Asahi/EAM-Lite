"""Applied filters remain visible and survive report navigation."""

from urllib.parse import parse_qs
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.reports.forms import ReportFilterForm
from apps.reports.templatetags.report_format import report_number
from apps.reports.views import _filter_layout
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import active_fixed_asset_context
from tests.test_sprint18_reports import report_context


@pytest.mark.parametrize('submitted, expected', [
    ({'label_scope': 'not_attached'}, {'asset_scope': 'managed'}),
    ({'asset_status': 'disposed'}, {'include_disposed': True}),
    ({'asset_status': 'draft'}, {'include_drafts': True}),
])
def test_applied_scope_is_reflected_in_visible_fields(submitted, expected):
    form = ReportFilterForm({'report_type': 'asset_ledger', **submitted})
    assert form.is_valid(), form.errors
    for key, value in expected.items():
        assert form.cleaned_data[key] == value
        assert form[key].value() == value


def test_excluding_disposed_assets_opens_the_changed_advanced_filter():
    form = ReportFilterForm({'report_type': 'asset_ledger', 'include_disposed': ''})
    assert form.is_valid()
    assert _filter_layout(form, 'asset_ledger')['advanced_filters_open'] is True


@pytest.mark.django_db(transaction=True)
def test_default_stock_movement_period_survives_paging(client):
    context = report_context()
    client.force_login(context['warehouse_user'])
    url = reverse('reports:supply-report-detail', args=['supply_stock_movement'])
    first = client.get(url)
    assert first.status_code == 200
    params = parse_qs(first.context['pagination_query'])
    assert params['date_from'] == [first.context['form'].cleaned_data['date_from'].isoformat()]
    assert params['date_to'] == [first.context['form'].cleaned_data['date_to'].isoformat()]
    again = client.get(url + '?' + first.context['pagination_query'] + '&page=1')
    assert again.status_code == 200
    assert again.context['dataset'].filters == first.context['dataset'].filters
    assert client.get(url, {'page_size': 100}).status_code == 200


@pytest.mark.parametrize('value, kind, expected', [
    (Decimal('4414139.12'), 'money', '4,414,139.12'),
    (Decimal('-19173.30'), 'money', '-19,173.30'),
    (Decimal('0'), 'money', '0.00'),
    (None, 'money', '—'),
    (Decimal('0.05'), 'rate', '5.00%'),
    (Decimal('12.34565'), 'quantity', '12.3457'),
    (Decimal('0.123456'), 'unit_cost', '0.123456'),
    ('00123', 'identifier', '00123'),
    ('missing', 'money', '数值无效：missing'),
])
def test_report_format_preserves_numbers_and_identifiers(value, kind, expected):
    assert report_number(value, kind) == expected


@pytest.mark.django_db(transaction=True)
def test_summary_drilldown_preserves_filters_and_links_to_authorized_asset(client):
    context, asset, *_ = active_fixed_asset_context('RPTRACE')
    client.force_login(context['finance'])
    url = reverse('reports:report-center')
    for key, dimensions in (
        ('department_assets', {'department': asset.department_id}),
        ('employee_assets', {'department': asset.department_id, 'responsible_employee': asset.responsible_employee_id}),
        ('fixed_asset_detail', {'fixed_asset_category': asset.finance.fixed_asset_category_id}),
    ):
        page = client.get(url, {'report_type': key, 'q': asset.asset_name, 'page_size': 100})
        assert page.status_code == 200
        detail_url = page.context['summary_details'][0]['url']
        params = parse_qs(detail_url.split('#')[0].lstrip('?'))
        for name, value in dimensions.items():
            assert params[name] == [str(value)]
        assert params['q'] == [asset.asset_name]
        assert params['page_size'] == ['100']
        assert params['as_of_date'] == [page.context['form'].cleaned_data['as_of_date'].isoformat()]
        narrowed = client.get(url + detail_url.split('#')[0])
        assert narrowed.status_code == 200
        assert narrowed.context['dataset'].row_count == page.context['summary']['rows'][0]['record_count'] == 1
        assert narrowed.context['dataset'].rows[0]['asset_code'] == asset.asset_code
        asset_url = narrowed.context['detail_rows'][0]['asset_url']
        assert asset_url == reverse('assets:asset-detail', args=[asset.pk])
        assert client.get(asset_url).status_code == 200

    client.force_login(make_user('rptrace-unassigned-employee', 'employee'))
    restricted = client.get(url, {'report_type': 'department_assets'})
    assert restricted.status_code == 200
    assert restricted.context['dataset'].row_count == 0
    assert str(asset.pk) not in restricted.content.decode()
    assert client.get(reverse('assets:asset-detail', args=[asset.pk])).status_code in (403, 404)


@pytest.mark.django_db(transaction=True)
def test_current_month_pagination_is_pinned_and_explicit_all_periods_stays_all(client, monkeypatch):
    context, *_ = active_fixed_asset_context('RPDATE')
    client.force_login(context['finance'])
    monkeypatch.setattr('apps.reports.views.timezone.localdate', lambda: date(2026, 9, 28))
    url = reverse('reports:report-center')
    first = client.get(url, {'report_type': 'monthly_depreciation'})
    query = first.context['pagination_query']
    monkeypatch.setattr('apps.reports.views.timezone.localdate', lambda: date(2026, 10, 1))
    same_query = client.get(url + '?' + query + '&page=1')
    assert same_query.status_code == 200
    assert same_query.context['form'].cleaned_data['period_start'] == date(2026, 9, 1)
    assert same_query.context['form'].cleaned_data['period_end'] == date(2026, 9, 30)
    all_periods = client.get(url, {'report_type': 'monthly_depreciation', 'period_start': '', 'period_end': ''})
    assert all_periods.context['form'].cleaned_data['period_start'] is None
    again = client.get(url + '?' + all_periods.context['pagination_query'] + '&page=1')
    assert again.context['form'].cleaned_data['period_start'] is None
