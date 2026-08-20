from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_permissions import (
    can_view_disposal,
    can_view_disposal_attachment,
    scoped_disposals,
)
from apps.assets.lifecycle_services import initiate_disposal
from apps.assets.models import AttachmentLink
from apps.assets.permissions import (
    can_view_asset,
    can_view_asset_p1,
    can_view_attachment,
)
from tests.test_sprint3_support import JPEG_BYTES, direct_attachment, make_user
from tests.test_sprint10_support import formal_asset, offboarding_context
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def test_unbound_hr_employee_cannot_upgrade_global_p0_scope_to_p1(client):
    context = offboarding_context("CORRP1")
    asset, qr = formal_asset(context, "CORRP1-ASSET")
    viewer = make_user("corrp1-hr-employee", "hr", "employee")
    secret = asset.serial_number

    assert can_view_asset(viewer, asset)
    assert not can_view_asset_p1(viewer, asset)

    client.force_login(viewer)
    listing = client.get(reverse("assets:asset-list"))
    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    scan = client.get(reverse("assets:qr-scan", args=[qr.public_token]))
    search = client.get(reverse("assets:asset-list"), {"q": secret})

    assert listing.status_code == detail.status_code == scan.status_code == 200
    assert asset.asset_name in listing.content.decode()
    assert secret not in listing.content.decode()
    assert secret not in detail.content.decode()
    assert detail.context["can_p1"] is False
    assert scan.context["can_p1"] is False
    assert not list(search.context["page"])

    disposal = initiate_disposal(
        actor=context["equipment"],
        asset=asset,
        disposal_type="scrap",
        application_date=timezone.localdate(),
        planned_disposal_date=timezone.localdate() + timedelta(days=1),
        reason="纠正测试处置",
        idempotency_key="CORRP1-disposal",
        expected_status=asset.asset_status,
    )
    attachment = direct_attachment(
        context["company"],
        context["equipment"],
        key="private/disposals/corrp1.jpg",
        filename="CORRP1-DISPOSAL-SECRET.jpg",
    )
    link = AttachmentLink.objects.create(
        company=context["company"],
        attachment=attachment,
        asset_disposal=disposal,
        role=AttachmentLink.Role.DISPOSAL,
        security_class=AttachmentLink.SecurityClass.A0,
        created_by=context["equipment"],
    )

    assert not can_view_disposal(viewer, disposal)
    assert not scoped_disposals(viewer, context["company"]).filter(
        pk=disposal.pk
    ).exists()
    assert not can_view_disposal_attachment(viewer, link)
    assert client.get(
        reverse("assets:disposal-detail", args=[disposal.pk])
    ).status_code == 404
    assert client.get(
        reverse(
            "assets:disposal-attachment-download",
            args=[disposal.pk, link.pk],
        )
    ).status_code == 404


def test_hr_combined_roles_union_a0_and_maintenance_permissions(
    client, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    context = maintenance_context("CORRROLEUNION")
    asset = context["asset"]
    plan = context["plan"]
    link = asset.attachment_links.get(
        security_class=AttachmentLink.SecurityClass.A0
    )
    detail_url = reverse("assets:asset-detail", args=[asset.pk])
    download_url = reverse(
        "assets:attachment-download", args=[asset.pk, link.pk]
    )
    viewers = (
        (make_user("corrrole-hr-finance", "hr", "finance"), True),
        (make_user("corrrole-hr-equipment", "hr", "equipment"), True),
        (make_user("corrrole-hr-management", "hr", "management"), True),
        (make_user("corrrole-hr-only", "hr"), False),
        (make_user("corrrole-hr-employee", "hr", "employee"), False),
    )

    for viewer, granted in viewers:
        assert can_view_asset_p1(viewer, asset) is granted
        assert can_view_attachment(viewer, link) is granted

        client.force_login(viewer)
        detail = client.get(detail_url)
        assert detail.status_code == 200
        assert [item.pk for item in detail.context["maintenance_plans"]] == (
            [plan.pk] if granted else []
        )
        assert [row["link"].pk for row in detail.context["attachment_rows"]] == (
            [link.pk] if granted else []
        )
        html = detail.content.decode()
        assert (plan.name in html) is granted
        assert (link.attachment.safe_filename in html) is granted

        download = client.get(download_url)
        assert download.status_code == (200 if granted else 403)
        if granted:
            assert b"".join(download.streaming_content) == JPEG_BYTES
