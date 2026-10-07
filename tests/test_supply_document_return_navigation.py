from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from tests.test_asset_list_return_navigation import Links
from tests.test_sprint3_support import grant_scope
from tests.test_sprint15_support import (
    make_company, make_department, make_employee, make_issue_document,
    make_supply_category, make_supply_document, make_supply_item,
    make_supply_warehouse, make_user, seed_supply_stock,
)
from apps.supplies.services import post_supply_document


pytestmark = pytest.mark.django_db


@pytest.fixture
def document_context():
    company = make_company()
    actor = make_user("document-return-warehouse", "warehouse")
    category = make_supply_category(company)
    warehouse = make_supply_warehouse(company)
    item = make_supply_item(company, category, name='导航耗材 & + / ? = % # "型号"')
    return {"company": company, "actor": actor, "warehouse": warehouse, "item": item}


def back_url(response):
    return next(href for href, label in Links(response).links if label.strip() == "返回")


def test_document_list_detail_back_retains_full_filters_page_and_special_query(client, document_context):
    context = document_context
    for index in range(26):
        make_supply_document(**context, document_type="receipt", key=f"return-document-{index}")
    client.force_login(context["actor"])
    listing = client.get(reverse("supplies:document-list"), {
        "q": context["item"].name, "document_type": "receipt", "status": "draft",
        "warehouse": str(context["warehouse"].pk), "item": context["item"].item_code,
        "date_from": "2026-08-26", "date_to": "2026-08-26", "page": "2",
    })
    assert listing.status_code == 200 and not listing.context["filter_errors"]
    assert listing.context["page_obj"].number == 2
    assert listing.context["page_obj"].paginator.count == 26
    ids = [document.pk for document in listing.context["page_obj"]]
    assert len(ids) == 1
    target = listing.wsgi_request.get_full_path()
    links = Links(listing).hrefs_for_path(reverse("supplies:document-detail", args=[ids[0]]))
    assert len(links) == 2  # The document number and the View link.
    for href in links:
        assert parse_qs(urlsplit(href).query) == {"return_to": [target]}
        detail = client.get(href)
        assert detail.status_code == 200 and back_url(detail) == target
        assert "返回原清单" not in detail.content.decode()  # One unambiguous list-return action.
    returned = client.get(back_url(detail))
    assert returned.status_code == 200 and returned.wsgi_request.GET == listing.wsgi_request.GET
    assert returned.context["page_obj"].number == 2
    assert [document.pk for document in returned.context["page_obj"]] == ids


def test_return_intent_survives_document_detail_navigation(client, document_context):
    context = document_context
    department = make_department(context["company"])
    seed_supply_stock(**context, key="return-navigation-stock")
    issue = make_issue_document(**context, department=department, key="return-navigation-issue")
    post_supply_document(actor=context["actor"], document=issue)
    client.force_login(context["actor"])
    listing = client.get(reverse("supplies:document-list"), {
        "intent": "consumable_return", "q": context["item"].name,
        "warehouse": str(context["warehouse"].pk), "page": "1",
    })
    assert listing.status_code == 200 and listing.context["return_intent"]
    assert [row.pk for row in listing.context["page_obj"]] == [issue.pk]
    href = Links(listing).hrefs_for_path(reverse("supplies:document-detail", args=[issue.pk]))[0]
    detail = client.get(href)
    assert detail.status_code == 200 and back_url(detail) == listing.wsgi_request.get_full_path()
    returned = client.get(back_url(detail))
    assert returned.status_code == 200 and returned.context["return_intent"]
    assert returned.context["selected_document_type"] == "issue"
    assert returned.context["selected_status"] == "posted"
    assert [row.pk for row in returned.context["page_obj"]] == [issue.pk]


def test_detail_return_rejects_unsafe_urls_and_preserves_other_allowed_workflow(client, document_context):
    document = make_supply_document(**document_context, document_type="receipt")
    client.force_login(document_context["actor"])
    detail_path = reverse("supplies:document-detail", args=[document.pk])
    fallback = reverse("supplies:document-list")
    for target in (None, "https://example.com/supplies/documents/", "//example.com/", "/\\example.com/",
                   fallback + "?q=bad\nvalue", "supplies/documents/", "/missing-page/", reverse("logout"),
                   reverse("supplies:document-edit", args=[document.pk]), detail_path,
                   fallback + "?q=" + "x" * 3000):
        response = client.get(detail_path, {} if target is None else {"return_to": target})
        assert response.status_code == 200 and back_url(response) == fallback, target
        assert "返回原清单" not in response.content.decode()
    original_workflow = reverse("offboarding:clearance-list") + "?status=unfinished"
    response = client.get(detail_path, {"return_to": original_workflow})
    assert response.status_code == 200 and back_url(response) == fallback
    assert (original_workflow, "返回原清单") in [(href, label.strip()) for href, label in Links(response).links]


def test_saved_return_address_rechecks_current_department_scope(client, document_context):
    context = document_context
    department = make_department(context["company"])
    other_department = make_department(context["company"], "RETURN-OTHER")
    employee = make_employee(context["company"], department)
    other_employee = make_employee(context["company"], other_department, "RETURN-OTHER-EMP")
    inside = make_issue_document(**context, department=department, employee=employee, key="return-inside")
    outside = make_issue_document(**context, department=other_department, employee=other_employee, key="return-outside")
    client.force_login(context["actor"])
    listing = client.get(reverse("supplies:document-list"), {"q": context["item"].name, "status": "draft"})
    assert listing.status_code == 200 and listing.context["page_obj"].paginator.count == 2
    href = Links(listing).hrefs_for_path(reverse("supplies:document-detail", args=[inside.pk]))[0]
    saved_back_url = back_url(client.get(href))
    assert saved_back_url == listing.wsgi_request.get_full_path()
    context["actor"].groups.set([Group.objects.get_or_create(name="department_manager")[0]])
    grant_scope(context["actor"], context["company"], department, descendants=False)
    returned = client.get(saved_back_url)
    assert returned.status_code == 200
    assert [row.pk for row in returned.context["page_obj"]] == [inside.pk]
    assert outside.document_no not in returned.content.decode()
    assert client.get(reverse("supplies:document-detail", args=[outside.pk])).status_code == 404
