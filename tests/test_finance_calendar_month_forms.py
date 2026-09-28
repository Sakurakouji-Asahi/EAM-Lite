from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.finance.forms import DepreciationBatchGenerateForm, WorkUsageForm
from apps.finance.models import AssetWorkUsage, DepreciationBatch, DepreciationEntry
from tests.test_correction_finance_services import _custom_profile_context
from tests.test_sprint3_support import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def finance_user():
    return make_user("calendar-finance", "finance")


@pytest.mark.parametrize("form_type", [DepreciationBatchGenerateForm, WorkUsageForm])
@pytest.mark.parametrize("start,end,exclusive", [
    ("2026-09-01", "2026-09-30", date(2026, 10, 1)),
    ("2026-01-01", "2026-01-31", date(2026, 2, 1)),
    ("2026-02-01", "2026-02-28", date(2026, 3, 1)),
    ("2028-02-01", "2028-02-29", date(2028, 3, 1)),
    ("2026-12-01", "2026-12-31", date(2027, 1, 1)),
])
def test_inclusive_calendar_dates_convert_only_for_services(finance_user, form_type, start, end, exclusive):
    form = form_type({"period_start": start, "period_end": end,
                      "current_units": "0", "work_unit": "台时"}, actor=finance_user)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["period_end"] == date.fromisoformat(end)
    assert form["period_end"].value() == end
    assert form.service_period_values() == {"period_start": date.fromisoformat(start), "period_end": exclusive}


@pytest.mark.parametrize("start,end,field", [
    ("2026-09-02", "2026-09-30", "period_start"),
    ("2026-09-01", "2026-09-29", "period_end"),
    ("2026-09-01", "2026-10-01", "period_end"),
    ("2026-09-01", "2026-08-31", "period_end"),
    ("2026-02-01", "2026-02-29", "period_end"),
    ("9999-12-01", "9999-12-31", "period_end"),
])
def test_incomplete_or_out_of_range_month_has_field_error(finance_user, start, end, field):
    form = DepreciationBatchGenerateForm({"period_start": start, "period_end": end}, actor=finance_user)
    assert not form.is_valid() and field in form.errors
    assert not DepreciationBatch.objects.exists()


def test_current_month_defaults_and_retry_keep_end_date(finance_user):
    with patch("apps.finance.forms.timezone.localdate", return_value=date(2028, 2, 12)):
        form = DepreciationBatchGenerateForm(actor=finance_user)
    assert form.initial["period_start"] == date(2028, 2, 1)
    assert form.initial["period_end"] == date(2028, 2, 29)
    assert form["idempotency_key"].value()
    invalid = DepreciationBatchGenerateForm({"period_start": "2026-09-01", "period_end": "2026-09-30",
                                            "manual_inputs_json": "invalid-json"}, actor=finance_user)
    assert not invalid.is_valid()
    assert invalid["period_end"].value() == "2026-09-30"
    assert "period_end" not in invalid.errors


def test_september_http_trial_is_190_and_does_not_post(client):
    company, actor, management, _, _, finance, _ = _custom_profile_context(
        method="straight_line", start_date=date(2026, 9, 1),
    )
    before_finance = type(finance).objects.filter(pk=finance.pk).values().get()
    client.force_login(management)
    url = reverse("finance:batch-generate")
    assert client.get(url).status_code == 403
    client.force_login(actor)
    page = client.get(url)
    nonce = page.context["form"]["idempotency_key"].value()
    data = {"period_start": "2026-09-01", "period_end": "2026-09-30", "idempotency_key": str(nonce)}
    response = client.post(url, data)
    assert response.status_code == 302
    batch = DepreciationBatch.objects.get(company=company)
    assert batch.period_start == date(2026, 9, 1) and batch.period_end == date(2026, 10, 1)
    assert batch.period_end_inclusive == date(2026, 9, 30) and batch.status == "draft"
    item = batch.items.get()
    # Independent monthly straight-line check: (12000 - 12000 * 5%) / 60 = 190.
    assert item.planned_amount == Decimal("190.00") and item.status == "ready"
    assert not DepreciationEntry.objects.exists()
    assert type(finance).objects.filter(pk=finance.pk).values().get() == before_finance
    assert client.post(url, data).url == response.url
    assert DepreciationBatch.objects.count() == 1
    for target in (response.url, reverse("finance:batch-list")):
        assert "2026-09-01 至 2026-09-30" in client.get(target).content.decode()


def test_work_usage_accepts_month_end_and_retains_internal_boundary(client):
    _, actor, _, _, asset, _, profile = _custom_profile_context(
        method="units_of_production", start_date=date(2026, 9, 1),
    )
    client.force_login(actor)
    response = client.post(reverse("finance:work-usage", args=[asset.pk]), {
        "period_start": "2026-09-01", "period_end": "2026-09-30",
        "current_units": "10", "work_unit": "台时",
    })
    assert response.status_code == 302
    usage = AssetWorkUsage.objects.get(depreciation_profile=profile)
    assert usage.period_start == date(2026, 9, 1) and usage.period_end == date(2026, 10, 1)
    assert usage.current_units == Decimal("10.000000")
    assert not DepreciationEntry.objects.exists()
