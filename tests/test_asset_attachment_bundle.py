import io
from zipfile import ZipFile

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings
from django.urls import reverse

from apps.assets.attachment_bundle import _filename
from apps.assets.models import AttachmentLink
from apps.audit.models import AuditLog
from tests.test_asset_attachment_workspace import attachment_context
from tests.test_sprint3_support import JPEG_BYTES, direct_attachment, direct_draft, make_user

pytestmark = pytest.mark.django_db


def _file(asset, actor, key, *, name="同名.jpg", security="A0", available=True, store=True):
    attachment = direct_attachment(asset.company, actor, key="private/assets/bundle-" + key,
        filename=name, data=JPEG_BYTES, available=available)
    if store:
        default_storage.save(attachment.storage_key, ContentFile(JPEG_BYTES))
    return AttachmentLink.objects.create(company=asset.company, asset=asset, attachment=attachment,
        created_by=actor, role="invoice" if security == "A1" else "photo", security_class=security)


def test_bundle_keeps_selected_bytes_and_duplicate_names_and_audits_once_per_file(client, tmp_path):
    actor, _, asset = attachment_context()
    client.force_login(actor)
    with override_settings(MEDIA_ROOT=tmp_path):
        first = _file(asset, actor, "first")
        second = _file(asset, actor, "second")
        _file(asset, actor, "unselected", name="不应下载.jpg")
        before = list(AttachmentLink.objects.order_by("pk").values())
        response = client.get(reverse("assets:attachment-bundle", args=[asset.pk]),
                              {"attachment": [str(first.pk), str(second.pk)]})
        assert response.status_code == 200 and response["Content-Type"] == "application/zip"
        assert "no-store" in response["Cache-Control"] and response["X-Content-Type-Options"] == "nosniff"
        assert response["Content-Disposition"].startswith("attachment;")
        with ZipFile(io.BytesIO(b"".join(response.streaming_content))) as archive:
            assert archive.namelist() == ["01_同名.jpg", "02_同名.jpg", "附件清单.txt"]
            assert archive.read("01_同名.jpg") == archive.read("02_同名.jpg") == JPEG_BYTES
            manifest = archive.read("附件清单.txt").decode("utf-8-sig")
            assert "文件数：2" in manifest and "不应下载" not in manifest
        assert list(AttachmentLink.objects.order_by("pk").values()) == before
        logs = AuditLog.objects.filter(action="asset_attachment_download")
        assert logs.count() == 2 and {row.object_id for row in logs} == {str(first.pk), str(second.pk)}
    assert _filename(r"C:\unsafe\..\name.jpg") == "name.jpg"
    assert _filename("../../unsafe\nname.jpg") == "unsafe_name.jpg"


def test_bundle_rechecks_every_file_scope_and_financial_permission(client, tmp_path):
    actor, company, asset = attachment_context()
    finance = make_user("bundle-finance", "finance")
    url = reverse("assets:attachment-bundle", args=[asset.pk])
    with override_settings(MEDIA_ROOT=tmp_path):
        visible = _file(asset, actor, "visible")
        sensitive = _file(asset, finance, "secret", security="A1")
        other_asset = direct_draft(company, asset.category, actor=actor)
        other = _file(other_asset, actor, "other")
        client.force_login(actor)
        assert client.get(url, {"attachment": [visible.pk, sensitive.pk]}).status_code == 403
        assert client.get(url, {"attachment": [visible.pk, other.pk]}).status_code == 404
        client.force_login(make_user("bundle-hr", "hr"))
        assert client.get(url, {"attachment": [visible.pk]}).status_code == 403
        assert not AuditLog.objects.filter(action="asset_attachment_download").exists()
        client.force_login(finance)
        response = client.get(url, {"attachment": [sensitive.pk]})
        assert response.status_code == 200
        assert ZipFile(io.BytesIO(b"".join(response.streaming_content))).read("01_同名.jpg") == JPEG_BYTES
        assert AuditLog.objects.filter(action="asset_attachment_download").count() == 1


def test_missing_or_unavailable_selection_never_returns_partial_archive(client, tmp_path):
    actor, _, asset = attachment_context()
    client.force_login(actor)
    url = reverse("assets:attachment-bundle", args=[asset.pk])
    with override_settings(MEDIA_ROOT=tmp_path):
        available = _file(asset, actor, "present")
        missing = _file(asset, actor, "missing", store=False)
        unavailable = _file(asset, actor, "unavailable", available=False)
        assert client.get(url, {"attachment": [available.pk, missing.pk]}).status_code == 404
        assert client.get(url, {"attachment": [available.pk, unavailable.pk]}).status_code == 404
        assert not AuditLog.objects.filter(action="asset_attachment_download").exists()


def test_bundle_enforces_empty_invalid_count_and_actual_size_limits(client, tmp_path, monkeypatch):
    actor, _, asset = attachment_context()
    client.force_login(actor)
    url = reverse("assets:attachment-bundle", args=[asset.pk])
    assert client.get(url).status_code == 400
    assert client.get(url, {"attachment": "invalid"}).status_code == 400
    assert client.get(url, {"attachment": [str(asset.pk)] * 21}).status_code == 400
    assert client.post(url).status_code == 405
    with override_settings(MEDIA_ROOT=tmp_path):
        link = _file(asset, actor, "limit")
        monkeypatch.setattr("apps.assets.attachment_bundle.BUNDLE_MAX_TOTAL_BYTES", len(JPEG_BYTES) - 1)
        assert client.get(url, {"attachment": [link.pk]}).status_code == 400
        monkeypatch.setattr("apps.assets.attachment_bundle.BUNDLE_MAX_TOTAL_BYTES", len(JPEG_BYTES) * 2)
        with default_storage.open(link.attachment.storage_key, "wb") as stored:
            stored.write(JPEG_BYTES * 3)
        response = client.get(url, {"attachment": [link.pk]})
        assert response.status_code == 400 and "实际总大小" in response.content.decode()
        assert not AuditLog.objects.filter(action="asset_attachment_download").exists()
