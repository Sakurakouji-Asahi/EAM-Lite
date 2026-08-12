from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone

from apps.assets.models import AttachmentLink
from apps.assets.services import upload_asset_attachment, void_asset_attachment
from apps.audit.models import AuditLog
from apps.imports.cleanup import (
    cleanup_orphan_attachments,
    cleanup_unreferenced_private_files,
)
from apps.masterdata.models import Attachment, SystemSetting
from tests.test_sprint3_support import (
    JPEG_BYTES,
    add_photo,
    complete_initialization,
    direct_attachment,
    jpeg_upload,
    make_asset,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
    pdf_upload,
)


pytestmark = pytest.mark.django_db


class RawNamedUpload:
    """Upload stub that deliberately does not sanitize a supplied filename."""

    def __init__(self, name, data=JPEG_BYTES, content_type="image/jpeg"):
        self.name = name
        self._data = data
        self.size = len(data)
        self.content_type = content_type

    def chunks(self):
        yield self._data


def make_context():
    actor = make_user("equipment", "equipment")
    company = make_company()
    complete_initialization(company, actor)
    department = make_department(company)
    employee = make_employee(company, department)
    category = make_category(company)
    _site, _area, location = make_location_tree(company)
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    return actor, company, asset


def test_upload_uses_private_random_storage_key_and_publishes_after_link_commit(tmp_path):
    actor, company, asset = make_context()

    with override_settings(MEDIA_ROOT=tmp_path):
        link = add_photo(actor, asset)
        attachment = link.attachment
        stored_path = Path(default_storage.path(attachment.storage_key))

        assert link.status == "active"
        assert attachment.is_available
        assert attachment.malware_scan_status == "policy_limited"
        assert attachment.original_filename == "asset.jpg"
        assert attachment.storage_key.startswith(
            f"private/assets/{company.pk}/"
        )
        assert "asset.jpg" not in attachment.storage_key
        assert stored_path.exists()
        assert stored_path.read_bytes() == JPEG_BYTES
        with pytest.raises(ValueError, match="没有公开 URL"):
            default_storage.url(attachment.storage_key)


def test_upload_database_failure_rolls_back_rows_and_removes_saved_file(tmp_path):
    actor, _company, asset = make_context()

    with override_settings(MEDIA_ROOT=tmp_path), patch(
        "apps.assets.services._audit", side_effect=RuntimeError("forced rollback")
    ), pytest.raises(RuntimeError, match="forced rollback"):
        upload_asset_attachment(
            actor=actor,
            asset=asset,
            uploaded_file=jpeg_upload(),
            role=AttachmentLink.Role.PHOTO,
            security_class=AttachmentLink.SecurityClass.A0,
        )

    assert Attachment.objects.count() == 0
    assert AttachmentLink.objects.count() == 0
    assert not list(tmp_path.rglob("*.*"))


@pytest.mark.parametrize(
    "upload",
    (
        lambda: jpeg_upload("asset.jpg", content_type="image/png"),
        lambda: pdf_upload("photo.jpg", content_type="application/pdf"),
        lambda: jpeg_upload("asset.exe"),
        lambda: jpeg_upload("asset.photo.jpg"),
        lambda: jpeg_upload("asset..jpg"),
        lambda: jpeg_upload("asset.svg", content_type="image/svg+xml"),
        # SimpleUploadedFile strips directory components in its own name
        # setter.  Use a raw stub so this is a real traversal-name assertion.
        lambda: RawNamedUpload("../asset.jpg"),
        lambda: RawNamedUpload(r"folder\asset.jpg"),
        lambda: RawNamedUpload("forged.jpg", b"\xff\xd8\xffgarbage"),
        lambda: RawNamedUpload("truncated.jpg", JPEG_BYTES[:-32]),
    ),
)
def test_upload_rejects_mime_signature_extension_and_dangerous_filename(upload, tmp_path):
    actor, _company, asset = make_context()

    with override_settings(MEDIA_ROOT=tmp_path), pytest.raises(ValidationError):
        upload_asset_attachment(
            actor=actor,
            asset=asset,
            uploaded_file=upload(),
            role="photo",
            security_class="A0",
        )

    assert Attachment.objects.count() == 0
    assert AttachmentLink.objects.count() == 0
    assert not list(tmp_path.rglob("*.*"))


def test_empty_file_and_configured_size_limit_are_enforced(tmp_path):
    actor, company, asset = make_context()
    SystemSetting.objects.create(
        company=company,
        key="attachment_max_size_bytes",
        value="4",
        value_type="integer",
        description="单个附件最大字节数",
        updated_by=actor,
    )

    with override_settings(MEDIA_ROOT=tmp_path):
        with pytest.raises(ValidationError, match="超过当前上限"):
            add_photo(actor, asset)

    SystemSetting.objects.filter(
        company=company, key="attachment_max_size_bytes"
    ).update(value="20971520")
    empty = jpeg_upload()
    empty.file = type(empty.file)(b"")
    empty.size = 0
    with override_settings(MEDIA_ROOT=tmp_path), pytest.raises(
        ValidationError, match="不能为空"
    ):
        upload_asset_attachment(
            actor=actor,
            asset=asset,
            uploaded_file=empty,
            role="photo",
            security_class="A0",
        )


def test_nonfinance_cannot_upload_a1_and_finance_can(tmp_path):
    equipment, _company, asset = make_context()
    finance = make_user("finance", "finance")

    with override_settings(MEDIA_ROOT=tmp_path):
        with pytest.raises(PermissionDenied):
            upload_asset_attachment(
                actor=equipment,
                asset=asset,
                uploaded_file=pdf_upload(),
                role="invoice",
                security_class="A1",
            )
        link = upload_asset_attachment(
            actor=finance,
            asset=asset,
            uploaded_file=pdf_upload(),
            role="invoice",
            security_class="A1",
        )

    assert link.security_class == "A1"
    assert link.role == "invoice"
    log = AuditLog.objects.get(action="asset_attachment_create")
    assert log.new_data_json["security_class"] == "A1"
    assert "invoice.pdf" not in str(log.new_data_json)


def test_cover_and_photo_require_real_image_and_a0_at_model_and_database_level():
    actor, company, asset = make_context()
    pdf = direct_attachment(
        company,
        actor,
        key="private/assets/not-image.pdf",
        filename="not-image.pdf",
        mime="application/pdf",
        data=b"%PDF-1.7\n",
    )
    link = AttachmentLink(
        company=company,
        attachment=pdf,
        asset=asset,
        role="photo",
        security_class="A1",
        created_by=actor,
    )

    with pytest.raises(ValidationError):
        link.full_clean()


def test_only_one_active_cover_and_void_preserves_file_metadata_and_is_idempotent(
    tmp_path,
):
    actor, company, asset = make_context()

    with override_settings(MEDIA_ROOT=tmp_path):
        first = add_photo(actor, asset, role=AttachmentLink.Role.COVER)
        key = first.attachment.storage_key
        assert default_storage.exists(key)
        with pytest.raises(ValidationError):
            add_photo(actor, asset, role=AttachmentLink.Role.COVER)

        with pytest.raises(ValidationError):
            void_asset_attachment(actor=actor, link=first, reason="")
        voided = void_asset_attachment(
            actor=actor, link=first, reason="封面拍摄错误"
        )
        repeated = void_asset_attachment(
            actor=actor, link=voided, reason="重复请求"
        )

        assert repeated.pk == voided.pk
        assert repeated.status == "voided"
        assert repeated.void_reason == "封面拍摄错误"
        assert Attachment.objects.filter(pk=first.attachment_id).exists()
        assert default_storage.exists(key)
        assert AuditLog.objects.filter(action="asset_attachment_void").count() == 1


def test_attachment_link_cross_company_scope_is_rejected_by_model_validation():
    actor, company, asset = make_context()
    other = make_company("C2", active=False)
    foreign = direct_attachment(
        other,
        actor,
        key="private/assets/foreign.jpg",
        filename="foreign.jpg",
    )
    link = AttachmentLink(
        company=company,
        attachment=foreign,
        asset=asset,
        role="photo",
        security_class="A0",
        created_by=actor,
    )

    with pytest.raises(ValidationError):
        link.full_clean()


def test_asset_private_orphan_cleanup_is_bounded_audited_and_repeatable(tmp_path):
    actor, company, asset = make_context()
    admin = make_user("cleanup-admin", "system_admin")
    with override_settings(MEDIA_ROOT=tmp_path):
        orphan_key = default_storage.save(
            f"private/assets/{company.pk}/orphan.jpg", ContentFile(JPEG_BYTES)
        )
        referenced = add_photo(actor, asset)
        old_time = (timezone.now() - timedelta(days=8)).timestamp()
        os.utime(default_storage.path(orphan_key), (old_time, old_time))
        os.utime(
            default_storage.path(referenced.attachment.storage_key),
            (old_time, old_time),
        )

        first = cleanup_unreferenced_private_files(
            actor=admin,
            older_than_days=7,
            dry_run=False,
            private_prefixes=("private/assets",),
        )
        repeated = cleanup_unreferenced_private_files(
            actor=admin,
            older_than_days=7,
            dry_run=False,
            private_prefixes=("private/assets",),
        )

        assert orphan_key in first.legacy_files_deleted
        assert not default_storage.exists(orphan_key)
        assert repeated.legacy_files_deleted == []
        assert default_storage.exists(referenced.attachment.storage_key)
        assert AuditLog.objects.filter(
            action="asset_private_file_cleanup", company=company
        ).exists()


def test_orphan_attachment_cleanup_skips_asset_business_reference(tmp_path):
    actor, _company, asset = make_context()
    admin = make_user("cleanup-link-admin", "system_admin")
    with override_settings(MEDIA_ROOT=tmp_path):
        link = add_photo(actor, asset)
        attachment = link.attachment
        Attachment.objects.filter(pk=attachment.pk).update(
            is_available=False,
            orphaned_at=timezone.now() - timedelta(days=8),
        )

        report = cleanup_orphan_attachments(
            actor=admin,
            orphan_retention_days=7,
            dry_run=False,
        )

        assert Attachment.objects.filter(pk=attachment.pk).exists()
        assert report.attachments_skipped[attachment.pk] == "仍被资产业务记录引用"
