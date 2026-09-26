from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.urls import reverse

from apps.assets.models import AssetRegistration
from apps.assets.services import create_asset_draft
from apps.masterdata.models import IssuedCode
from tests.test_label_queue_usability import Forms
from tests.test_sprint3_support import make_company, make_department, make_employee
from tests.test_unified_asset_identity import context, physical_data

pytestmark = pytest.mark.django_db(transaction=True)


def draft(ctx, index=1, **overrides):
    return create_asset_draft(
        actor=ctx["equipment"], company=ctx["company"],
        data=physical_data(ctx, asset_name=f"批量设备 {index}",
                           equipment_number=f"DRAFT-EQ-{index:03}", **overrides),
    )


def test_search_draft_number_equipment_and_department_subtree(context, client):
    child = make_department(context["company"], "CHILD", parent=context["department"])
    employee = make_employee(context["company"], child, "CHILD-E")
    asset = draft(context, department=child, responsible_employee=employee)
    draft(context, 2)
    client.force_login(context["equipment"])
    url = reverse("assets:bulk-registration")
    for query in (asset.draft_number, "DRAFT-EQ-001"):
        response = client.get(url, {"q": query, "department": context["department"].pk})
        assert response.status_code == 200
        assert [a.pk for a in response.context["page"]] == [asset.pk]
        assert response.context["all_filtered_ids"] == [str(asset.pk)]
    assert not IssuedCode.objects.exists()


def test_cross_page_selection_and_preview_preserve_filter_and_page(context, client):
    assets = [draft(context, i) for i in range(26)]
    client.force_login(context["equipment"])
    params = {"q": "DRAFT-EQ", "department": context["department"].pk, "page_size": 25}
    url = reverse("assets:bulk-registration")
    first = client.get(url, params)
    second = client.get(url, {**params, "page": 2})
    assert len(first.context["page"]) == 25 and len(second.context["page"]) == 1
    assert first.context["all_filtered_count"] == 26
    assert len(first.context["all_filtered_ids"]) == 26
    assert first.context["selection_key"] == second.context["selection_key"]
    assert client.get(url, {**params, "page_size": 50}).context["selection_key"] == first.context["selection_key"]
    assert client.get(url, {**params, "q": "DRAFT-EQ-001"}).context["selection_key"] != first.context["selection_key"]
    parser = Forms()
    parser.feed(first.content.decode())
    assert not parser.nested and "bulk-selection-form" in parser.submit_form_ids
    preview = client.post(second.context["selection_url"], {
        "action": "preview", "assets": [str(a.pk) for a in assets],
    })
    assert preview.context["preview"]["ready_count"] == 26
    returned = parse_qs(urlparse(preview.context["selection_url"]).query)
    assert returned["page"] == ["2"] and returned["page_size"] == ["25"]
    assert returned["q"] == ["DRAFT-EQ"]
    assert returned["department"] == [str(context["department"].pk)]
    assert not AssetRegistration.objects.exists() and not IssuedCode.objects.exists()


def test_assignment_goes_to_review_without_registering_until_confirmed(context, client):
    assets = [draft(context, i, responsible_employee=None) for i in (1, 2)]
    client.force_login(context["equipment"])
    url = reverse("assets:bulk-registration") + "?" + urlencode({"q": "DRAFT-EQ"})
    preview = client.post(url, {
        "action": "assignment_preview", "assets": [str(a.pk) for a in assets],
        "responsible_employee": str(context["employee"].pk), "reason": "核对保管责任人",
    })
    token = preview.context["assignment_preview"]["token"]
    assert token and not IssuedCode.objects.exists()
    assert all(a.responsible_employee_id is None for a in assets)
    saved = client.post(preview.context["selection_url"], {"action": "assignment_confirm", "token": token})
    assert saved.context["assignment_result"]["count"] == 2
    assert saved.context["preview"]["ready_count"] == 2
    assert not IssuedCode.objects.exists() and not AssetRegistration.objects.exists()
    for asset in assets:
        asset.refresh_from_db()
        assert asset.responsible_employee_id == context["employee"].pk and asset.asset_status == "draft"
    confirm = {"action": "confirm", "token": saved.context["preview"]["token"]}
    result = client.post(saved.context["selection_url"], confirm)
    assert result.context["result"]["success_count"] == 2
    assert set(result.context["cleared_asset_ids"]) == {str(a.pk) for a in assets}
    assert client.post(saved.context["selection_url"], confirm).context["result"]["success_count"] == 2
    assert AssetRegistration.objects.count() == IssuedCode.objects.count() == 2


def test_unavailable_batch_is_rejected_before_confirm_writes(context, client):
    asset = draft(context)
    client.force_login(context["equipment"])
    url = reverse("assets:bulk-registration")
    preview = client.post(url, {"action": "preview", "assets": [str(asset.pk)]})
    response = client.post(url + "?import_batch=999999", {
        "action": "confirm", "token": preview.context["preview"]["token"],
    })
    assert response.status_code == 403
    asset.refresh_from_db()
    assert asset.asset_status == "draft" and asset.asset_code is None
    assert not IssuedCode.objects.exists() and not AssetRegistration.objects.exists()


def test_invalid_department_never_falls_back_to_all_or_saves(context, client):
    asset = draft(context)
    foreign = make_department(make_company("OTHER", active=False), "OTHER-DEPT")
    client.force_login(context["equipment"])
    url = reverse("assets:bulk-registration")
    preview = client.post(url, {"action": "preview", "assets": [str(asset.pk)]})
    response = client.post(url + "?" + urlencode({"department": foreign.pk}), {
        "action": "confirm", "token": preview.context["preview"]["token"],
    })
    assert response.context["filter_form"].errors and response.context["error"]
    assert response.context["page"].paginator.count == 0
    assert not AssetRegistration.objects.exists() and not IssuedCode.objects.exists()


def test_more_than_200_requires_narrowing_or_explicit_small_selection(context, client):
    assets = [draft(context, i) for i in range(201)]
    client.force_login(context["equipment"])
    url = reverse("assets:bulk-registration")
    response = client.get(url)
    assert response.context["all_filtered_count"] == 201
    assert response.context["all_filtered_ids"] == [] and not response.context["can_select_filtered"]
    rejected = client.post(url, {"action": "preview", "assets": [str(a.pk) for a in assets]})
    assert "200" in rejected.context["error"] and not rejected.context["preview"]
    accepted = client.post(url, {"action": "preview", "assets": [str(assets[0].pk)]})
    assert accepted.context["preview"]["ready_count"] == 1
    assert not IssuedCode.objects.exists()
