from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.models import Department
from apps.supplies.models import SupplyDocument, SupplyStockBalance, SupplyStockLedger
from tests.test_supply_stock_query_workspace import stock_query_context
from tests.test_sprint15_support import make_company, make_supply_category, make_supply_item, make_supply_warehouse, make_user


pytestmark = pytest.mark.django_db


def stock_links(client, context):
    source=context[2]
    listing=client.get(reverse("supplies:stock-balance-list"),{"warehouse":str(source.pk),"item_type":"durable_quantity","quantity_state":"available","page":2})
    return listing,listing.context["page_obj"][0]


def test_stock_row_prefills_only_exact_source_and_item_without_saving_quantity_or_financial_fields(client,stock_query_context):
    listing,row=stock_links(client,stock_query_context)
    origin=listing.wsgi_request.get_full_path()
    before=(SupplyDocument.objects.count(),SupplyStockLedger.objects.count(),AuditLog.objects.count())
    for url,kind in ((row.issue_draft_url,"issue"),(row.transfer_draft_url,"transfer")):
        assert urlsplit(url).path==reverse("supplies:document-create",args=[kind])
        params=parse_qs(urlsplit(url).query)
        assert params["stock_warehouse"]==[str(row.warehouse_id)] and params["stock_item"]==[str(row.item_id)]
        assert params["return_to"]==[origin]
        page=client.get(url)
        assert page.status_code==200 and page.context["document_form_back_url"]==origin
        assert str(page.context["form"]["source_warehouse"].value())==str(row.warehouse_id)
        assert str(page.context["formset"].forms[0]["item"].value())==str(row.item_id)
        assert page.context["formset"].forms[0]["quantity"].value() is None
        assert "entered_unit_cost" not in page.context["formset"].forms[0].fields
        if kind=="transfer":
            assert not page.context["form"]["target_warehouse"].value()
        else:
            assert not page.context["form"]["department"].value() and not page.context["form"]["employee"].value()
        assert "3.0000 把" in page.content.decode() and "尚未保存" in page.content.decode()
    assert (SupplyDocument.objects.count(),SupplyStockLedger.objects.count(),AuditLog.objects.count())==before


def test_prefill_rejects_foreign_inactive_malformed_and_nonexistent_stock_pairs(client,stock_query_context):
    company,_,source,target,paper,chair=stock_query_context
    route=reverse("supplies:document-create",args=["issue"])
    foreign=make_company("PREFILL-FOREIGN",active=False)
    foreign_item=make_supply_item(foreign,make_supply_category(foreign),"FOREIGN")
    foreign_warehouse=make_supply_warehouse(foreign)
    before=SupplyDocument.objects.count()
    for params in ({"stock_warehouse":str(source.pk),"stock_item":str(foreign_item.pk)},
                   {"stock_warehouse":str(foreign_warehouse.pk),"stock_item":str(paper.pk)},
                   {"stock_warehouse":str(target.pk),"stock_item":str(chair.pk)},
                   {"stock_warehouse":"invalid","stock_item":str(paper.pk)},
                   {"stock_warehouse":str(source.pk)}):
        assert client.get(route,params).status_code==404
    chair.is_active=False
    chair.save(update_fields=["is_active"])
    assert client.get(route,{"stock_warehouse":str(source.pk),"stock_item":str(chair.pk)}).status_code==404
    source.is_active=False
    source.save(update_fields=["is_active"])
    assert client.get(route,{"stock_warehouse":str(source.pk),"stock_item":str(paper.pk)}).status_code==404
    assert SupplyDocument.objects.count()==before


def test_stock_opening_entries_keep_existing_roles_and_hide_zero_or_inactive_rows(client,stock_query_context):
    company,_,source,_,_,chair=stock_query_context
    for role in ("management","equipment"):
        client.force_login(make_user(f"stock-prefill-{role}",role))
        page=client.get(reverse("supplies:stock-balance-list"))
        assert all(not row.issue_draft_url and not row.transfer_draft_url for row in page.context["page_obj"])
        assert "新建领用草稿" not in page.content.decode()
        assert client.get(reverse("supplies:document-create",args=["issue"]),{"stock_warehouse":str(source.pk),"stock_item":str(chair.pk)}).status_code==403
    client.force_login(stock_query_context[1])
    page=client.get(reverse("supplies:stock-balance-list"))
    zero=next(row for row in page.context["page_obj"] if row.quantity_on_hand==0)
    assert not zero.issue_draft_url and not zero.transfer_draft_url
    chair.is_active=False
    chair.save(update_fields=["is_active"])
    inactive=client.get(reverse("supplies:stock-balance-list"),{"item":chair.item_code})
    assert all(not row.issue_draft_url for row in inactive.context["page_obj"])


def test_prefill_post_uses_changed_inputs_keeps_errors_and_only_creates_original_neutral_draft(client,stock_query_context):
    company,_,source,_,paper,_=stock_query_context
    listing,row=stock_links(client,stock_query_context)
    page=client.get(row.issue_draft_url)
    before=(list(SupplyStockBalance.objects.order_by("pk").values()),SupplyStockLedger.objects.count())
    data={"source_warehouse":str(source.pk),"department":str(Department.objects.filter(company=company,is_active=True).first().pk),
        "employee":"","business_date":"2026-10-04","remark":"从库存开单后更换物品",
        "idempotency_key":page.context["form"]["idempotency_key"].value(),"return_to":listing.wsgi_request.get_full_path(),
        "next_action":"detail","lines-TOTAL_FORMS":"1","lines-INITIAL_FORMS":"0","lines-MIN_NUM_FORMS":"0","lines-MAX_NUM_FORMS":"100",
        "lines-0-item":str(paper.pk),"lines-0-quantity":"","lines-0-line_remark":""}
    invalid=client.post(row.issue_draft_url,data)
    assert invalid.status_code==200 and invalid.context["formset"].forms[0].errors["quantity"]
    assert str(invalid.context["formset"].forms[0]["item"].value())==str(paper.pk)
    assert invalid.context["document_form_back_url"]==listing.wsgi_request.get_full_path()
    saved=client.post(row.issue_draft_url,{**data,"lines-0-quantity":"1.2345"})
    assert saved.status_code==302
    document=SupplyDocument.objects.get(idempotency_key=data["idempotency_key"])
    assert document.status=="draft" and document.source_warehouse_id==source.pk
    assert document.lines.get().item_id==paper.pk and document.lines.get().quantity==Decimal("1.2345")
    assert document.lines.get().entered_unit_cost is None
    assert parse_qs(urlsplit(saved.url).query)["return_to"]==[listing.wsgi_request.get_full_path()]
    assert (list(SupplyStockBalance.objects.order_by("pk").values()),SupplyStockLedger.objects.count())==before
