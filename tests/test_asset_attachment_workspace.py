import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.assets.models import AttachmentLink
from apps.core.multi_upload import MAX_UPLOAD_FILES, MAX_UPLOAD_TOTAL_BYTES
from apps.masterdata.services import set_system_setting
from tests.test_sprint3_http import make_context
from tests.test_sprint3_support import JPEG_BYTES, direct_attachment, make_asset, make_user


pytestmark = pytest.mark.django_db


def attachment_context():
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(actor=actor, company=company, category=category,
                       department=department, employee=employee, location=location)
    return actor, company, asset


def link_file(asset, actor, filename, *, role="photo", security="A0", mime="image/jpeg", available=True):
    attachment = direct_attachment(asset.company, actor, filename=filename,
        key="private/assets/" + filename, mime=mime, available=available)
    return AttachmentLink.objects.create(company=asset.company, attachment=attachment, asset=asset,
        role=role, security_class=security, created_by=actor)


def test_attachment_facets_sizes_and_preview_flags_use_only_authorized_available_files(client):
    actor, company, asset = attachment_context()
    finance = make_user("attachment-workspace-finance", "finance")
    image = link_file(asset, actor, '现场 & "检查".jpg')
    manual = link_file(asset, actor, "使用说明.pdf", role="manual", mime="application/pdf")
    invoice = link_file(asset, finance, "hidden-invoice.pdf", role="invoice", security="A1", mime="application/pdf")
    link_file(asset, actor, "unavailable-image.jpg", available=False)
    client.force_login(actor)
    response = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    workspace = response.context["attachment_workspace"]
    assert workspace["count"] == 2
    assert workspace["total_bytes"] == image.attachment.file_size + manual.attachment.file_size
    assert {role["value"] for role in workspace["roles"]} == {"photo", "manual"}
    assert {row["link"].pk: row["can_preview"] for row in response.context["attachment_rows"]} == {image.pk: True, manual.pk: False}
    html = response.content.decode()
    assert "hidden-invoice" not in html and "unavailable-image" not in html
    assert 'data-filename="现场 &amp; &quot;检查&quot;.jpg"' in html
    assert reverse("assets:attachment-download", args=[asset.pk, image.pk]) in html
    client.force_login(finance)
    own = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert own.context["attachment_workspace"]["count"] == 3
    assert {role["value"] for role in own.context["attachment_workspace"]["roles"]} == {"photo", "manual", "invoice"}
    assert invoice.pk in {row["link"].pk for row in own.context["attachment_rows"]}


def test_summary_viewer_receives_no_attachment_metadata_or_finder(client):
    actor, _company, asset = attachment_context()
    link_file(asset, actor, "private-workspace-name.jpg")
    client.force_login(make_user("attachment-workspace-hr", "hr"))
    response = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert response.status_code == 200
    assert response.context["attachment_workspace"] == {"count": 0, "total_bytes": 0, "roles": []}
    assert "private-workspace-name" not in response.content.decode()
    assert "data-attachment-search" not in response.content.decode()


def test_upload_review_shows_actual_company_limits_and_shared_batch_limits(client):
    actor, company, asset = attachment_context()
    admin = make_user("attachment-workspace-admin", "system_admin")
    set_system_setting(actor=admin, company=company, key="attachment_allowed_extensions", value=["png", "pdf"])
    set_system_setting(actor=admin, company=company, key="attachment_max_size_bytes", value=1024 * 1024)
    client.force_login(actor)
    response = client.get(reverse("assets:attachment-upload", args=[asset.pk]))
    widget = response.context["form"].fields["file"].widget.attrs
    assert widget["accept"] == ".png,.pdf"
    assert widget["data-max-file-bytes"] == 1024 * 1024
    assert widget["data-max-files"] == MAX_UPLOAD_FILES
    assert widget["data-max-total-bytes"] == MAX_UPLOAD_TOTAL_BYTES
    assert response.context["upload_extensions"] == ["png", "pdf"]
    assert "单个文件最多 1.0 MB" in " ".join(response.content.decode().split())
    capture = client.get(reverse("assets:attachment-upload", args=[asset.pk]), {"role": "photo"})
    assert capture.context["form"].fields["file"].widget.attrs["accept"] == "image/png"
    assert capture.context["form"].fields["file"].widget.attrs["capture"] == "environment"


def test_upload_error_keeps_purpose_and_explains_file_reselection(client, tmp_path):
    actor, _company, asset = attachment_context()
    client.force_login(actor)
    with override_settings(MEDIA_ROOT=tmp_path):
        response = client.post(reverse("assets:attachment-upload", args=[asset.pk]), {
            "role": "cover", "security_class": "A0",
            "file": [SimpleUploadedFile(name, JPEG_BYTES, content_type="image/jpeg") for name in ("first.jpg", "second.jpg")],
        })
    assert response.status_code == 200
    assert response.context["form"]["role"].value() == "cover"
    assert response.context["form"].errors["file"]
    assert "请重新选择文件后再提交" in response.content.decode()
    assert not asset.attachment_links.exists()
