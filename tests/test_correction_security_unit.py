from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import PurePosixPath
from types import MappingProxyType, SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import load_workbook

from apps.assets import lifecycle_services as lifecycle_attachment_services
from apps.assets import services as asset_attachment_services
from apps.assets.models import AssetDisposal
from apps.audit.models import AuditLog
from apps.audit.query import project_audit_log
from apps.audit.services import REDACTED, write_audit_log
from apps.inventory import services as inventory_attachment_services
from apps.imports import services as import_services
from apps.maintenance import services as maintenance_attachment_services
from apps.offboarding import services as clearance_attachment_services
from apps.reports.excel import write_tplus_workbook
from tests.test_sprint11_schemas_excel import _tplus_dataset
from tests.test_sprint3_support import make_company, make_user


class RawNamedUpload:
    def __init__(self, name):
        self.name = name


@pytest.mark.django_db
def test_five_evidence_uploads_reject_windows_separator_under_posix_semantics(
    monkeypatch,
):
    dangerous_name = r"folder\asset.jpg"
    monkeypatch.setattr(asset_attachment_services, "Path", PurePosixPath)
    original_validator = asset_attachment_services._validate_filename
    calls = []

    def validating(name):
        calls.append(name)
        return original_validator(name)

    monkeypatch.setattr(asset_attachment_services, "_validate_filename", validating)
    company = SimpleNamespace(pk=1)
    asset = SimpleNamespace(company=company)
    monkeypatch.setattr(asset_attachment_services, "_lock_current_asset", lambda value: value)
    monkeypatch.setattr(
        asset_attachment_services, "_require_initialization_completed", lambda _company: None
    )
    monkeypatch.setattr(
        asset_attachment_services, "can_create_attachment_link", lambda *_args: True
    )

    disposal = SimpleNamespace(pk=1, asset_id=1)
    disposal_asset = SimpleNamespace(company=company)
    disposal_queryset = SimpleNamespace(get=lambda **_kwargs: disposal)
    monkeypatch.setattr(
        lifecycle_attachment_services, "_lock_asset", lambda _asset_id: disposal_asset
    )
    monkeypatch.setattr(
        AssetDisposal.objects, "select_for_update", lambda: disposal_queryset
    )
    monkeypatch.setattr(
        lifecycle_attachment_services,
        "can_manage_disposal_attachment",
        lambda *_args, **_kwargs: True,
    )

    inventory_target = object()
    inventory_task = SimpleNamespace(company=company)
    monkeypatch.setattr(
        inventory_attachment_services,
        "_lock_inventory_attachment_target",
        lambda target: (target, inventory_task),
    )
    monkeypatch.setattr(
        inventory_attachment_services,
        "can_manage_inventory_attachment",
        lambda *_args: True,
    )

    maintenance_target = object()
    maintenance_plan = SimpleNamespace(company=company)
    monkeypatch.setattr(
        maintenance_attachment_services,
        "_lock_attachment_target",
        lambda target: (target, maintenance_plan),
    )
    monkeypatch.setattr(
        maintenance_attachment_services,
        "can_manage_maintenance_attachment",
        lambda *_args, **_kwargs: True,
    )

    clearance = SimpleNamespace(
        _meta=SimpleNamespace(model_name="employeeassetclearance"), company=company
    )
    monkeypatch.setattr(
        clearance_attachment_services, "_lock_clearance", lambda _target: clearance
    )
    monkeypatch.setattr(
        clearance_attachment_services,
        "can_manage_clearance_attachment",
        lambda *_args, **_kwargs: True,
    )

    upload = RawNamedUpload(dangerous_name)
    invocations = (
        lambda: asset_attachment_services.upload_asset_attachment(
            actor=object(), asset=asset, uploaded_file=upload,
            role="invoice", security_class="A0",
        ),
        lambda: lifecycle_attachment_services.upload_disposal_attachment(
            actor=object(), disposal=disposal, uploaded_file=upload,
        ),
        lambda: inventory_attachment_services.upload_inventory_attachment(
            actor=object(), target=inventory_target, uploaded_file=upload,
        ),
        lambda: maintenance_attachment_services.upload_maintenance_attachment(
            actor=object(), target=maintenance_target, uploaded_file=upload,
        ),
        lambda: clearance_attachment_services.upload_clearance_attachment(
            actor=object(), target=clearance, uploaded_file=upload,
        ),
    )
    for invoke in invocations:
        with pytest.raises(ValidationError, match="危险路径"):
            invoke()
    assert calls == [dangerous_name] * 5


def test_tplus_difference_escapes_user_text_without_changing_formula_or_money_types():
    dataset = _tplus_dataset()
    asset_row = dict(dataset.asset_rows[0])
    asset_row.update(
        asset_code="=EAM-CODE",
        tplus_card_code="+TPLUS-CODE",
        asset_name="=DDE|danger",
    )
    dataset = replace(dataset, asset_rows=(MappingProxyType(asset_row),))
    output = io.BytesIO()
    write_tplus_workbook(
        dataset,
        output,
        export_id="00000000-0000-0000-0000-000000000011",
        company_name="测试公司",
        requested_by="finance",
        generated_at=timezone.now(),
    )
    output.seek(0)
    difference = load_workbook(output, data_only=False)["对账差异"]

    for coordinate in ("B2", "C2", "D2"):
        assert difference[coordinate].data_type == "s"
        assert difference[coordinate].value.startswith("'")
    for coordinate in ("A2", "F2", "G2", "I2", "J2", "L2", "M2", "O2", "P2", "R2", "S2"):
        assert difference[coordinate].data_type == "f"
    for coordinate in ("E2", "H2", "K2", "N2", "Q2"):
        assert difference[coordinate].data_type == "n"


@pytest.mark.django_db
def test_audit_file_payload_fields_are_redacted_on_write_and_again_on_read():
    company = make_company("CORRAUDIT")
    actor = make_user("correction-audit-admin", "system_admin")
    written = write_audit_log(
        company=company,
        user=actor,
        action="correction.audit_write",
        object_type="Asset",
        old_data={
            "file_content": "WRITE-FILE-CONTENT",
            "file_contents": "WRITE-FILE-CONTENTS",
            "binary_content": "WRITE-BINARY-CONTENT",
            "file_blob": "WRITE-FILE-BLOB",
        },
        new_data={
            "nested": [
                {"content_bytes": "WRITE-CONTENT-BYTES"},
                {"file_body": "WRITE-FILE-BODY"},
            ]
        },
    )
    assert written.old_data_json["file_content"] == REDACTED
    assert written.old_data_json["file_contents"] == REDACTED
    assert written.old_data_json["binary_content"] == REDACTED
    assert written.old_data_json["file_blob"] == REDACTED
    assert written.new_data_json["nested"][0]["content_bytes"] == REDACTED
    assert written.new_data_json["nested"][1]["file_body"] == REDACTED

    legacy = AuditLog.objects.create(
        company=company,
        user=actor,
        action="correction.audit_legacy",
        object_type="Asset",
        old_data_json={},
        new_data_json={
            "file_content": "READ-FILE-CONTENT",
            "file_contents": "READ-FILE-CONTENTS",
            "content_bytes": "READ-CONTENT-BYTES",
            "nested": {
                "binary_content": "READ-BINARY-CONTENT",
                "file_blob": "READ-FILE-BLOB",
                "file_body": "READ-FILE-BODY",
                "safe": "visible",
            },
        },
    )
    projected = json.loads(project_audit_log(legacy, user=actor)["new_data"])
    assert projected["file_content"] == REDACTED
    assert projected["file_contents"] == REDACTED
    assert projected["content_bytes"] == REDACTED
    assert projected["nested"]["binary_content"] == REDACTED
    assert projected["nested"]["file_blob"] == REDACTED
    assert projected["nested"]["file_body"] == REDACTED
    assert projected["nested"]["safe"] == "visible"


@pytest.mark.parametrize(
    "filename",
    (
        "folder/import.xlsx",
        r"folder\import.xlsx",
        r"C:\import.xlsx",
        "import\x00.xlsx",
        "import.xlsx.exe",
        "import.xlsm.xlsx",
        "import.xlsm",
    ),
)
def test_import_upload_rejects_cross_platform_unsafe_filename(monkeypatch, filename):
    monkeypatch.setattr(
        import_services,
        "require_import_permission",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        import_services,
        "_require_current_import_company",
        lambda _company: None,
    )
    monkeypatch.setattr(
        import_services,
        "get_template_definition",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        import_services,
        "get_system_setting",
        lambda **_kwargs: ["xlsx"],
    )

    upload = RawNamedUpload(filename)
    with pytest.raises(ValidationError):
        import_services.upload_and_validate_import(
            actor=object(),
            company=SimpleNamespace(pk=1),
            import_type="asset_initialization",
            uploaded_file=upload,
            idempotency_key="dangerous-import-name",
        )
