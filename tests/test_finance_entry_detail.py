from datetime import date, timedelta
from decimal import Decimal
import pytest
from django.urls import reverse
from django.utils import timezone

from apps.finance.models import DepreciationEntry
from apps.finance.services import (confirm_depreciation_batch, create_value_adjustment,
    generate_depreciation_batch, reverse_depreciation_batch)
from tests.test_asset_list_return_navigation import Links
from tests.test_correction_finance_services import _custom_profile_context

pytestmark = pytest.mark.django_db


def _batch():
    ctx = _custom_profile_context(method="straight_line", start_date=date(2026, 9, 1))
    company, actor, _, _, asset, _, _ = ctx
    batch = generate_depreciation_batch(actor=actor, company=company,
        period_start=date(2026, 9, 1), period_end=date(2026, 10, 1), idempotency_key="entry-source-batch")
    confirm_depreciation_batch(actor=actor, batch=batch, reason="核对分录来源")
    return ctx, batch, DepreciationEntry.objects.get(asset=asset)


def test_batch_and_reversal_entry_trace_keeps_original_query_and_history(client):
    ctx, batch, original = _batch()
    _, actor, management, _, asset, finance, _ = ctx
    reverse_depreciation_batch(actor=actor, batch=batch, reason="来源核对更正", idempotency_key="entry-source-reversal")
    reversal = DepreciationEntry.objects.get(reversal_of=original)
    before = list(DepreciationEntry.objects.order_by("pk").values())
    finance_before = type(finance).objects.filter(pk=finance.pk).values().get()
    client.force_login(management)
    origin = reverse("finance:asset-finance-detail", args=[asset.pk]) + "?entry-year=2026&entry-source=batch&entry_page=1"
    listing = client.get(origin)
    links = [url for url, label in Links(listing).links if label.strip() == "查看来源"]
    assert listing.status_code == 200 and len(links) == 2
    detail = client.get(reverse("finance:entry-detail", args=[original.pk]), {"return_to": origin})
    assert detail.status_code == 200 and detail.context["entry_return_url"] == origin + "#depreciation-entries"
    assert detail.context["source_batch"].pk == batch.pk
    assert "190.00" in detail.content.decode() and "该分录已有对应冲销记录" in detail.content.decode()
    assert "记账后累计折旧" in detail.content.decode()
    assert client.get(detail.context["source_batch_url"]).status_code == 200
    follow = client.get(detail.context["entry_relations"][0]["url"])
    assert follow.context["entry"].pk == reversal.pk
    assert follow.context["entry"].amount == Decimal("-190.00")
    assert "这是一笔冲销分录" in follow.content.decode()
    assert follow.context["entry_return_url"] == origin + "#depreciation-entries"
    back = client.get(follow.context["entry_relations"][0]["url"])
    assert back.context["entry"].pk == original.pk
    assert list(DepreciationEntry.objects.order_by("pk").values()) == before
    assert type(finance).objects.filter(pk=finance.pk).values().get() == finance_before


def test_opening_and_adjustment_sources_show_recorded_values_and_reason(client):
    company, actor, _, _, asset, _, profile = _custom_profile_context(
        method="straight_line", start_date=date(2026, 9, 1))
    boundary = timezone.localdate().replace(day=1)
    opening = DepreciationEntry.objects.create(company=company, asset=asset, depreciation_profile=profile,
        entry_date=boundary, period_start=boundary, period_end=boundary+timedelta(days=1),
        source_type="opening", opening_profile=profile, amount=Decimal("100.00"),
        accumulated_depreciation_after=Decimal("100.00"), book_value_after=Decimal("11900.00"),
        posted_by=actor, posted_at=timezone.now())
    adjustment = create_value_adjustment(actor=actor, asset=asset, adjustment_type="depreciation_adjustment",
        amount="12.34", effective_date=boundary, reason="补录核对差额\n记录原始说明")
    adjusted = DepreciationEntry.objects.get(value_adjustment=adjustment)
    client.force_login(actor)
    initial = client.get(reverse("finance:entry-detail", args=[opening.pk]))
    assert initial.status_code == 200 and "期初累计折旧记录" in initial.content.decode()
    assert not initial.context["entry_relations"] and not initial.context["source_batch"]
    detail = client.get(reverse("finance:entry-detail", args=[adjusted.pk]))
    assert detail.status_code == 200 and detail.context["source_adjustment"].pk == adjustment.pk
    assert "补录核对差额<br>记录原始说明" in detail.content.decode() and "12.34" in detail.content.decode()


def test_entry_scope_method_and_return_target_remain_read_only(client, monkeypatch):
    ctx, _, entry = _batch()
    company, actor, management, admin, asset, _, _ = ctx
    url = reverse("finance:entry-detail", args=[entry.pk])
    client.force_login(admin)
    assert client.get(url).status_code == 403
    client.force_login(management)
    assert client.get(url).status_code == 200
    assert "no-store" in client.get(url)["Cache-Control"]
    assert client.post(url).status_code == 405
    fallback = reverse("finance:asset-finance-detail", args=[asset.pk]) + "#depreciation-entries"
    for target in ("https://example.test/", "//example.test/", reverse("finance:batch-list"),
                   reverse("finance:asset-finance-detail", args=[entry.pk]), "/finance/\\elsewhere"):
        assert client.get(url, {"return_to": target}).context["entry_return_url"] == fallback
    from apps.masterdata.models import Company
    other = Company.objects.create(code="ENTRYOTHER", name="历史公司", is_active=False)
    # Switching the selected company cannot reveal the previous company's entry.
    monkeypatch.setattr("apps.finance.entry_detail.current_company", lambda: other)
    monkeypatch.setattr("apps.finance.permissions.current_company", lambda **kwargs: other)
    client.force_login(actor)
    assert client.get(url).status_code == 404
    client.logout()
    assert client.get(url).status_code == 302
