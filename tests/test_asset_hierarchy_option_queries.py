"""Large asset selectors retain full paths without fetching ancestors per option."""
import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.assets.forms import AssetDraftForm
from apps.assets.views import _configure_hierarchy_labels
from tests.test_sprint3_support import make_category, make_company, make_location, make_user

pytestmark = pytest.mark.django_db


def render_hierarchies(actor, company):
    form = AssetDraftForm(actor=actor, company=company)
    _configure_hierarchy_labels(form)
    with CaptureQueriesContext(connection) as queries:
        html = str(form["category"]) + str(form["location"])
    return form, html, len(queries)


def test_rendering_many_sibling_choices_uses_bounded_queries_and_keeps_full_paths(record_property):
    company = make_company("HIERARCHY")
    actor = make_user("hierarchy-equipment", "equipment")
    category_root = make_category(company, "CR")
    category_floor = make_category(company, "CF", parent=category_root)
    location_root = make_location(company, "LR")
    location_floor = make_location(company, "LF", parent=location_root)
    for index in range(16):
        make_category(company, f"C{index:02}", parent=category_floor)
        make_location(company, f"L{index:02}", parent=location_floor)
    _, html, queries = render_hierarchies(actor, company)
    record_property("hierarchy_render_queries", queries)
    assert "C15 / CR 实物分类 / CF 实物分类 / C15 实物分类" in html
    assert "L15 / LR 位置 / LF 位置 / L15 位置" in html
    assert queries <= 8, f"Rendering 32 leaf choices made {queries} queries"


def test_new_form_reads_updated_paths_without_expanding_the_allowed_choices():
    company = make_company("FRESH-HIERARCHY")
    actor = make_user("fresh-hierarchy-equipment", "equipment")
    root = make_category(company, "CURRENT-ROOT")
    leaf = make_category(company, "CURRENT-LEAF", parent=root)
    inactive = make_category(company, "INACTIVE-CHOICE", active=False)
    other = make_company("HISTORY-HIERARCHY", active=False)
    make_category(other, "OTHER-COMPANY-CATEGORY")
    make_location(other, "OTHER-COMPANY-LOCATION")
    _, first, _ = render_hierarchies(actor, company)
    root.name = "更新后的上级名称"
    root.save(update_fields=["name"])
    form, following, _ = render_hierarchies(actor, company)
    assert "更新后的上级名称" not in first and "更新后的上级名称" in following
    assert "OTHER-COMPANY" not in following and "INACTIVE-CHOICE" not in following
    assert form.fields["category"].clean(leaf.pk) == leaf
    with pytest.raises(ValidationError):
        form.fields["category"].clean(inactive.pk)
