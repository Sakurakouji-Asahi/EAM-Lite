from html.parser import HTMLParser

import pytest
from django.urls import reverse

from tests.test_navigation_information_architecture import navigation_users


pytestmark = pytest.mark.django_db


class SearchInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id"):
            self.values[attributes["id"]] = attributes.get("value", "")


def test_unrelated_page_query_does_not_populate_asset_search(client, navigation_users):
    _company, users = navigation_users
    client.force_login(users["equipment"])
    response = client.get(reverse("home"), {"q": "保养到期"})
    parser = SearchInputParser()
    parser.feed(response.content.decode())
    assert parser.values["global-asset-search"] == ""
    assert parser.values["quick-navigation-query"] == ""


def test_asset_list_search_keeps_its_query(client, navigation_users):
    _company, users = navigation_users
    client.force_login(users["equipment"])
    response = client.get(reverse("assets:asset-list"), {"q": "EQ-42"})
    parser = SearchInputParser()
    parser.feed(response.content.decode())
    assert parser.values["global-asset-search"] == "EQ-42"


@pytest.mark.parametrize("role,can_search_assets", [("employee", True), ("equipment", True), ("hr", False)])
def test_quick_asset_search_respects_available_navigation(client, navigation_users, role, can_search_assets):
    _company, users = navigation_users
    client.force_login(users[role])
    html = client.get(reverse("home")).content.decode()
    assert 'id="quick-navigation"' in html
    assert f'data-preference-scope="{users[role].pk}"' in html
    assert ('id="quick-navigation-asset-search"' in html) is can_search_assets


def test_anonymous_login_has_no_quick_navigation(client):
    html = client.get(reverse("login")).content.decode()
    assert 'id="quick-navigation"' not in html
    assert 'data-quick-navigation-open' not in html
