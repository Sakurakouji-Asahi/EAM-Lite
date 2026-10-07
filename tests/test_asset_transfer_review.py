"""Transfer review is read-only and preserves the controlled native save."""
import pytest
from django.urls import reverse
from apps.assets.models import Asset, AssetMovement
from tests.test_sprint3_support import make_company, make_department, make_employee, make_user
from tests.test_sprint7_support import active_asset_context, add_target_assignment

pytestmark = pytest.mark.django_db


def payload(page):
    return {field.name: field.value() or "" for field in page.context["form"]}


def test_transfer_review_get_and_other_lifecycle_forms_do_not_mutate(client):
    context, asset, qr = active_asset_context("TRANSFER-REVIEW-GET")
    client.force_login(context["equipment"])
    original = Asset._base_manager.filter(pk=asset.pk).values().get()
    movement_count = asset.movements.count()
    page = client.get(reverse("assets:lifecycle-transfer", args=[asset.pk]))
    assert page.status_code == 200
    review = page.context["transfer_review"]
    assert review["changed_count"] == review["missing_count"] == 0
    assert all(row["state"] == "unchanged" for row in review["rows"])
    assert context["employee"].employee_no in review["rows"][1]["original_label"]
    assert context["location"].parent.name in review["rows"][2]["original_label"]
    assert all(str(field.value()) for field in page.context["form"].hidden_fields())
    assert "data-transfer-review" in page.content.decode()
    idle = client.get(reverse("assets:lifecycle-idle", args=[asset.pk]))
    assert idle.status_code == 200 and idle.context["transfer_review"] is None
    assert "asset-transfer-review.js" not in idle.content.decode()
    assert Asset._base_manager.filter(pk=asset.pk).values().get() == original
    assert asset.movements.count() == movement_count


def test_bound_review_keeps_targets_and_native_save_preserves_identity_history_and_replay(client):
    context, asset, qr = active_asset_context("TRANSFER-REVIEW-SAVE")
    department, employee, location = add_target_assignment(context, "TRANSFER-REVIEW-SAVE")
    client.force_login(context["equipment"])
    url = reverse("assets:lifecycle-transfer", args=[asset.pk])
    page = client.get(url)
    data = payload(page)
    data.update(to_department=department.pk, to_responsible_employee=employee.pk,
                to_location=location.pk, reason="", remark="核对后办理")
    before = (asset.department_id, asset.responsible_employee_id, asset.location_id)
    identity = (asset.asset_code, asset.current_issued_code_id, qr.public_token)
    rejected = client.post(url, data)
    assert rejected.status_code == 200 and "reason" in rejected.context["form"].errors
    assert rejected.context["transfer_review"]["changed_count"] == 3
    assert rejected.context["transfer_review"]["missing_count"] == 0
    assert employee.employee_no in rejected.context["transfer_review"]["rows"][1]["target_label"]
    asset.refresh_from_db()
    assert (asset.department_id, asset.responsible_employee_id, asset.location_id) == before
    assert not asset.movements.filter(movement_type="transfer").exists()
    data["reason"] = "调拨核对办理"
    saved = client.post(url, data)
    assert saved.status_code == 302 and saved.url == reverse("assets:asset-detail", args=[asset.pk])
    repeated = client.post(url, data)
    assert repeated.status_code == 302 and repeated.url == saved.url
    asset.refresh_from_db()
    qr.refresh_from_db()
    movement = asset.movements.get(movement_type="transfer")
    assert (asset.department_id, asset.responsible_employee_id, asset.location_id) == (department.pk, employee.pk, location.pk)
    assert (movement.from_department_id, movement.from_employee_id, movement.from_location_id) == before
    assert (movement.to_department_id, movement.to_employee_id, movement.to_location_id) == (department.pk, employee.pk, location.pk)
    assert movement.reason == data["reason"] and movement.remark == data["remark"]
    assert movement.from_status == movement.to_status == asset.asset_status == "in_use"
    assert (asset.asset_code, asset.current_issued_code_id, qr.public_token) == identity
    assert AssetMovement.objects.filter(asset=asset, movement_type="transfer").count() == 1


def test_invalid_choice_never_appears_in_review_and_action_permission_stays_enforced(client):
    context, asset, qr = active_asset_context("TRANSFER-REVIEW-SCOPE")
    other = make_company("TRANSFER-OTHER", active=False)
    department = make_department(other, "TRANSFER-OTHER-D")
    outsider = make_employee(other, department, "TRANSFER-OTHER-E")
    client.force_login(context["equipment"])
    url = reverse("assets:lifecycle-transfer", args=[asset.pk])
    data = payload(client.get(url))
    data.update(to_responsible_employee=outsider.pk, reason="非法选择校验")
    page = client.post(url, data)
    assert page.status_code == 200 and "to_responsible_employee" in page.context["form"].errors
    assert page.context["transfer_review"]["missing_count"] == 1
    assert page.context["transfer_review"]["rows"][1]["target_label"] == "请选择有效目标"
    assert outsider.employee_no not in page.content.decode()
    assert not asset.movements.filter(movement_type="transfer").exists()
    client.force_login(make_user("transfer-review-reader", "hr"))
    assert client.get(url).status_code in (403, 404)
