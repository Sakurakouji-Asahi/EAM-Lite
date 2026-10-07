from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.models import Company
from apps.supplies.models import SupplyItem, SupplyStockBalance, SupplyStockLedger, SupplyCustody
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import make_supply_category, make_supply_item, make_supply_warehouse, make_user, seed_supply_stock


pytestmark = pytest.mark.django_db


@pytest.fixture
def copy_context(client):
    company, actor, _, _, warehouse, _, paper, source = supply_context()
    source.name = "资料复用办公椅"
    source.brand = "复用品牌"
    source.model = "MODEL-COPY"
    source.specification = "25 × 30"
    source.minimum_stock_quantity = Decimal("1.2345")
    source.default_warehouse = warehouse
    source.remark = "来源档案专用说明"
    source.save()
    seed_supply_stock(actor=actor, company=company, warehouse=warehouse, item=source,
        quantity="5.4321", unit_cost="10", key="item-copy-source-stock")
    client.force_login(actor)
    return company, actor, warehouse, paper, source


def item_data(source, code):
    return {"item_code": code, "name": source.name, "category": str(source.category_id),
        "item_type": source.item_type, "unit": source.unit, "specification": source.specification,
        "model": source.model, "brand": source.brand, "minimum_stock_quantity": str(source.minimum_stock_quantity),
        "default_warehouse": str(source.default_warehouse_id) if source.default_warehouse_id else "", "remark": ""}


def test_copy_preview_prefills_shared_values_leaves_identity_blank_and_does_not_write(client, copy_context):
    source = copy_context[-1]
    before = (SupplyItem.objects.count(), SupplyStockLedger.objects.count(), AuditLog.objects.count())
    list_url = reverse("supplies:item-list") + "?q=COPY&status=all&page=2"
    response = client.get(reverse("supplies:item-create"), {"copy_from": str(source.pk), "return_to": list_url})
    assert response.status_code == 200 and response.context["copy_source"].pk == source.pk
    form = response.context["form"]
    assert not form["item_code"].value() and not form["remark"].value()
    assert form["name"].value() == source.name and form["unit"].value() == source.unit
    assert form["minimum_stock_quantity"].value() == Decimal("1.2345")
    assert str(form["category"].value()) == str(source.category_id)
    assert str(form["default_warehouse"].value()) == str(source.default_warehouse_id)
    assert not form.fields["unit"].disabled and "is_active" not in form.initial
    assert response.context["item_list_url"] == list_url
    assert "当前表单尚未保存" in response.content.decode()
    assert (SupplyItem.objects.count(), SupplyStockLedger.objects.count(), AuditLog.objects.count()) == before


def test_copy_save_creates_new_archive_only_and_preserves_source_and_return_query(client, copy_context):
    source = copy_context[-1]
    original = SupplyItem.objects.filter(pk=source.pk).values().get()
    before = SupplyStockLedger.objects.count()
    audits = AuditLog.objects.filter(action="supply_item_create").count()
    list_url = reverse("supplies:item-list") + "?q=COPY&category=" + str(source.category_id) + "&status=all&page=2"
    response = client.post(reverse("supplies:item-create"), {**item_data(source, "COPY-NEW"),
        "name": "复制后的新规格办公椅", "copy_from": str(source.pk), "return_to": list_url})
    assert response.status_code == 302
    created = SupplyItem.objects.get(company=source.company, normalized_item_code="copy-new")
    assert created.pk != source.pk and created.is_active and created.name == "复制后的新规格办公椅"
    assert created.brand == source.brand and created.minimum_stock_quantity == source.minimum_stock_quantity
    assert created.created_by_id == copy_context[1].pk
    assert urlsplit(response.url).path == reverse("supplies:item-detail", args=[created.pk])
    assert parse_qs(urlsplit(response.url).query)["return_to"] == [list_url]
    assert SupplyItem.objects.filter(pk=source.pk).values().get() == original
    assert not SupplyStockBalance.objects.filter(item=created).exists()
    assert not SupplyCustody.objects.filter(item=created).exists()
    assert SupplyStockLedger.objects.count() == before
    assert AuditLog.objects.filter(action="supply_item_create").count() == audits + 1


def test_invalid_copy_keeps_changed_input_and_automatic_numbering_remains_available(client, copy_context):
    source = copy_context[-1]
    count = SupplyItem.objects.count()
    data = {**item_data(source, ""), "copy_from": str(source.pk), "unit": "盒", "name": "待核对的新名称", "minimum_stock_quantity": "-1"}
    invalid = client.post(reverse("supplies:item-create"), data)
    assert invalid.status_code == 200 and invalid.context["form"].errors["minimum_stock_quantity"]
    assert invalid.context["form"]["unit"].value() == "盒"
    assert invalid.context["form"]["name"].value() == "待核对的新名称"
    assert 'data-unsaved-guard="true"' in invalid.content.decode()
    duplicate = client.post(reverse("supplies:item-create"), {**data, "item_code": source.item_code, "minimum_stock_quantity": "1.2345"})
    assert duplicate.status_code == 200 and duplicate.context["form"].errors
    assert SupplyItem.objects.count() == count
    source.refresh_from_db()
    assert source.unit == "把"
    automatic = client.post(reverse("supplies:item-create"), {**data, "name": "自动编号的新物品", "minimum_stock_quantity": "1.2345"})
    assert automatic.status_code == 302
    created = SupplyItem.objects.get(company=source.company, name="自动编号的新物品")
    assert created.pk != source.pk and created.item_code and created.item_code != source.item_code
    assert created.unit == "盒"


def test_copy_keeps_existing_permissions_and_rechecks_them_at_submission(client, copy_context):
    company, _, _, paper, source = copy_context
    equipment = make_user("copy-item-equipment", "equipment")
    client.force_login(equipment)
    create = reverse("supplies:item-create")
    assert client.get(create, {"copy_from": str(source.pk)}).status_code == 200
    assert client.get(create, {"copy_from": str(paper.pk)}).status_code == 403
    equipment.groups.clear()
    before = SupplyItem.objects.filter(company=company).count()
    assert client.post(create, {**item_data(source, "COPY-UNAUTHORIZED"), "copy_from": str(source.pk)}).status_code == 403
    assert SupplyItem.objects.filter(company=company).count() == before
    for role in ("management", "employee"):
        client.force_login(make_user(f"copy-item-denied-{role}", role))
        assert client.get(create, {"copy_from": str(source.pk)}).status_code == 403
        if role == "management":
            detail = client.get(reverse("supplies:item-detail", args=[source.pk]))
            assert "复制资料新增" not in detail.content.decode()


def test_inactive_copy_relations_are_not_prefilled_and_foreign_source_is_rejected(client, copy_context):
    company, _, _, _, source = copy_context
    inactive_category = make_supply_category(company, "COPY-INACTIVE-CAT")
    inactive_warehouse = make_supply_warehouse(company, "COPY-INACTIVE-WH")
    archived = make_supply_item(company, inactive_category, "COPY-ARCHIVED", item_type="durable_quantity", unit="把")
    archived.default_warehouse = inactive_warehouse
    archived.is_active = False
    archived.save()
    inactive_category.is_active = False
    inactive_category.save()
    inactive_warehouse.is_active = False
    inactive_warehouse.save()
    preview = client.get(reverse("supplies:item-create"), {"copy_from": str(archived.pk)})
    assert preview.status_code == 200 and len(preview.context["copy_notices"]) == 2
    assert not preview.context["form"]["category"].value()
    assert not preview.context["form"]["default_warehouse"].value()
    created = client.post(reverse("supplies:item-create"), {**item_data(source, "COPY-FROM-ARCHIVED"), "copy_from": str(archived.pk)})
    assert created.status_code == 302 and SupplyItem.objects.get(item_code="COPY-FROM-ARCHIVED").is_active
    foreign_company = Company.objects.create(code="COPY-FOREIGN", name="另一公司", short_name="CF", is_active=False)
    foreign = make_supply_item(foreign_company, make_supply_category(foreign_company), "FOREIGN")
    count = SupplyItem.objects.count()
    assert client.get(reverse("supplies:item-create"), {"copy_from": str(foreign.pk)}).status_code == 404
    assert client.post(reverse("supplies:item-create"), {**item_data(source, "COPY-FOREIGN-POST"), "copy_from": str(foreign.pk)}).status_code == 404
    assert SupplyItem.objects.count() == count


def test_normal_create_and_edit_keep_original_list_query_and_return_urls_are_local(client, copy_context):
    source = copy_context[-1]
    list_url = reverse("supplies:item-list") + "?q=PAPER&item_type=consumable&page=2"
    created = client.post(reverse("supplies:item-create"), {**item_data(source, "NORMAL-NEW"), "return_to": list_url})
    assert created.status_code == 302 and created.url == list_url
    edited = client.post(reverse("supplies:item-edit", args=[source.pk]), {**item_data(source, source.item_code),
        "name": "编辑后物品名称", "return_to": list_url})
    assert edited.status_code == 302 and edited.url == list_url
    for target in ("https://example.org/supplies/items/", "//example.org/supplies/items/", "/supplies/items/new/"):
        form = client.get(reverse("supplies:item-create"), {"copy_from": str(source.pk), "return_to": target})
        assert form.context["item_list_url"] == reverse("supplies:item-list")
