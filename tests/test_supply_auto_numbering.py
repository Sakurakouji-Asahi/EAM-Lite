import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.supplies.forms import (
    SupplyCategoryForm,
    SupplyItemForm,
    SupplyWarehouseForm,
)
from apps.supplies.models import SupplyCategory, SupplyItem, SupplyWarehouse
from apps.supplies.services import (
    create_supply_category,
    create_supply_item,
    create_supply_warehouse,
    update_supply_category,
    update_supply_item,
    update_supply_warehouse,
)
from tests.test_sprint13_support import (
    make_company,
    make_supply_category,
    make_user,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def context():
    company = make_company()
    actor = make_user("supply-auto-number", "warehouse")
    category = make_supply_category(company)
    return company, actor, category


CASES = (
    ("category", SupplyCategory, "code", "SC", SupplyCategoryForm),
    ("warehouse", SupplyWarehouse, "code", "WH", SupplyWarehouseForm),
    ("durable_quantity", SupplyItem, "item_code", "LVD", SupplyItemForm),
    ("consumable", SupplyItem, "item_code", "LVC", SupplyItemForm),
)


def _data(kind, category, **overrides):
    data = {"name": "自动编号档案"}
    if kind not in {"category", "warehouse"}:
        data.update(category=category, item_type=kind, unit="个")
    data.update(overrides)
    return data


def _create(kind, company, actor, data):
    service = {
        "category": create_supply_category,
        "warehouse": create_supply_warehouse,
    }.get(kind, create_supply_item)
    return service(company=company, actor=actor, data=data)


def _update(kind, instance, actor, data):
    service, argument = {
        "category": (update_supply_category, "category"),
        "warehouse": (update_supply_warehouse, "warehouse"),
    }.get(kind, (update_supply_item, "item"))
    return service(actor=actor, data=data, **{argument: instance})


@pytest.mark.parametrize("kind,model,field,prefix,form_class", CASES)
def test_supplies_auto_numbers_and_manual_numbers_are_saved_and_audited(
    context, kind, model, field, prefix, form_class
):
    company, actor, category = context
    first = _create(kind, company, actor, _data(kind, category))
    second = _create(kind, company, actor, _data(kind, category, **{field: ""}))
    manual = _create(
        kind, company, actor, _data(kind, category, **{field: " 手填-Ａ01 "})
    )

    assert getattr(first, field) == f"{prefix}000001"
    assert getattr(second, field) == f"{prefix}000002"
    assert getattr(manual, field) == "手填-A01"
    assert AuditLog.objects.get(
        object_id=str(first.pk), action=f"supply_{model._meta.model_name[6:]}_create"
    ).new_data_json[field] == f"{prefix}000001"


@pytest.mark.parametrize("kind,model,field,prefix,form_class", CASES)
def test_auto_numbers_skip_manual_numbers_and_keep_inactive_numbers_reserved(
    context, kind, model, field, prefix, form_class
):
    company, actor, category = context
    reserved = _create(
        kind,
        company,
        actor,
        _data(kind, category, **{field: f" {prefix.lower()}000001 "}),
    )
    reserved.is_active = False
    reserved.save(update_fields=("is_active", "updated_at"))

    generated = _create(kind, company, actor, _data(kind, category))

    assert getattr(generated, field) == f"{prefix}000002"
    with pytest.raises(ValidationError):
        _create(
            kind,
            company,
            actor,
            _data(kind, category, **{field: f"{prefix}000001"}),
        )
    reserved.refresh_from_db()
    assert not reserved.is_active
    assert getattr(reserved, field) == f"{prefix.lower()}000001"


@pytest.mark.parametrize("kind,model,field,prefix,form_class", CASES)
def test_edit_preserves_existing_number_and_rejects_clearing_it(
    context, kind, model, field, prefix, form_class
):
    company, actor, category = context
    instance = _create(kind, company, actor, _data(kind, category))
    original_code = getattr(instance, field)
    updated = _update(kind, instance, actor, {"name": "更新名称"})
    assert getattr(updated, field) == original_code

    with pytest.raises(ValidationError):
        _update(kind, updated, actor, {field: ""})
    updated.refresh_from_db()
    assert getattr(updated, field) == original_code
    assert updated.name == "更新名称"

    form = form_class(instance=updated, actor=actor, company=company)
    assert form.fields[field].required


@pytest.mark.parametrize("kind,model,field,prefix,form_class", CASES)
def test_http_create_accepts_blank_number_and_edit_rejects_blank(
    context, client, kind, model, field, prefix, form_class
):
    company, actor, category = context
    client.force_login(actor)
    route = kind if kind in {"category", "warehouse"} else "item"
    data = _data(kind, category, **{field: ""})
    if route == "item":
        data.update(category=str(category.pk), minimum_stock_quantity="0")
    form = form_class(data=data, actor=actor, company=company)
    assert not form.fields[field].required
    assert form.is_valid(), form.errors
    response = client.post(reverse(f"supplies:{route}-create"), data)
    assert response.status_code == 302
    instance = model.objects.get(company=company, **{field: f"{prefix}000001"})

    data["name"] = "试图清空编号"
    response = client.post(
        reverse(f"supplies:{route}-edit", args=[instance.pk]), data
    )
    assert response.status_code == 200
    assert field in response.context["form"].errors
    instance.refresh_from_db()
    assert getattr(instance, field) == f"{prefix}000001"
    assert instance.name == "自动编号档案"


def test_durable_and_consumable_numbers_have_separate_prefixes(context):
    company, actor, category = context
    durable = _create("durable_quantity", company, actor, _data("durable_quantity", category))
    consumable = _create("consumable", company, actor, _data("consumable", category))

    assert durable.item_code == "LVD000001"
    assert consumable.item_code == "LVC000001"
    assert durable.item_type == "durable_quantity"
    assert consumable.item_type == "consumable"


def test_auto_numbering_keeps_management_and_company_permissions(context):
    company, actor, category = context
    equipment = make_user("auto-number-equipment", "equipment")
    for kind in ("category", "warehouse", "consumable"):
        with pytest.raises(PermissionDenied):
            _create(kind, company, equipment, _data(kind, category))

    other = make_company("OTHER", active=False)
    with pytest.raises(PermissionDenied):
        _create("warehouse", other, actor, {"name": "越权仓库"})
    assert not SupplyWarehouse.objects.exists()
    assert not SupplyItem.objects.exists()
    assert SupplyCategory.objects.count() == 1

    durable = _create(
        "durable_quantity", company, equipment, _data("durable_quantity", category)
    )
    assert durable.item_code == "LVD000001"


def test_auto_numbering_does_not_weaken_cross_company_category_validation(context):
    company, actor, category = context
    other = make_company("OTHER", active=False)
    foreign_category = make_supply_category(other, "FOREIGN")
    with pytest.raises(ValidationError):
        _create("consumable", company, actor, _data("consumable", foreign_category))

    assert not SupplyItem.objects.exists()
    valid = _create("consumable", company, actor, _data("consumable", category))
    assert valid.item_code == "LVC000001"
