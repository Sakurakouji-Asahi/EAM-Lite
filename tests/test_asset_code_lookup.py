import io
from urllib.parse import urlencode

import pytest
from django.core.files.storage import default_storage
from django.urls import reverse
from openpyxl import load_workbook

from apps.assets.models import Asset, AssetMovement
from apps.audit.models import AuditLog
from apps.reports.models import ExportLog
from tests.test_sprint3_support import make_department, make_employee, make_user, grant_scope
from tests.test_unified_asset_identity import context, registered


pytestmark = pytest.mark.django_db


def test_bulk_codes_exact_deduplicated_lookup_summary_and_export_match(context, client):
    first = registered(context, key="lookup-first", asset_name="查找甲", equipment_number="Lookup-A")
    second = registered(context, key="lookup-second", asset_name="查找乙", equipment_number="SHARED-CODE")
    third = registered(context, key="lookup-third", asset_name="查找丙", equipment_number="SHARED-CODE")
    client.force_login(context["finance"])
    source = f"{first.asset_code.lower()}；Lookup-A, shared-code\nMISSING\tSHARED-CODE"
    response = client.get(reverse("assets:asset-list"), {"codes": source, "page_size": 25})
    assert response.status_code == 200
    assert {asset.pk for asset in response.context["page"]} == {first.pk, second.pk, third.pk}
    assert response.context["code_lookup"] == {"total": 4, "matched": 3, "missing": ["MISSING"], "multiple": [{"code": "shared-code", "count": 2}]}
    assert response.context["filters"]["codes"].splitlines() == [first.asset_code.lower(), "Lookup-A", "shared-code", "MISSING"]
    limited = client.get(reverse("assets:asset-list"), {"codes": source, "q": "查找甲"})
    assert [asset.pk for asset in limited.context["page"]] == [first.pk]
    assert limited.context["code_lookup"]["missing"] == ["shared-code", "MISSING"]
    partial = client.get(reverse("assets:asset-list"), {"codes": "Lookup"})
    assert partial.context["page"].paginator.count == 0
    preview = client.get(reverse("assets:asset-list-export"), {"codes": source})
    assert preview.status_code == 200
    assert preview.context["dataset"].row_count == 3
    payload = {**preview.context["filters"], "idempotency_key": preview.context["idempotency_key"]}
    saved = client.post(reverse("assets:asset-list-export"), payload)
    assert saved.status_code == 302
    export = ExportLog.objects.get()
    assert export.row_count == 3
    with default_storage.open(export.output_attachment.storage_key, "rb") as stream:
        workbook = load_workbook(io.BytesIO(stream.read()), data_only=True)
    values = [value for sheet in workbook for row in sheet.values for value in row]
    assert all(asset.asset_name in values and asset.asset_code in values for asset in (first, second, third))
    assert export.filters_json["asset_list_filters"]["codes"] == response.context["filters"]["codes"]
    long_codes = "\n".join([first.asset_code, *(f"UNMATCHED-{index}-" + "X" * 75 for index in range(49))])
    return_url = reverse("assets:asset-list") + "?" + urlencode({"codes": long_codes})
    assert len(return_url) > 3000
    detail = client.get(reverse("assets:asset-detail", args=[first.pk]), {"return_to": return_url})
    assert detail.context["back_url"] == return_url


def test_codes_keep_department_scope_and_summary_only_equipment_visibility(context, client):
    own = registered(context, key="lookup-own", equipment_number="PRIVATE-EQUIPMENT")
    other_department = make_department(context["company"], "LOOKUP-OTHER")
    other_employee = make_employee(context["company"], other_department, "LOOKUP-EMPLOYEE")
    outside = registered(context, key="lookup-outside", asset_name="不应显示的外部门资产",
        department=other_department, responsible_employee=other_employee, equipment_number="OUTSIDE-EQUIPMENT")
    manager = make_user("lookup-manager", "department_manager")
    grant_scope(manager, context["company"], context["department"], descendants=False, assigned_by=context["admin"])
    client.force_login(manager)
    response = client.get(reverse("assets:asset-list"), {"codes": f"{own.asset_code}\n{outside.asset_code}"})
    assert [asset.pk for asset in response.context["page"]] == [own.pk]
    assert response.context["code_lookup"]["missing"] == [outside.asset_code]
    assert outside.asset_name not in response.content.decode()
    client.force_login(make_user("lookup-hr", "hr"))
    response = client.get(reverse("assets:asset-list"), {"codes": f"{own.asset_code}\nPRIVATE-EQUIPMENT"})
    assert not response.context["list_has_p1"]
    assert [asset.pk for asset in response.context["page"]] == [own.pk]
    assert response.context["code_lookup"]["missing"] == ["PRIVATE-EQUIPMENT"]
    assert "或设备编号；" not in response.content.decode()


def test_lookup_limits_preserve_input_and_never_write_asset_state(context, client):
    registered(context, key="lookup-validation")
    client.force_login(context["finance"])
    models = (Asset, AssetMovement, AuditLog)
    before = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
    for source in ("\n".join(f"CODE-{index}" for index in range(51)), "X" * 5001, "A" * 201):
        response = client.get(reverse("assets:asset-list"), {"codes": source})
        assert response.context["filter_errors"]
        assert response.context["page"].paginator.count == 0
        assert response.context["filters"]["codes"] == source
        assert client.get(reverse("assets:asset-list-export"), {"codes": source}).status_code == 400
    assert {model.__name__: list(model.objects.order_by("pk").values()) for model in models} == before
