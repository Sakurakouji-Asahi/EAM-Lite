from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from tests.test_sprint3_http import make_context
from tests.test_sprint3_support import grant_scope, make_asset, make_department, make_employee, make_user


pytestmark = pytest.mark.django_db


class Links(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.links = []
        self.current = None
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = [dict(attrs).get("href", ""), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append(tuple(self.current))
            self.current = None

    def hrefs_for_path(self, path):
        return [href for href, _ in self.links if urlsplit(href).path == path]

    def back_url(self):
        return next(href for href, label in self.links if label.strip() == "返回总账")


@pytest.fixture
def ledger_context():
    actor, company, department, employee, category, _, _, location = make_context()
    return {
        "actor": actor, "company": company, "department": department,
        "employee": employee, "category": category, "location": location,
    }


def draft(context, **overrides):
    return make_asset(**context, management_attribute="LV", serial_number="",
                      is_maintenance_required=False, **overrides)


def test_list_detail_back_preserves_full_filters_page_and_special_query(client, ledger_context):
    context = ledger_context
    query = '导航设备 & + / ? = % # "型号"'
    for index in range(51):
        draft(context, asset_name=f"{query} {index:02d}")
    draft(context, asset_name="其他设备")
    client.force_login(make_user("return-finance", "finance"))
    today = timezone.localdate().isoformat()
    filters = {
        "q": query, "category": str(context["category"].pk),
        "department": str(context["department"].pk), "employee": str(context["employee"].pk),
        "location": str(context["location"].pk), "asset_status": "draft",
        "record_status": "active", "accounting_treatment": "unconfirmed",
        "fixed_asset_category": "", "view": "individual_durable",
        "maintenance_required": "no", "label_status": "not_generated",
        "has_serial_number": "no", "has_attachments": "no",
        "initialized_from": "", "initialized_to": "",
        "created_from": today, "created_to": today,
        "page": "2", "page_size": "50",
    }
    listing = client.get(reverse("assets:asset-list"), filters)
    assert listing.status_code == 200
    assert not listing.context["filter_errors"]
    assert listing.context["page"].number == 2
    assert listing.context["page"].paginator.count == 51
    assert listing.context["page_size"] == "50"
    listed_ids = [asset.pk for asset in listing.context["page"]]
    assert len(listed_ids) == 1
    target = listing.wsgi_request.get_full_path()
    detail_path = reverse("assets:asset-detail", args=[listed_ids[0]])
    detail_links = Links(listing).hrefs_for_path(detail_path)
    assert len(detail_links) == 2  # Both the asset name and the View button.
    for href in detail_links:
        assert parse_qs(urlsplit(href).query) == {"return_to": [target]}
        detail = client.get(href)
        assert detail.status_code == 200
        assert detail.context["back_url"] == target
        assert Links(detail).back_url() == target
    returned = client.get(Links(detail).back_url())
    assert returned.status_code == 200
    assert returned.context["page"].number == 2
    assert returned.context["page_size"] == "50"
    assert returned.context["filters"] == listing.context["filters"]
    assert [asset.pk for asset in returned.context["page"]] == listed_ids
    assert returned.wsgi_request.GET == listing.wsgi_request.GET


def test_detail_back_falls_back_for_missing_external_and_unsafe_destinations(client, ledger_context):
    asset = draft(ledger_context)
    client.force_login(ledger_context["actor"])
    detail_path = reverse("assets:asset-detail", args=[asset.pk])
    fallback = reverse("assets:asset-list")
    targets = (
        None, "https://example.com/assets/", "//example.com/assets/",
        "/\\example.com/assets/", fallback + "?q=bad\nvalue",
        "assets/", "/missing-page/", reverse("logout"),
        reverse("assets:asset-edit", args=[asset.pk]), detail_path,
        fallback + "?q=" + "x" * 3000,
    )
    for target in targets:
        response = client.get(detail_path, {} if target is None else {"return_to": target})
        assert response.status_code == 200
        assert response.context["back_url"] == fallback, target
        assert Links(response).back_url() == fallback, target


def test_return_rechecks_current_scope_after_user_role_changes(client, ledger_context):
    context = ledger_context
    inside = draft(context, asset_name="导航权限 本部门")
    outside_department = make_department(context["company"], "RETURN-OUT")
    outside_employee = make_employee(context["company"], outside_department, "RETURN-OUT-E")
    outside = draft({**context, "department": outside_department, "employee": outside_employee},
                    asset_name="导航权限 其他部门保密设备")
    client.force_login(context["actor"])
    listing = client.get(reverse("assets:asset-list"), {"q": "导航权限", "page_size": "50"})
    assert listing.context["page"].paginator.count == 2
    detail_path = reverse("assets:asset-detail", args=[inside.pk])
    detail = client.get(Links(listing).hrefs_for_path(detail_path)[0])
    assert detail.status_code == 200
    saved_back_url = Links(detail).back_url()
    context["actor"].groups.set([Group.objects.get_or_create(name="department_manager")[0]])
    grant_scope(context["actor"], context["company"], context["department"], descendants=False)

    returned = client.get(saved_back_url)
    assert returned.status_code == 200
    assert not returned.context["filter_errors"]
    assert [asset.pk for asset in returned.context["page"]] == [inside.pk]
    assert outside.asset_name not in returned.content.decode()
    assert client.get(reverse("assets:asset-detail", args=[outside.pk])).status_code == 404

    stale_department_url = reverse("assets:asset-list") + "?" + urlencode({
        "q": "导航权限", "department": outside_department.pk, "page_size": "50",
    })
    detail = client.get(detail_path, {"return_to": stale_department_url})
    assert detail.status_code == 200
    assert Links(detail).back_url() == stale_department_url
    returned = client.get(Links(detail).back_url())
    assert returned.status_code == 200
    assert returned.context["filter_errors"]
    assert returned.context["page"].paginator.count == 0
    assert outside.asset_name not in returned.content.decode()
    assert outside_department.name not in returned.content.decode()
