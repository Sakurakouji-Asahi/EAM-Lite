from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.urls import reverse


class FilterLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
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
            self.links.append(tuple(self.current))
            self.current = None


@pytest.mark.parametrize("individual_durable", [False, True])
def test_empty_list_actions_keep_the_current_asset_view(individual_durable):
    """Both clear links and creation from an empty search stay in the user's ledger."""
    html = render_to_string("assets/asset_list.html", {
        "request": SimpleNamespace(user=SimpleNamespace(is_authenticated=True, username="preview-user", display_name="预览用户")),
        "page": Paginator([], 25).get_page(1),
        "individual_durable_view": individual_durable,
        "can_create": True,
        "page_size": "50",
        "filter_query": "q=unmatched&view=individual_durable" if individual_durable else "q=unmatched",
    })
    links = FilterLinks(html).links
    clear_links = [href for href, label in links if label.strip() == "清除筛选"]
    assert len(clear_links) == 2
    for href in clear_links:
        assert urlsplit(href).path == reverse("assets:asset-list")
        assert parse_qs(urlsplit(href).query) == (
            {"view": ["individual_durable"]} if individual_durable else {}
        )
    create_links = [href for href, label in links if label.strip() == "新增资产"]
    assert create_links == [reverse(
        "supplies:individual-durable-create" if individual_durable else "assets:asset-create"
    )]
