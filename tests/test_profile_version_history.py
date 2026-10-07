"""Stored parameter differences, adjacent versions and read-only F1 scope."""
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.finance.models import AssetDepreciationProfile, DepreciationEntry, DepreciationSchedule
from apps.finance.profile_history import PARAMETERS
from tests.test_sprint3_support import complete_initialization, make_company, make_user
from tests.test_sprint4_services import _profile_context

pytestmark = pytest.mark.django_db


def _next_profile(old, **overrides):
    values = {field: getattr(old, field) for field, _label in PARAMETERS}
    old.status = "completed"
    old.effective_to = date(2024, 1, 31)
    old.save(update_fields=["status", "effective_to"])
    values.update(company=old.company, asset=old.asset, version=old.version + 1,
                  effective_from=date(2024, 2, 1), status="active", created_by=old.created_by,
                  change_reason="寿命与工作量估计调整\n保留原记录 <script>unsafe</script>")
    values.update(overrides)
    return AssetDepreciationProfile.objects.create(**values)


def test_version_detail_compares_saved_parameters_without_rounding_or_recalculation(client):
    company, actor, _management, _admin, asset, finance, original = _profile_context(method="manual")
    selected = _next_profile(original, method="units_of_production", expected_total_units=Decimal("12345.000001"),
                             work_unit="台时", salvage_mode="amount", salvage_rate=None,
                             salvage_amount=Decimal("0.00"), useful_life_months=36,
                             start_rule="specified_month", start_date=date(2024, 2, 1),
                             actual_continuation_date=date(2024, 2, 1),
                             opening_book_value=Decimal("11999.99"))
    client.force_login(actor)
    before = list(asset.depreciation_profiles.order_by("version").values())
    audit_before, schedules_before = AuditLog.objects.count(), DepreciationSchedule.objects.count()
    source = reverse("finance:asset-finance-detail", args=[asset.pk]) + "?" + urlencode({
        "entry-year": "2024", "entry-source": "batch", "entry_page": "2", "pending_query": "q=PROFILE&page=3",
    })
    response = client.get(reverse("finance:profile-detail", args=[selected.pk]), {"return_to": source})
    assert response.status_code == 200 and response.context["previous_profile"].pk == original.pk
    rows = {row["key"]: row for row in response.context["parameter_rows"]}
    assert rows["expected_total_units"]["value"] == "12345.000001"
    assert rows["salvage_amount"]["value"] == "0.00" and rows["salvage_amount"]["changed"]
    assert rows["opening_book_value"]["value"] == "11999.99"
    assert rows["salvage_rate"]["previous"] == "0.05000000"
    assert rows["method"]["previous"] == "手工折旧" and rows["method"]["value"] == "工作量法"
    assert not rows["depreciation_policy"]["changed"] and original.depreciation_policy.policy_key in rows["depreciation_policy"]["value"]
    assert all(key not in rows for key in ("status", "effective_from", "effective_to", "change_reason"))
    assert response.context["profile_return_url"] == source + "#depreciation-profiles"
    assert parse_qs(urlsplit(response.context["previous_profile_url"]).query)["return_to"] == [source]
    html = response.content.decode()
    assert "寿命与工作量估计调整" in html and "&lt;script&gt;unsafe&lt;/script&gt;" in html
    assert "<script>unsafe</script>" not in html
    assert list(asset.depreciation_profiles.order_by("version").values()) == before
    finance.refresh_from_db()
    assert finance.original_cost == Decimal("12000.00")
    assert AuditLog.objects.count() == audit_before and DepreciationSchedule.objects.count() == schedules_before
    assert not DepreciationEntry.objects.exists()


def test_first_and_equal_versions_have_clear_states_and_original_query_links(client):
    _company, actor, _management, _admin, asset, _finance, original = _profile_context(method="manual")
    following = _next_profile(original, version=3, change_reason="生效区间变更，参数值保持")
    client.force_login(actor)
    origin = reverse("finance:asset-finance-detail", args=[asset.pk]) + "?entry-year=2024&entry-source=batch&entry_page=2"
    response = client.get(origin)
    links = {profile.version: profile.ui_detail_url for profile in response.context["profiles"]}
    assert parse_qs(urlsplit(links[1]).query)["return_to"] == [origin]
    response = client.get(links[1])
    assert response.context["previous_profile"] is None and response.context["next_profile"].pk == following.pk
    assert "首个存档版本" in response.content.decode()
    assert len(response.context["parameter_rows"]) == 17
    response = client.get(response.context["next_profile_url"])
    assert response.context["previous_profile"].pk == original.pk
    assert response.context["changed_rows"] == [] and len(response.context["unchanged_rows"]) == 17
    assert "参数值与上一存档版本一致" in response.content.decode()
    assert response.context["profile_return_url"] == origin + "#depreciation-profiles"


def test_profile_detail_is_get_only_finance_read_scope_and_fixed_same_asset_return(client):
    company, actor, management, _admin, asset, _finance, profile = _profile_context(method="manual")
    target = reverse("finance:profile-detail", args=[profile.pk])
    fallback = reverse("finance:asset-finance-detail", args=[asset.pk]) + "#depreciation-profiles"
    client.force_login(management)
    before = AuditLog.objects.count()
    response = client.get(target, {"return_to": "https://example.invalid/finance/assets/elsewhere/"})
    assert response.status_code == 200 and response.context["profile_return_url"] == fallback
    content = response.content.decode().split('<div class="finance-profile-history">', 1)[1].split('</main>', 1)[0]
    assert '<form' not in content
    assert client.get(target, {"return_to": "/finance/assets/00000000-0000-0000-0000-000000000000/"}).context["profile_return_url"] == fallback
    assert client.post(target).status_code == 405
    assert AuditLog.objects.count() == before
    client.force_login(make_user("profile-history-hr", "hr"))
    assert client.get(target).status_code == 403
    client.force_login(actor)
    company.is_active = False
    company.save(update_fields=["is_active"])
    other_company = make_company("PROFILE-OTHER")
    complete_initialization(other_company, actor)
    # Fixture creation has its own audit; compare only the following read.
    before_company_read = AuditLog.objects.count()
    assert client.get(target).status_code == 404
    assert AuditLog.objects.count() == before_company_read
