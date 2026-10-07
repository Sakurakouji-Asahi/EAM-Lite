from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.supplies.models import SupplyCustody, SupplyCustodyMovement, SupplyStockLedger
from apps.supplies.services import post_supply_document, return_custody_to_warehouse, write_off_custody
from tests.test_sprint16_services import issued_custody
from tests.test_sprint15_support import make_user, make_employee, make_supply_item, make_issue_document, seed_supply_stock


pytestmark = pytest.mark.django_db


@pytest.fixture
def personal_context(client):
    company, actor, department, employee, source, target, chair, original, partial = issued_custody(quantity="3.4567", unit_cost="10")
    user = make_user("personal-custody-employee", "employee")
    employee.user = user
    employee.save(update_fields=["user"])
    chair.name = "MY+ & 保管椅"
    chair.save(update_fields=["name"])
    returned = return_custody_to_warehouse(actor=actor, custody=partial, target_warehouse=target,
        quantity=Decimal("1.2345"), business_date=date(2026, 8, 26), reason="部分归还经过",
        idempotency_key="personal-partial-return")
    post_supply_document(actor=actor, document=returned)
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=chair,
        quantity="5", unit_cost="10", key="personal-extra-opening")
    def issue(item, quantity, person, key):
        document = make_issue_document(actor=actor, company=company, warehouse=source, item=item,
            department=department, employee=person, quantity=quantity, key=key)
        post_supply_document(actor=actor, document=document)
        return SupplyCustody.objects.get(origin_issue_line=document.lines.get())
    closed = issue(chair,"1.1111",employee,"personal-closed")
    full = return_custody_to_warehouse(actor=actor, custody=closed, target_warehouse=target,
        quantity=Decimal("1.1111"), business_date=date(2026, 8, 26), reason="全部归还经过",
        idempotency_key="personal-full-return")
    post_supply_document(actor=actor, document=full)
    loss = issue(chair,"1.0001",employee,"personal-loss")
    write_off_custody(actor=actor, custody=loss, quantity=Decimal("1.0001"), action="loss",
        business_date=date(2026, 8, 26), reason="报损结清经过", idempotency_key="personal-loss-close")
    desk = make_supply_item(company, chair.category, "MY-DESK", name="MY 个人办公桌", item_type="durable_quantity", unit="张")
    seed_supply_stock(actor=actor, company=company, warehouse=source, item=desk,
        quantity="1", unit_cost="20", key="personal-desk-opening")
    desk_custody = issue(desk,"0.5001",employee,"personal-desk")
    other_employee = make_employee(company,department,"PERSONAL-OTHER")
    other = issue(chair,"0.6001",other_employee,"personal-other")
    client.force_login(user)
    return {"actor":actor,"user":user,"chair":chair,"desk":desk,"partial":partial,"closed":closed,
        "loss":loss,"desk_custody":desk_custody,"other":other,"original":original}


def test_personal_status_history_and_per_item_quantities_keep_default_open_and_never_mix_units(client,personal_context):
    response=client.get(reverse("supplies:my-custodies"))
    assert response.status_code==200 and response.context["selected_status"]=="open"
    assert response.context["custody_personal_counts"]=={"total":4,"open":2,"closed":2}
    assert response.context["page_obj"].paginator.count==2
    quantities={row["item_id"]:(row["quantity"],row["item__unit"]) for row in response.context["custody_open_quantities"]}
    assert quantities=={personal_context["chair"].pk:(Decimal("2.2222"),"把"),personal_context["desk"].pk:(Decimal("0.5001"),"张")}
    assert "2.7223" not in response.content.decode()
    assert not response.context["show_cost"] and "当前金额" not in response.content.decode()
    for status,count in (("all",4),("closed",2)):
        page=client.get(reverse("supplies:my-custodies"),{"status":status})
        assert page.context["page_obj"].paginator.count==count
        assert page.context["custody_personal_counts"]==response.context["custody_personal_counts"]
    assert "已归还" not in page.content.decode()


def test_personal_keyword_and_status_links_keep_query_reset_page_and_find_direct_source(client,personal_context):
    page=client.get(reverse("supplies:my-custodies"),{"q":"MY+ &","status":"closed","page":2})
    assert page.context["custody_personal_counts"]=={"total":3,"open":1,"closed":2}
    assert page.context["page_obj"].paginator.count==2
    for row in page.context["custody_status_links"]:
        params=parse_qs(urlsplit(row["url"]).query)
        assert params["q"]==["MY+ &"] and params["status"]==[row["value"]] and "page" not in params
    source=client.get(reverse("supplies:my-custodies"),{"q":personal_context["original"].document_no,"status":"all"})
    assert [custody.pk for custody in source.context["page_obj"]]==[personal_context["partial"].pk]
    empty=client.get(reverse("supplies:my-custodies"),{"q":"不存在的个人物品","status":"all"})
    assert empty.context["custody_personal_counts"]["total"]==0 and not empty.context["custody_open_quantities"]
    assert "查看本人全部记录" in empty.content.decode()


def test_custody_details_keep_personal_or_team_query_and_show_exact_history_units(client,personal_context):
    origin=reverse("supplies:my-custodies")+"?q=CHAIR&status=closed&page=2"
    page=client.get(origin)
    detail=client.get(page.context["page_obj"][0].ui_detail_url)
    assert detail.status_code==200 and detail.context["custody_list_url"]==origin
    assert "返回我的保管" in detail.content.decode()
    assert "1.1111 把" in detail.content.decode() or "1.0001 把" in detail.content.decode()
    client.force_login(personal_context["actor"])
    listing=client.get(reverse("supplies:custody-list"),{"item":"CHAIR","status":"open","page":2})
    team_origin=listing.wsgi_request.get_full_path()
    team_detail=client.get(listing.context["page_obj"][0].ui_detail_url)
    assert team_detail.context["custody_list_url"]==team_origin and "返回保管清单" in team_detail.content.decode()
    for target in ("https://example.org/supplies/custodies/","//example.org/supplies/custodies/","/supplies/items/new/"):
        rejected=client.get(reverse("supplies:custody-detail",args=[personal_context["partial"].pk]),{"return_to":target})
        assert rejected.context["custody_list_url"]==reverse("supplies:custody-list")


def test_personal_history_stays_with_the_signed_in_employee_and_is_read_only(client,personal_context):
    before=(list(SupplyCustody.objects.order_by("pk").values()),SupplyCustodyMovement.objects.count(),SupplyStockLedger.objects.count())
    page=client.get(reverse("supplies:my-custodies"),{"status":"all","employee":personal_context["other"].employee_id})
    assert page.context["page_obj"].paginator.count==4
    assert personal_context["other"].pk not in {row.pk for row in page.context["page_obj"]}
    assert client.get(reverse("supplies:custody-detail",args=[personal_context["other"].pk])).status_code==404
    assert (list(SupplyCustody.objects.order_by("pk").values()),SupplyCustodyMovement.objects.count(),SupplyStockLedger.objects.count())==before
