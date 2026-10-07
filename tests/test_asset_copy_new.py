"""Copying physical information is a scoped, unsaved prefill with fresh identity."""
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from django.urls import reverse

from apps.assets.models import Asset, AssetRegistration, AssetQrIdentity, AttachmentLink
from apps.assets.registration import create_registered_asset
from apps.finance.models import AssetFinance
from apps.finance.services import confirm_asset_finance
from apps.masterdata.models import IssuedCode, SequenceCounter, UserDepartmentScope
from tests.test_asset_edit_revision import browser_payload
from tests.test_asset_registration_finance_separation import context, physical_data, registered, activate
from tests.test_sprint3_support import add_photo, make_custom_field, make_department, make_employee, make_user
from tests.test_sprint7_support import add_department_manager

pytestmark = pytest.mark.django_db


def copy_url(asset):
    return reverse("assets:asset-create") + "?" + urlencode({"copy_from": asset.pk})


def identity_state():
    return {
        "assets": Asset.objects.count(), "registration": AssetRegistration.objects.count(),
        "qr": AssetQrIdentity.objects.count(), "codes": IssuedCode.objects.count(),
        "counters": list(SequenceCounter.objects.order_by("pk").values()),
    }


def test_copy_get_is_unsaved_and_native_create_has_fresh_identity_no_history(client, context):
    custom = make_custom_field(context["company"], context["category"], "COPY-PRIVATE-REFERENCE", "text")
    source = create_registered_asset(actor=context["equipment"], company=context["company"],
        data=physical_data(context, asset_name="同型号设备", brand="品牌 A", model="MODEL-18", manufacturer="制造商 A",
            serial_number="SOURCE-SERIAL-ONLY", factory_number="SOURCE-FACTORY-ONLY", equipment_number="SOURCE-EQ-ONLY",
            historical_code="SOURCE-HISTORY-ONLY", description="通用规格说明", notes="仅此件的历史备注",
            vehicle_plate="SOURCE-PLATE-ONLY", chassis_number="SOURCE-CHASSIS-ONLY",
            calibration_number="SOURCE-CALIBRATION-ONLY", is_maintenance_required=True),
        custom_values={custom.pk: "SOURCE-CUSTOM-ONLY"}, idempotency_key="copy-new-source")
    activate(context, source)
    add_photo(context["equipment"], source)
    confirm_asset_finance(actor=context["finance"], asset=source,
        finance_data={"accounting_treatment": "controlled_non_fixed", "original_cost": Decimal("42789.51"),
                      "accounting_treatment_reason": "受控非固定资产测试，验证复制不会带入财务资料"},
        idempotency_key="copy-new-source-finance", reason="来源资产已有财务资料")
    client.force_login(context["equipment"])
    original = Asset._base_manager.filter(pk=source.pk).values().get()
    old_finance = AssetFinance._base_manager.filter(asset=source).values().get()
    before = identity_state()
    page = client.get(copy_url(source))
    assert page.status_code == 200 and page.context["asset"] is None
    assert page.context["copy_source_asset"].pk == source.pk
    assert page.context["form"].instance._state.adding and not page.context["form"].is_bound
    assert page.context["cancel_url"] == reverse("assets:asset-detail", args=[source.pk])
    for field in ("asset_name", "brand", "model", "manufacturer", "description", "unit", "is_maintenance_required"):
        assert page.context["form"][field].value() == getattr(source, field)
    for field in ("serial_number", "factory_number", "equipment_number", "historical_code", "vehicle_plate",
                  "chassis_number", "calibration_number", "notes", "acquisition_date", "commissioning_date",
                  "management_attribute", "coding_year", "coding_year_note", "component_of"):
        assert page.context["form"][field].value() in (None, "")
    assert page.context["custom_value_forms"][0]["value"].value() in (None, "")
    assert "42789.51" not in page.content.decode() and "SOURCE-CUSTOM-ONLY" not in page.content.decode()
    first_key = str(page.context["form"]["idempotency_key"].value())
    assert str(client.get(copy_url(source)).context["form"]["idempotency_key"].value()) != first_key
    assert identity_state() == before
    data = browser_payload(page)
    data.update(copy_from=str(source.pk), asset_action="register", asset_name="新建的第二件设备", brand="用户重新核对的品牌")
    invalid = {**data, "asset_name": ""}
    rejected = client.post(copy_url(source), invalid)
    assert rejected.status_code == 200 and "asset_name" in rejected.context["form"].errors
    assert rejected.context["form"]["brand"].value() == invalid["brand"]
    assert str(rejected.context["form"]["idempotency_key"].value()) == first_key
    assert rejected.context["copy_source_asset"].pk == source.pk and identity_state() == before
    saved = client.post(copy_url(source), data)
    assert saved.status_code == 302
    clone = Asset.objects.get(asset_name=data["asset_name"])
    assert clone.pk != source.pk and clone.asset_code != source.asset_code and clone.asset_code
    assert clone.current_issued_code_id != source.current_issued_code_id
    assert clone.qr_identities.get(status="active").public_token != source.qr_identities.get(status="active").public_token
    assert clone.asset_status == "pending_label" and clone.brand == data["brand"]
    assert not AttachmentLink.objects.filter(asset=clone).exists()
    assert not AssetFinance.objects.filter(asset=clone).exists() and not clone.movements.exists()
    assert not clone.custom_values.exists()
    assert Asset._base_manager.filter(pk=source.pk).values().get() == original
    assert AssetFinance._base_manager.filter(asset=source).values().get() == old_finance
    assert client.post(copy_url(source), data).url == saved.url
    assert Asset.objects.count() == before["assets"] + 1


def test_copy_omits_inactive_references_and_keeps_source_read_only(client, context):
    source = registered(context, key="copy-inactive-source")
    context["employee"].is_active = False
    context["employee"].save(update_fields=["is_active"])
    context["category"].is_active = False
    context["category"].save(update_fields=["is_active"])
    context["location"].is_active = False
    context["location"].save(update_fields=["is_active"])
    client.force_login(context["equipment"])
    before = identity_state()
    page = client.get(copy_url(source))
    assert page.status_code == 200 and page.context["selected_category"] is None
    assert set(page.context["copy_omitted_fields"]) == {"实物分类", "责任人", "当前位置"}
    for name in ("category", "responsible_employee", "location"):
        assert page.context["form"][name].value() is None
    assert "已不在当前可选范围" in page.content.decode() and identity_state() == before


def test_copy_checks_source_scope_and_create_permission_on_get_and_post(client, context):
    source = registered(context, key="copy-permission-source")
    other_department = make_department(context["company"], "COPY-OTHER")
    other_employee = make_employee(context["company"], other_department, "COPY-OTHER-E")
    outside = create_registered_asset(actor=context["equipment"], company=context["company"],
        data=physical_data(context, department=other_department, responsible_employee=other_employee),
        idempotency_key="copy-outside-source")
    manager = add_department_manager(context, "COPY-OWNER", context["department"])
    client.force_login(manager)
    page = client.get(copy_url(source))
    assert page.status_code == 200
    data = browser_payload(page)
    data.update(copy_from=str(source.pk), asset_action="register")
    assert client.get(copy_url(outside)).status_code == 404
    assert client.post(copy_url(outside), {**data, "copy_from": str(outside.pk)}).status_code == 404
    UserDepartmentScope.objects.filter(user=manager).delete()
    assert client.post(copy_url(source), data).status_code == 404
    for role in ("management", "hr"):
        reader = make_user(f"copy-{role}-reader", role)
        client.force_login(reader)
        detail = client.get(reverse("assets:asset-detail", args=[source.pk]))
        assert detail.status_code == 200 and "复制资料新增" not in detail.content.decode()
        assert client.get(copy_url(source)).status_code == 403
        assert client.post(copy_url(source), data).status_code == 403
    client.force_login(context["equipment"])
    assert client.get(reverse("assets:asset-create"), {"copy_from": "invalid"}).status_code == 404
    assert client.get(reverse("assets:asset-create"), {"copy_from": source.pk, "component_of": source.pk}).status_code == 400
    assert Asset.objects.count() == 2
