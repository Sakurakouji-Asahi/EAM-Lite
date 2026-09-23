"""Equipment number is separate from the issued asset and factory numbers."""
import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.assets.models import Asset
from apps.assets.services import update_asset_equipment_number
from apps.audit.models import AuditLog
from tests.test_sprint3_http import form_data, make_context
from tests.test_sprint3_support import make_user
from tests.test_unified_asset_identity import context, registered


pytestmark = pytest.mark.django_db(transaction=True)


def test_draft_equipment_number_is_saved_searched_displayed_and_audited(client):
    actor, company, department, employee, category, _site, _area, location = make_context()
    client.force_login(actor)
    values = form_data(
        category, department, employee, location,
        equipment_number="EQ-2026-001",
    )
    created = client.post(reverse("assets:asset-create"), values)
    assert created.status_code == 302
    asset = Asset.objects.get(company=company, asset_name="HTTP 检具")
    assert asset.equipment_number == "EQ-2026-001"
    assert asset.factory_number == "FACTORY-HTTP"
    assert asset.asset_code is None
    audit = AuditLog.objects.filter(
        object_type="Asset", object_id=str(asset.pk), action="asset_draft_create"
    ).latest("created_at")
    assert audit.new_data_json["equipment_number"] == "EQ-2026-001"

    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert detail.status_code == 200
    assert "设备编号" in detail.content.decode()
    assert "EQ-2026-001" in detail.content.decode()

    listing = client.get(reverse("assets:asset-list"), {"q": "EQ-2026-001"})
    assert listing.status_code == 200
    assert asset.pk in {item.pk for item in listing.context["page"]}

    changed = client.post(reverse("assets:asset-edit", args=[asset.pk]), {
        **values, "equipment_number": "EQ-2026-002",
    })
    assert changed.status_code == 302
    asset.refresh_from_db()
    assert asset.equipment_number == "EQ-2026-002"
    assert asset.factory_number == "FACTORY-HTTP"
    assert AuditLog.objects.filter(
        object_type="Asset", object_id=str(asset.pk), action="asset_draft_update",
        new_data_json__equipment_number="EQ-2026-002",
    ).exists()


def test_equipment_number_is_optional_and_old_records_keep_blank_value(client):
    actor, company, department, employee, category, _site, _area, location = make_context()
    client.force_login(actor)
    created = client.post(
        reverse("assets:asset-create"),
        form_data(category, department, employee, location),
    )
    assert created.status_code == 302
    asset = Asset.objects.get(company=company, asset_name="HTTP 检具")
    assert asset.equipment_number == ""
    form = client.get(reverse("assets:asset-edit", args=[asset.pk]))
    assert form.status_code == 200
    assert 'name="equipment_number"' in form.content.decode()
    assert "更多实物资料（选填）" in form.content.decode()


def test_registered_asset_can_correct_only_equipment_number_with_audit(client, context):
    asset = registered(context, "equipment-number-registered")
    original_identity = asset.current_issued_code_id
    original_code = asset.asset_code
    client.force_login(context["equipment"])
    detail_url = reverse("assets:asset-detail", args=[asset.pk])
    change_url = reverse("assets:asset-equipment-number", args=[asset.pk])
    detail = client.get(detail_url)
    assert "补录或更正设备编号" in detail.content.decode()
    assert client.get(change_url).status_code == 200

    saved = client.post(change_url, {
        "equipment_number": "EQ-PLANT-001",
        "reason": "根据现场设备铭牌补录",
        "asset_code": "FA-FAKE-CODE",
        "original_cost": "0.01",
    })
    assert saved.status_code == 302
    asset.refresh_from_db()
    assert asset.equipment_number == "EQ-PLANT-001"
    assert asset.asset_code == original_code
    assert asset.current_issued_code_id == original_identity
    assert not hasattr(asset, "finance")
    audit = AuditLog.objects.get(
        object_type="Asset", object_id=str(asset.pk),
        action="asset_equipment_number_update",
    )
    assert audit.old_data_json == {"equipment_number": ""}
    assert audit.new_data_json == {
        "equipment_number": "EQ-PLANT-001",
        "reason": "根据现场设备铭牌补录",
    }
    assert "EQ-PLANT-001" in client.get(detail_url).content.decode()

    repeat = client.post(change_url, {
        "equipment_number": "EQ-PLANT-001", "reason": "重复点击保存",
    })
    assert repeat.status_code == 302
    assert AuditLog.objects.filter(action="asset_equipment_number_update").count() == 1


def test_registered_equipment_number_correction_enforces_reason_and_scope(client, context):
    asset = registered(context, "equipment-number-permission")
    change_url = reverse("assets:asset-equipment-number", args=[asset.pk])
    client.force_login(context["equipment"])
    missing_reason = client.post(change_url, {"equipment_number": "EQ-2", "reason": ""})
    assert missing_reason.status_code == 400
    asset.refresh_from_db()
    assert asset.equipment_number == ""
    with pytest.raises(ValidationError):
        update_asset_equipment_number(
            actor=context["equipment"], asset=asset,
            equipment_number="EQ-2", reason="",
        )
    outsider = make_user("equipment-number-outsider", "employee")
    with pytest.raises(PermissionDenied):
        update_asset_equipment_number(
            actor=outsider, asset=asset,
            equipment_number="EQ-2", reason="越权提交",
        )
    client.force_login(outsider)
    assert client.get(change_url).status_code == 404
    assert not AuditLog.objects.filter(action="asset_equipment_number_update").exists()
