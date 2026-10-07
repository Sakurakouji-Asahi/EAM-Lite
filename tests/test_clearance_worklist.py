"""Compact clearance worklists preserve scope, actions and detail navigation."""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.offboarding import views
from apps.offboarding.forms import ClearanceDetailFilterForm
from apps.offboarding.services import initiate_clearance
from tests.test_sprint10_lifecycle import _initiate_disposal
from tests.test_sprint10_support import (
    active_internal_loan, formal_asset, offboarding_context,
)
from tests.test_sprint3_support import (
    complete_initialization, grant_scope, make_department, make_employee, make_user,
)
from tests.test_sprint17_services import issued_custody


class DetailLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.all_links = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = [dict(attrs).get("href", ""), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.all_links.append(tuple(self.current))
            if self.current[1].strip() == "核对详情":
                self.links.append(self.current[0])
            self.current = None


@pytest.mark.parametrize("value, valid", [("", True), ("compact", True), ("unknown", False)])
def test_clearance_view_is_validated(value, valid):
    assert ClearanceDetailFilterForm({"view": value}).is_valid() is valid


@pytest.mark.django_db
def test_unresolved_filter_includes_disposal_in_progress(client):
    context = offboarding_context("WORKPENDING")
    asset, _ = formal_asset(context, "WORKPENDING-A")
    clearance = initiate_clearance(actor=context["hr"], employee=context["employee"], idempotency_key="workpending-init")
    _initiate_disposal(context, asset, "workpending-disposal")
    item = clearance.items.get()
    assert item.resolution == "disposal_in_progress"
    client.force_login(context["hr"])
    url = reverse("offboarding:clearance-detail", args=[clearance.pk])
    pending = client.get(url, {"resolution": "pending", "view": "compact"})
    assert pending.status_code == 200
    assert pending.context["asset_page"].paginator.count == 1
    assert pending.context["item_rows"][0]["item"].pk == item.pk
    assert "处置处理中" in pending.content.decode()
    resolved = client.get(url, {"resolution": "resolved", "view": "compact"})
    assert resolved.context["asset_page"].paginator.count == 0
    assert "没有符合当前条件的逐件资产" in resolved.content.decode()


@pytest.mark.django_db
def test_compact_worklist_keeps_paging_search_and_safe_detail_actions(client, monkeypatch):
    context = offboarding_context("WORKPAGES")
    formal_asset(context, "WORKPAGES-A")
    formal_asset(context, "WORKPAGES-B")
    clearance = initiate_clearance(actor=context["hr"], employee=context["employee"], idempotency_key="workpages-init")
    paginate = views.paginate_query
    monkeypatch.setattr(views, "paginate_query", lambda *args, **kwargs: paginate(*args, per_page=1, **kwargs))
    client.force_login(context["equipment"])
    url = reverse("offboarding:clearance-detail", args=[clearance.pk])
    params = {"q": "清退测试资产", "resolution": "pending", "view": "compact", "page": 2, "supply_page": 1}
    compact = client.get(url, params)
    assert compact.status_code == 200
    assert compact.context["compact_view"] is True
    assert compact.context["asset_page"].number == 2
    assert len(compact.context["item_rows"]) == 1
    assert "逐件资产紧凑工作清单" in compact.content.decode()
    assert "发起时原快照（不可变）" not in compact.content.decode()
    item = compact.context["item_rows"][0]["item"]
    link, = DetailLinks(compact.content.decode()).links
    parsed = urlsplit(link)
    query = parse_qs(parsed.query, keep_blank_values=True)
    assert query == {**{key: [str(value)] for key, value in params.items()}, "view": [""]}
    assert parsed.fragment == f"clearance-item-{item.pk}"
    detailed = client.get(url + "?" + parsed.query)
    assert detailed.status_code == 200 and detailed.context["compact_view"] is False
    assert f'id="{parsed.fragment}"' in detailed.content.decode()
    assert reverse("offboarding:item-return", args=[clearance.pk, item.pk]) in compact.content.decode()
    return_path = reverse("offboarding:item-return", args=[clearance.pk, item.pk])
    return_link = next(href for href, _label in DetailLinks(compact.content.decode()).all_links if urlsplit(href).path == return_path)
    assert parse_qs(urlsplit(return_link).query)["return_to"] == [compact.wsgi_request.get_full_path()]
    action = client.get(return_link)
    assert action.status_code == 200
    assert action.context["cancel_url"] == compact.wsgi_request.get_full_path()
    assert "987654.32" not in compact.content.decode()
    client.force_login(context["hr"])
    readonly = client.get(url, {"view": "compact"})
    assert reverse("offboarding:item-return", args=[clearance.pk, item.pk]) not in readonly.content.decode()
    assert reverse("offboarding:item-transfer", args=[clearance.pk, item.pk]) not in readonly.content.decode()


@pytest.mark.django_db
def test_compact_worklist_retains_department_item_scope(client):
    context = offboarding_context("WORKSCOPE")
    owned, _ = formal_asset(context, "WORKSCOPE-OWN")
    other_department = make_department(context["company"], "WORKSCOPE-OTHER")
    other_owner = make_employee(context["company"], other_department, "WORKSCOPE-OTHER-OWNER")
    borrowed, _ = formal_asset(context, "WORKSCOPE-BORROWED", employee=other_owner)
    active_internal_loan(context, borrowed, context["employee"], "workscope-borrow")
    clearance = initiate_clearance(actor=context["hr"], employee=context["employee"], idempotency_key="workscope-init")
    manager = make_user("workscope-manager", "department_manager")
    grant_scope(manager, context["company"], context["department"], descendants=False, assigned_by=context["admin"])
    client.force_login(manager)
    page = client.get(reverse("offboarding:clearance-detail", args=[clearance.pk]), {"view": "compact"})
    assert page.status_code == 200
    assert {row["item"].asset_id for row in page.context["item_rows"]} == {owned.pk}
    assert borrowed.asset_code not in page.content.decode()
    assert borrowed.asset_name not in page.content.decode()
    client.force_login(context["admin"])
    assert client.get(reverse("offboarding:clearance-detail", args=[clearance.pk]), {"view": "compact"}).status_code == 404


@pytest.mark.django_db
def test_supply_worklist_detail_link_keeps_filters_and_cost_hidden_for_hr(client):
    company, _actor, _department, _employee_department, _source, _target, _durable, custody = issued_custody(quantity="2")
    hr = make_user("worksupply-hr", "hr")
    complete_initialization(company, hr)
    clearance = initiate_clearance(actor=hr, employee=custody.employee, idempotency_key="worksupply-init")
    client.force_login(hr)
    url = reverse("offboarding:clearance-detail", args=[clearance.pk])
    page = client.get(url, {"view": "compact", "resolution": "pending", "q": custody.item.item_code})
    assert page.status_code == 200
    assert "数量型耐用品紧凑工作清单" in page.content.decode()
    assert "管理金额" not in page.content.decode()
    assert page.context["supply_page"].paginator.count == 1
    item = page.context["supply_item_rows"][0]["item"]
    link, = DetailLinks(page.content.decode()).links
    assert urlsplit(link).fragment == f"clearance-supply-item-{item.pk}"
    detailed = client.get(url + "?" + urlsplit(link).query)
    assert detailed.context["compact_view"] is False
    assert f'id="clearance-supply-item-{item.pk}"' in detailed.content.decode()
