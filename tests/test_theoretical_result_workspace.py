from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.finance.models import DepreciationEntry, TheoreticalDepreciationRun
from apps.finance.services import run_theoretical_depreciation
from tests.test_correction_finance_services import _custom_profile_context

pytestmark = pytest.mark.django_db


def _runs():
    ctx = _custom_profile_context(method="straight_line", start_date=date(2026, 9, 1))
    _, actor, _, _, asset, _, _ = ctx
    runs = []
    for cost in ("12000.00", "24000.00"):
        runs.append(run_theoretical_depreciation(actor=actor, asset=asset, as_of_date=date(2026, 10, 31),
            parameters={"original_cost": Decimal(cost), "method": "straight_line", "posting_period": "monthly",
                        "commissioning_date": date(2026, 9, 1), "start_rule": "specified_date",
                        "specified_start": date(2026, 9, 1), "useful_life_months": 60,
                        "salvage_mode": "rate", "salvage_rate": Decimal("0.05")},
            idempotency_key="workspace-theory-" + cost))
    return ctx, runs


def test_compare_saved_results_and_detail_do_not_change_actual_finance(client):
    ctx, (a, b) = _runs()
    _, _, management, _, asset, finance, _ = ctx
    client.force_login(management)
    before = type(finance).objects.filter(pk=finance.pk).values().get()
    audits = AuditLog.objects.count()
    saved = list(TheoreticalDepreciationRun.objects.order_by("pk").values())
    origin = reverse("finance:theoretical-history", args=[asset.pk])
    page = client.get(origin, {"compare_a": a.pk, "compare_b": b.pk, "status": "completed"})
    assert page.status_code == 200
    values = {row["label"]: row for row in page.context["result_comparison"]}
    assert values["本次试算折旧合计"] == {"label": "本次试算折旧合计", "a": "380.00", "b": "760.00", "changed": True}
    assert values["末期理论净值"]["a"] == "11620.00"
    assert values["末期理论净值"]["b"] == "23240.00"
    assert values["已保存期间数"]["a"] == values["已保存期间数"]["b"] == 2
    assert values["末期截至日期"]["a"] == "2026-10-31"
    parameters = {row["label"]: row for row in page.context["comparison"]}
    assert parameters["试算方法"]["a"] == "年限平均法"
    assert "digest" not in parameters
    detail = client.get(page.context["compared_runs"][0].ui_detail_url)
    assert detail.status_code == 200
    assert detail.context["theory_return_url"] == page.wsgi_request.get_full_path()
    assert detail.context["theory_result"]["amount"] == Decimal("380.00")
    assert "本次保存的参数" in detail.content.decode()
    assert type(finance).objects.filter(pk=finance.pk).values().get() == before
    assert list(TheoreticalDepreciationRun.objects.order_by("pk").values()) == saved
    assert not DepreciationEntry.objects.exists() and AuditLog.objects.count() == audits


def test_row_comparison_and_second_page_return_preserve_query(client):
    ctx, (a, b) = _runs()
    company, actor, _, _, asset, _, _ = ctx
    for i in range(24):
        TheoreticalDepreciationRun.objects.create(company=company, asset=asset, as_of_date=date(2026, 10, 31),
            requested_by=actor, requested_at=timezone.now(), idempotency_key=f"workspace-history-{i}")
    client.force_login(actor)
    url = reverse("finance:theoretical-history", args=[asset.pk])
    page = client.get(url, {"date_from": "2026-10-01", "date_to": "2026-10-31", "compare_b": b.pk, "page": 2})
    assert page.status_code == 200 and page.context["page_obj"].number == 2
    assert page.context["page_obj"].paginator.count == 26
    row = page.context["page_obj"].object_list[0]
    selection = parse_qs(urlsplit(row.ui_compare_a).query)
    assert selection["compare_a"] == [str(row.pk)] and selection["compare_b"] == [str(b.pk)]
    assert selection["date_from"] == ["2026-10-01"] and "page" not in selection
    detail = client.get(row.ui_detail_url)
    assert detail.context["theory_return_url"] == page.wsgi_request.get_full_path()
    assert client.get(detail.context["theory_return_url"]).context["page_obj"].number == 2
    assert client.get(row.ui_compare_a).context["compared_runs"][0].pk == row.pk


def test_no_period_results_and_original_scope_are_explicit(client):
    ctx, (a, _) = _runs()
    company, actor, management, admin, asset, _, _ = ctx
    empty = TheoreticalDepreciationRun.objects.create(company=company, asset=asset, as_of_date=date(2026, 8, 31),
        requested_by=actor, requested_at=timezone.now(), completed_at=timezone.now(), status="completed",
        idempotency_key="workspace-empty")
    incomplete = TheoreticalDepreciationRun.objects.create(company=company, asset=asset, as_of_date=date(2026, 8, 31),
        requested_by=actor, requested_at=timezone.now(), idempotency_key="workspace-incomplete")
    history = reverse("finance:theoretical-history", args=[asset.pk])
    url = reverse("finance:theoretical-detail", args=[asset.pk, empty.pk])
    client.force_login(management)
    detail = client.get(url)
    assert detail.context["theory_result"]["amount"] == Decimal("0.00")
    assert detail.context["theory_result"]["book_value"] is None
    assert "无期间记录，未推算余额" in detail.content.decode()
    for value in ("https://example.test/", "//example.test/", history + "#bad", "/finance/\\elsewhere",
                  reverse("finance:theoretical-history", args=[a.pk])):
        assert client.get(url, {"return_to": value}).context["theory_return_url"] == history
    pending = client.get(reverse("finance:theoretical-detail", args=[asset.pk, incomplete.pk]))
    assert pending.context["theory_result"]["amount"] is None and "暂无完整试算结果" in pending.content.decode()
    assert client.get(history, {"compare_a": a.pk, "compare_b": "00000000-0000-0000-0000-000000000001"}).status_code == 400
    assert client.get(reverse("finance:theoretical-detail", args=[a.pk, empty.pk])).status_code == 404
    client.force_login(admin)
    assert client.get(url).status_code == client.get(history).status_code == 403
