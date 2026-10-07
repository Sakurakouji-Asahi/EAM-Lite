"""Report display controls use only columns already visible to the actor."""

from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string

from apps.reports.forms import ReportFilterForm
from apps.reports.schemas import visible_report_definition
from apps.reports.views import _filter_layout


class ColumnControls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.toggles = []
        self.owner = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-report-columns" in attrs:
            self.owner = (attrs["data-report-owner"], attrs["data-report-company"], attrs["data-report-key"])
        if tag == "input" and "data-report-column-toggle" in attrs:
            self.toggles.append((attrs["value"], "disabled" in attrs))


@pytest.mark.parametrize("report_key, financial", [
    ("asset_ledger", False),
    ("fixed_asset_detail", True),
    ("supply_stock_balance", False),
    ("supply_stock_balance", True),
])
def test_column_choices_match_authorized_definition(report_key, financial):
    definition = visible_report_definition(
        report_key, include_supply_cost=financial, include_asset_finance=financial,
    )
    html = render_to_string("reports/_columns.html", {
        "dataset": SimpleNamespace(definition=definition),
        "columns": definition.columns,
        "request": SimpleNamespace(user=SimpleNamespace(pk=17)),
        "current_company": SimpleNamespace(pk="company-test"),
    })
    controls = ColumnControls(html)
    assert controls.owner == ("17", "company-test", report_key)
    assert controls.toggles == [(column.key, index == 0) for index, column in enumerate(definition.columns)]
    assert "导出仍包含全部列" in html
    if not financial:
        assert not any(column.kind in {"money", "unit_cost"} for column in definition.columns)
        assert "原值" not in html and "金额" not in html and "单价" not in html


@pytest.mark.parametrize("has_results", [False, True])
def test_filter_undo_only_appears_with_applied_results(has_results):
    form = ReportFilterForm({"report_type": "asset_ledger", "q": "已生效关键词"})
    assert form.is_valid()
    html = render_to_string("reports/_filters.html", {
        "form": form,
        "dataset": SimpleNamespace(row_count=0) if has_results else None,
        "reset_url": "/reports/?report_type=asset_ledger",
        **_filter_layout(form, "asset_ledger"),
    })
    assert ("data-report-filter-undo" in html) is has_results
    assert 'value="已生效关键词"' in html
