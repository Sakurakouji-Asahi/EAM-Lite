"""Policy version lookup, company/role boundaries and preserved list navigation."""

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.finance.models import DepreciationPolicy
from tests.test_sprint3_support import complete_initialization, make_company, make_user
from tests.test_sprint4_http import _policy_form_data

pytestmark = pytest.mark.django_db


def _context():
    company = make_company("POLICY-LOOKUP")
    finance = make_user("policy-lookup-finance", "finance")
    complete_initialization(company, finance)
    return company, finance


def _policy(company, *, key="LOOKUP", version=1, status="draft", method="straight_line"):
    return DepreciationPolicy(
        company=company, policy_key=key, version=version, name=f"政策 {key} 版本 {version}",
        method=method, posting_period="monthly", start_rule="next_month", stop_rule="next_month",
        default_useful_life_months=60, default_salvage_mode="rate", default_salvage_rate=Decimal("0.05"),
        status=status, effective_from=date(2025, 1, 1) if status != "draft" else None,
    )


def test_lookup_counts_versions_in_current_keyword_method_scope_and_keeps_page(client):
    company, finance = _context()
    policies = [_policy(company, version=version) for version in range(1, 27)]
    policies += [_policy(company, version=27, status="active"), _policy(company, version=28, status="retired"),
                 _policy(company, key="LOOKUP-MANUAL", method="manual"), _policy(company, key="UNRELATED")]
    foreign = make_company("FOREIGN-POLICY", active=False)
    policies.append(_policy(foreign))
    DepreciationPolicy.objects.bulk_create(policies)
    client.force_login(finance)
    before = AuditLog.objects.count()
    query = {"q": "LOOKUP", "method": "straight_line", "status": "draft", "page_size": "25", "page": "2"}
    response = client.get(reverse("finance:policy-list"), query)
    assert response.status_code == 200
    page = response.context["page_obj"]
    assert page.paginator.count == 26 and page.number == 2
    assert [policy.version for policy in response.context["policies"]] == [1]
    cards = response.context["policy_status_cards"]
    assert [card["count"] for card in cards] == [28, 26, 1, 1]
    assert [card["active"] for card in cards] == [False, True, False, False]
    for card in cards:
        params = parse_qs(urlsplit(card["url"]).query)
        assert params["q"] == ["LOOKUP"] and params["method"] == ["straight_line"]
        assert "page" not in params
    source = parse_qs(urlsplit(page.object_list[0].detail_url).query)["policy_query"][0]
    assert parse_qs(source) == {key: [value] for key, value in query.items()}
    for summary in response.context["policy_filter_summary"]:
        assert "page" not in parse_qs(urlsplit(summary["remove_url"]).query)
    assert AuditLog.objects.count() == before


def test_detail_edit_validation_and_success_preserve_query_without_policy_action(client):
    company, finance = _context()
    policy = _policy(company)
    policy.save()
    client.force_login(finance)
    source = urlencode({"q": "LOOKUP", "status": "draft", "method": "straight_line", "page_size": "25", "page": "2"})
    detail_url = reverse("finance:policy-detail", args=[policy.pk]) + "?" + urlencode({"policy_query": source})
    response = client.get(detail_url)
    assert response.context["policy_return_url"] == reverse("finance:policy-list") + "?" + source
    assert f'name="policy_query" value="{source.replace("&", "&amp;")}"' in response.content.decode()
    edit_url = response.context["policy_edit_url"]
    response = client.get(edit_url)
    assert response.context["cancel_url"] == detail_url
    assert response.context["cancel_label"] == "返回政策详情"
    before = DepreciationPolicy.objects.filter(pk=policy.pk).values().get()
    audit_before = AuditLog.objects.count()
    response = client.post(reverse("finance:policy-action", args=[policy.pk, "activate"]),
                           {"reason": "未确认的操作", "policy_query": source})
    assert response.status_code == 400 and "confirm" in response.context["action_form"].errors
    assert response.context["policy_query"] == source
    assert DepreciationPolicy.objects.filter(pk=policy.pk).values().get() == before
    assert AuditLog.objects.count() == audit_before
    data = _policy_form_data(policy_key="LOOKUP", name="保留的名称", default_salvage_rate="5")
    data["policy_query"] = source
    response = client.post(edit_url, data)
    assert "default_salvage_rate" in response.context["form"].errors
    assert response.context["form"]["name"].value() == "保留的名称"
    assert response.context["cancel_url"] == detail_url
    assert response.context["policy_query"] == source
    assert DepreciationPolicy.objects.filter(pk=policy.pk).values().get() == before
    data["default_salvage_rate"] = "0.05"
    response = client.post(edit_url, data)
    assert response.status_code == 302 and response.url == detail_url
    policy.refresh_from_db()
    assert policy.name == "保留的名称" and policy.status == "draft" and not policy.is_default


def test_invalid_queries_fail_closed_and_returns_cannot_change_route_or_role_scope(client):
    company, finance = _context()
    policy = _policy(company)
    policy.save()
    foreign = _policy(make_company("POLICY-FOREIGN", active=False))
    foreign.save()
    client.force_login(finance)
    response = client.get(reverse("finance:policy-list"), {"status": "not-a-state", "method": "unknown"})
    assert response.status_code == 400 and response.context["page_obj"].paginator.count == 0
    assert response.context["policy_status_cards"] == []
    assert set(response.context["filter_form"].errors) == {"status", "method"}
    assert client.get(reverse("finance:policy-detail", args=[foreign.pk])).status_code == 404
    forged = urlencode({"q": "LOOKUP", "status": "active", "page": "999999999", "method": "unknown",
                         "next": "https://example.invalid/", "company": foreign.company_id})
    response = client.get(reverse("finance:policy-create"), {"policy_query": forged})
    expected = reverse("finance:policy-list") + "?q=LOOKUP&status=active"
    assert response.context["policy_return_url"] == expected and response.context["cancel_url"] == expected
    management = make_user("policy-lookup-management", "management")
    client.force_login(management)
    assert client.get(reverse("finance:policy-list")).status_code == 200
    response = client.get(reverse("finance:policy-detail", args=[policy.pk]))
    assert not response.context["can_manage"] and response.context["action_form"] is None
    assert b"\xe7\xbc\x96\xe8\xbe\x91\xe8\x8d\x89\xe7\xa8\xbf" not in response.content
    assert client.get(reverse("finance:policy-create")).status_code == 403
    assert client.post(reverse("finance:policy-action", args=[policy.pk, "activate"]),
                       {"reason": "越权", "confirm": "on"}).status_code == 403
    client.force_login(make_user("policy-lookup-hr", "hr"))
    assert client.get(reverse("finance:policy-list")).status_code == 403
