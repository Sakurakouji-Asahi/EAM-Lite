from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.location_tree import LocationTree
from apps.masterdata.models import Location
from apps.masterdata.services import create_location
from tests.test_sprint3_support import make_company, make_user
from tests.test_sprint13_support import make_location


pytestmark = pytest.mark.django_db


@pytest.fixture
def location_context(client):
    company = make_company("BRANCH")
    actor = make_user("location-branch-editor", "equipment")
    values = {}
    for key, name, parent, active in (
        ("first", "一号楼", None, True),
        ("second", "二号楼", None, True),
        ("office", "办公室", "first", True),
        ("position", "文件柜", "office", True),
        ("old", "原库房", "first", False),
        ("other_office", "办公室", "second", True),
    ):
        values[key] = create_location(actor=actor, company=company, data={
            "code": key.upper(), "name": name, "parent": values.get(parent), "is_active": active,
        })
    foreign_company = make_company("FOREIGN-BRANCH", active=False)
    values["foreign"] = make_location(foreign_company, "FOREIGN", name="其他公司保密位置")
    client.force_login(actor)
    return company, actor, values


def test_location_branch_includes_selected_node_and_all_descendant_levels(client, location_context):
    _, _, nodes = location_context
    response = client.get(reverse("masterdata:location-list"), {"branch": nodes["first"].pk})
    assert response.status_code == 200
    assert {row["object"].pk for row in response.context["rows"]} == {
        nodes["first"].pk, nodes["office"].pk, nodes["position"].pk,
    }
    assert response.context["branch_path"] == "一号楼"
    paths = {node.pk: node.path_label for node in response.context["branch_options"]}
    assert paths[nodes["office"].pk] == "一号楼 / 办公室"
    assert paths[nodes["other_office"].pk] == "二号楼 / 办公室"
    assert nodes["foreign"].pk not in paths


def test_location_branch_search_and_status_are_applied_together(client, location_context):
    _, _, nodes = location_context
    response = client.get(reverse("masterdata:location-list"), {
        "branch": nodes["first"].pk, "q": "库房", "status": "all",
    })
    assert [row["object"].pk for row in response.context["rows"]] == [nodes["old"].pk]
    assert response.context["rows"][0]["ancestor_path"] == "一号楼"
    assert parse_qs(urlsplit(response.context["clear_branch_url"]).query) == {
        "q": ["库房"], "status": ["all"],
    }
    active = client.get(reverse("masterdata:location-list"), {
        "branch": nodes["first"].pk, "q": "库房", "status": "active",
    })
    assert active.context["rows"] == []
    assert "清除筛选" in active.content.decode()


@pytest.mark.parametrize("branch", ["invalid", "-1", "foreign"])
def test_location_branch_rejects_invalid_and_other_company_nodes(client, location_context, branch):
    _, _, nodes = location_context
    if branch == "foreign":
        branch = nodes["foreign"].pk
    response = client.get(reverse("masterdata:location-list"), {"branch": branch, "status": "all"})
    assert response.status_code == 400
    assert response.context["filter_errors"]
    assert response.context["rows"] == []
    assert "其他公司保密位置" not in response.content.decode()


def test_location_drilldown_resets_search_preserves_status_and_detail_return(client, location_context):
    _, _, nodes = location_context
    list_url = reverse("masterdata:location-list") + "?q=OFFICE&status=all"
    response = client.get(list_url)
    office = next(row for row in response.context["rows"] if row["object"].pk == nodes["office"].pk)
    assert office["has_children"]
    assert parse_qs(urlsplit(office["branch_url"]).query) == {
        "branch": [str(nodes["office"].pk)], "status": ["all"],
    }
    children = client.get(office["branch_url"])
    assert {row["object"].pk for row in children.context["rows"]} == {nodes["office"].pk, nodes["position"].pk}
    assert not next(row for row in children.context["rows"] if row["object"].pk == nodes["position"].pk)["has_children"]
    detail = client.get(reverse("masterdata:location-detail", args=[nodes["old"].pk]), {"return_to": list_url})
    assert detail.status_code == 200
    assert detail.context["back_url"] == list_url
    assert parse_qs(urlsplit(detail.context["location_branch_url"]).query) == {
        "branch": [str(nodes["old"].pk)], "status": ["all"],
    }


def test_branch_queries_are_read_only_and_keep_existing_permissions(client, location_context):
    _, _, nodes = location_context
    before = list(Location.objects.order_by("pk").values())
    audits = AuditLog.objects.count()
    assert client.get(reverse("masterdata:location-list"), {"branch": nodes["first"].pk}).status_code == 200
    assert list(Location.objects.order_by("pk").values()) == before
    assert AuditLog.objects.count() == audits
    client.force_login(make_user("location-branch-employee", "employee"))
    assert client.get(reverse("masterdata:location-list"), {"branch": nodes["first"].pk}).status_code == 403


def test_location_tree_can_reuse_loaded_company_records_without_queries(location_context, django_assert_num_queries):
    company, _, nodes = location_context
    records = list(Location.objects.all())
    with django_assert_num_queries(0):
        tree = LocationTree(company, nodes=records)
        assert nodes["foreign"].pk not in tree.nodes
        assert tree.path(nodes["position"].pk) == "一号楼 / 办公室 / 文件柜"
        assert tree.descendants(nodes["office"].pk) == {nodes["office"].pk, nodes["position"].pk}
        assert len(tree.options(tree.nodes)) == 6
