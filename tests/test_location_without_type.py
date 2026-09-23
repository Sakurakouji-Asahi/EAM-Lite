"""A location's actual hierarchy drives asset placement and setup readiness."""
import pytest
from django.urls import reverse

from apps.assets.forms import AssetDraftForm
from apps.masterdata.models import Location
from apps.masterdata.services import compute_initialization_progress, create_location
from tests.test_sprint3_support import make_company, make_user


pytestmark = pytest.mark.django_db(transaction=True)


def test_location_page_creates_hierarchy_without_type_and_asset_uses_leaf(client):
    company = make_company("NOLOCATTYPE")
    actor = make_user("location-type-free", "equipment")
    client.force_login(actor)
    create_url = reverse("masterdata:location-create")

    assert not compute_initialization_progress(company)["locations_configured"]
    form_page = client.get(create_url)
    assert form_page.status_code == 200
    assert 'name="location_type"' not in form_page.content.decode()

    created = client.post(create_url, {
        "code": "", "name": "一号楼", "parent": "", "location_type": "warehouse",
    })
    assert created.status_code == 302
    building = Location.objects.get(company=company, name="一号楼")
    assert building.code == "LOC000001"
    assert building.location_type == Location.LocationType.OTHER
    assert compute_initialization_progress(company)["locations_configured"]
    assert building in AssetDraftForm(actor=actor, company=company).fields["location"].queryset

    child_created = client.post(create_url, {
        "code": "", "name": "办公室 A", "parent": str(building.pk),
    })
    assert child_created.status_code == 302
    office = Location.objects.get(company=company, name="办公室 A")
    assert office.code == "LOC000001-01"
    choices = AssetDraftForm(actor=actor, company=company).fields["location"].queryset
    assert office in choices
    assert building not in choices
    assert compute_initialization_progress(company)["locations_configured"]

    listing = client.get(reverse("masterdata:location-list"))
    detail = client.get(reverse("masterdata:location-detail", args=[office.pk]))
    assert listing.status_code == detail.status_code == 200
    assert "位置类型" not in listing.content.decode()
    assert "位置类型" not in detail.content.decode()


def test_edit_keeps_historical_type_while_page_does_not_ask_for_it(client):
    company = make_company("OLDLOCATTYPE")
    actor = make_user("location-legacy", "equipment")
    legacy = create_location(actor=actor, company=company, data={
        "code": "WH-AREA", "name": "原仓库位置", "location_type": "warehouse",
    })
    client.force_login(actor)
    edit_url = reverse("masterdata:location-edit", args=[legacy.pk])
    form_page = client.get(edit_url)
    assert form_page.status_code == 200
    assert 'name="location_type"' not in form_page.content.decode()

    saved = client.post(edit_url, {
        "code": legacy.code, "name": "更新后的仓库位置", "parent": "",
        "location_type": "site",
    })
    assert saved.status_code == 302
    legacy.refresh_from_db()
    assert legacy.name == "更新后的仓库位置"
    assert legacy.location_type == "warehouse"
    assert compute_initialization_progress(company)["locations_configured"]
