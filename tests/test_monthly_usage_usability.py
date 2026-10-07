"""Monthly entry retains failed input and makes existing records visible."""

from datetime import date
from decimal import Decimal

import pytest
from django.core import signing
from django.urls import reverse

from apps.finance.models import AssetWorkUsage
from apps.finance.services import record_work_usage
from tests.test_correction_finance_services import _custom_profile_context


@pytest.mark.django_db
@pytest.mark.parametrize("raw", ["未核对数字", "-1", "0.1234567"])
def test_invalid_usage_values_and_notes_are_preserved_without_writes(client, raw):
    company, actor, _management, _admin, _asset, _finance, profile = _custom_profile_context(
        method="units_of_production", start_date=date(2026, 8, 1),
    )
    client.force_login(actor)
    url = reverse("finance:monthly-usage") + "?month=2026-09&state=missing"
    page = client.get(url)
    payload = {"manifest": page.context["manifest"], f"usage-{profile.pk}-units": raw,
               f"usage-{profile.pk}-remark": "现场记录仍需核对"}
    rejected = client.post(url, payload)
    assert rejected.status_code == 200
    row, = rejected.context["row_forms"]
    assert row["units"].value() == raw and row["remark"].value() == "现场记录仍需核对"
    assert row.errors and rejected.context["usage_has_errors"] is True
    assert row["units"].field.widget.input_type == "text"
    assert f'value="{raw}"' in rejected.content.decode()
    assert 'data-unsaved-guard="true"' in rejected.content.decode()
    assert signing.loads(rejected.context["manifest"], salt="monthly-usage") == signing.loads(page.context["manifest"], salt="monthly-usage")
    assert not AssetWorkUsage.objects.filter(company=company).exists()


@pytest.mark.django_db
def test_stale_missing_page_keeps_input_and_shows_existing_usage(client):
    company, actor, _management, _admin, asset, _finance, profile = _custom_profile_context(
        method="units_of_production", start_date=date(2026, 8, 1),
    )
    client.force_login(actor)
    url = reverse("finance:monthly-usage") + "?month=2026-09&state=missing"
    original = client.get(url)
    current = record_work_usage(actor=actor, profile=profile, period_start=date(2026, 9, 1), period_end=date(2026, 10, 1),
                                current_units=Decimal("12.345678"), work_unit=profile.work_unit, remark="已核实的工作量")
    rejected = client.post(url, {"manifest": original.context["manifest"], f"usage-{profile.pk}-units": "18.000001",
                                f"usage-{profile.pk}-remark": "旧页面保留的输入"})
    assert rejected.status_code == 200
    assert rejected.context["missing"] == 0
    assert rejected.context["already_recorded_count"] == 1
    assert rejected.context["row_forms"][0]["units"].value() == "18.000001"
    assert rejected.context["row_forms"][0].already_recorded is True
    assert [row.pk for row in rejected.context["recorded_rows"]] == [current.pk]
    html = rejected.content.decode()
    assert "该月已有工作量记录" in html and "12.345678" in html and "已核实的工作量" in html
    current.refresh_from_db()
    assert current.current_units == Decimal("12.345678")
    assert AssetWorkUsage.objects.filter(company=company, asset=asset).count() == 1


@pytest.mark.django_db
def test_recorded_usage_has_actor_context_and_cannot_be_reentered(client):
    _company, actor, management, _admin, _asset, _finance, profile = _custom_profile_context(
        method="units_of_production", start_date=date(2026, 8, 1),
    )
    usage = record_work_usage(actor=actor, profile=profile, period_start=date(2026, 9, 1), period_end=date(2026, 10, 1),
                              current_units=Decimal("0"), work_unit=profile.work_unit)
    client.force_login(actor)
    url = reverse("finance:monthly-usage") + "?month=2026-09&state=recorded"
    page = client.get(url)
    assert page.status_code == 200 and page.context["row_forms"] == []
    assert [row.pk for row in page.context["recorded_rows"]] == [usage.pk]
    assert "录入人 / 时间" in page.content.decode()
    assert "data-usage-fill-zero" not in page.content.decode()
    client.force_login(management)
    assert client.get(url).status_code == 403
