from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.assets.models import Asset, AssetCodeHistory, AttachmentLink
from apps.assets.services import submit_asset_for_finance, upload_asset_attachment
from apps.audit.models import AuditLog
from apps.masterdata.models import AssetCodingScheme, Attachment, IssuedCode, SequenceCounter
from tests.test_sprint3_support import (
    JPEG_BYTES,
    add_photo,
    complete_initialization,
    direct_attachment,
    direct_draft,
    grant_scope,
    jpeg_upload,
    make_asset,
    make_category,
    make_company,
    make_custom_field,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
)


pytestmark = pytest.mark.django_db


def make_context(*, role="equipment", initialized=True):
    actor = make_user(f"{role}-actor", role)
    company = make_company()
    if initialized:
        complete_initialization(company, actor)
    department = make_department(company)
    employee = make_employee(company, department)
    category = make_category(company)
    site, area, location = make_location_tree(company)
    return actor, company, department, employee, category, site, area, location


def form_data(category, department, employee, location, **overrides):
    data = {
        "asset_name": "HTTP 检具",
        "category": str(category.pk),
        "brand": "HTTP",
        "model": "MODEL-HTTP",
        "manufacturer": "HTTP 制造商",
        "serial_number": "SERIAL-HTTP",
        "factory_number": "FACTORY-HTTP",
        "historical_code": "HISTORY-HTTP",
        "quantity": "1",
        "unit": "台",
        "description": "HTTP 说明",
        "department": str(department.pk),
        "responsible_employee": str(employee.pk),
        "location": str(location.pk),
        "acquisition_date": "2026-08-01",
        "commissioning_date": "2026-08-02",
        "is_maintenance_required": "on",
        "notes": "HTTP 备注",
    }
    data.update(overrides)
    return data


def test_all_asset_pages_require_login(client):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )

    urls = (
        reverse("assets:asset-list"),
        reverse("assets:asset-create"),
        reverse("assets:asset-detail", args=[asset.pk]),
        reverse("assets:asset-edit", args=[asset.pk]),
        reverse("assets:asset-submit", args=[asset.pk]),
        reverse("assets:attachment-upload", args=[asset.pk]),
    )
    for url in urls:
        response = client.get(url)
        assert response.status_code == 302
        assert "/login/" in response.url


def test_real_unfinished_initialization_blocks_list_create_and_direct_urls(client):
    actor, company, department, employee, category, _site, _area, location = make_context(
        initialized=False
    )
    asset = direct_draft(
        company,
        category,
        actor=actor,
        department=department,
        responsible_employee=employee,
        location=location,
    )
    client.force_login(actor)

    for url in (
        reverse("assets:asset-list"),
        reverse("assets:asset-create"),
        reverse("assets:asset-detail", args=[asset.pk]),
        reverse("assets:asset-edit", args=[asset.pk]),
        reverse("assets:asset-submit", args=[asset.pk]),
        reverse("assets:attachment-upload", args=[asset.pk]),
    ):
        assert client.get(url).status_code == 403


def test_create_edit_detail_and_dynamic_fields_work_without_financial_inputs(client):
    actor, company, department, employee, category, _site, _area, location = make_context()
    custom = make_custom_field(company, category, "COLOR", "select", options=["红", "蓝"])
    client.force_login(actor)
    create_data = form_data(category, department, employee, location)
    create_data[f"custom_{custom.pk}-value"] = "红"

    created = client.post(reverse("assets:asset-create"), create_data)

    assert created.status_code == 302
    asset = Asset.objects.get()
    assert created.url == reverse("assets:asset-detail", args=[asset.pk])
    assert asset.asset_code is None
    assert asset.custom_values.get().value_text == "红"

    detail = client.get(created.url)
    assert detail.status_code == 200
    content = detail.content.decode()
    assert asset.draft_number in content
    assert "分类扩展资料" in content
    assert "COLOR 字段" in content
    assert "红" in content
    assert "财务确认和财务资料将在 Sprint 4" not in content
    assert "当前步骤" in content
    assert "继续补齐实物资料和资产照片" in content

    edit_data = form_data(
        category,
        department,
        employee,
        location,
        asset_name="HTTP 已更新",
    )
    edit_data[f"custom_{custom.pk}-value"] = "蓝"
    edited = client.post(
        reverse("assets:asset-edit", args=[asset.pk]), edit_data
    )
    assert edited.status_code == 302
    asset.refresh_from_db()
    assert asset.asset_name == "HTTP 已更新"
    assert asset.custom_values.get().value_text == "蓝"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("original_cost", "5000.00"),
        ("accounting_treatment", "fixed_asset"),
        ("asset_status", "in_use"),
        ("asset_code", "FAKE"),
        ("requested_coding_scheme", "1"),
    ),
)
def test_constructed_http_forbidden_fields_are_403_audited_and_not_saved(
    client, field, value
):
    actor, _company, department, employee, category, _site, _area, location = make_context()
    client.force_login(actor)

    response = client.post(
        reverse("assets:asset-create"),
        form_data(category, department, employee, location, **{field: value}),
    )

    assert response.status_code == 403
    assert not Asset.objects.exists()
    log = AuditLog.objects.get(action="asset_forbidden_field_attempt")
    assert log.new_data_json["attempted_fields"] == [field]


def test_quantity_two_is_rejected_with_chinese_error_and_no_batch_entry(client):
    actor, _company, department, employee, category, _site, _area, location = make_context()
    client.force_login(actor)

    response = client.post(
        reverse("assets:asset-create"),
        form_data(category, department, employee, location, quantity="2"),
    )

    assert response.status_code == 200
    assert "V1 每条记录代表一件实物" in response.content.decode()
    assert not Asset.objects.exists()
    assert "/split/" not in response.content.decode()
    assert "/partial-transfer/" not in response.content.decode()


@pytest.mark.parametrize(
    "query_field",
    ("asset_name", "model", "serial_number", "responsible_employee"),
)
def test_list_searches_name_model_serial_and_responsible_employee(client, query_field):
    actor, company, department, employee, category, _site, _area, location = make_context()
    values = {
        "asset_name": "UNIQUE-NAME",
        "model": "UNIQUE-MODEL",
        "serial_number": "UNIQUE-SERIAL",
    }
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
        **values,
    )
    client.force_login(actor)
    term = employee.name if query_field == "responsible_employee" else values[query_field]

    response = client.get(reverse("assets:asset-list"), {"q": term})

    assert response.status_code == 200
    assert list(response.context["page"].object_list) == [asset]


def test_list_searches_displayed_draft_number(client):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    client.force_login(actor)

    response = client.get(reverse("assets:asset-list"), {"q": asset.draft_number})

    assert response.status_code == 200
    assert list(response.context["page"].object_list) == [asset]


@pytest.mark.parametrize(
    "filter_name",
    ("category", "department", "employee", "location", "asset_status"),
)
def test_list_filters_each_approved_dimension(client, filter_name):
    actor, company, department, employee, category, _site, _area, location = make_context()
    matching = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    other_department = make_department(company, "D2")
    other_employee = make_employee(company, other_department, "E2")
    other_category = make_category(company, "OTHER", category_type="other")
    _site2, _area2, other_location = make_location_tree(company, "X")
    make_asset(
        actor=actor,
        company=company,
        category=other_category,
        department=other_department,
        employee=other_employee,
        location=other_location,
    )
    params = {
        "category": str(category.pk),
        "department": str(department.pk),
        "employee": str(employee.pk),
        "location": str(location.pk),
        "asset_status": "draft",
    }
    client.force_login(actor)

    response = client.get(
        reverse("assets:asset-list"), {filter_name: params[filter_name]}
    )

    assert response.status_code == 200
    rows = list(response.context["page"].object_list)
    if filter_name == "asset_status":
        assert len(rows) == 2
    else:
        assert rows == [matching]


def test_department_manager_cannot_list_detail_edit_or_download_outside_scope(
    client, tmp_path
):
    actor, company, inside, inside_employee, category, _site, _area, location = make_context()
    manager = make_user("manager", "department_manager")
    outside = make_department(company, "OUT")
    outside_employee = make_employee(company, outside, "E2")
    grant_scope(manager, company, inside)
    own = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=inside,
        employee=inside_employee,
        location=location,
        asset_name="INSIDE",
    )
    foreign = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=outside,
        employee=outside_employee,
        location=location,
        asset_name="OUTSIDE-SECRET",
    )
    with override_settings(MEDIA_ROOT=tmp_path):
        link = add_photo(actor, foreign)
        client.force_login(manager)
        listing = client.get(reverse("assets:asset-list"))
        assert "INSIDE" in listing.content.decode()
        assert "OUTSIDE-SECRET" not in listing.content.decode()
        assert client.get(reverse("assets:asset-detail", args=[foreign.pk])).status_code == 404
        assert client.get(reverse("assets:asset-edit", args=[foreign.pk])).status_code == 404
        assert client.get(
            reverse("assets:attachment-download", args=[foreign.pk, link.pk])
        ).status_code == 404
        assert client.get(reverse("assets:asset-detail", args=[own.pk])).status_code == 200


def test_hr_receives_p0_summary_only_without_p1_or_attachment_metadata(client):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
        serial_number="P1-SECRET-SERIAL",
    )
    attachment = direct_attachment(
        company,
        actor,
        key="private/assets/p1.jpg",
        filename="P1-SECRET-FILENAME.jpg",
    )
    AttachmentLink.objects.create(
        company=company,
        attachment=attachment,
        asset=asset,
        role="photo",
        security_class="A0",
        created_by=actor,
    )
    hr = make_user("hr", "hr")
    client.force_login(hr)

    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    listing = client.get(reverse("assets:asset-list"))
    detail_html = detail.content.decode()
    list_html = listing.content.decode()

    assert detail.status_code == listing.status_code == 200
    assert asset.asset_name in detail_html
    assert "P1-SECRET-SERIAL" not in detail_html
    assert "P1-SECRET-FILENAME" not in detail_html
    assert "P1-SECRET-SERIAL" not in list_html


def test_a1_filename_and_download_are_hidden_from_equipment_and_admin_but_visible_to_finance(
    client,
):
    equipment, company, department, employee, category, _site, _area, location = make_context()
    finance = make_user("finance", "finance")
    admin = make_user("admin", "system_admin")
    asset = make_asset(
        actor=equipment,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    attachment = direct_attachment(
        company,
        finance,
        key="private/assets/a1.pdf",
        filename="A1-SECRET-INVOICE.pdf",
        mime="application/pdf",
        data=b"%PDF-1.7\n",
    )
    link = AttachmentLink.objects.create(
        company=company,
        attachment=attachment,
        asset=asset,
        role="invoice",
        security_class="A1",
        created_by=finance,
    )
    download_url = reverse("assets:attachment-download", args=[asset.pk, link.pk])

    for viewer in (equipment, admin):
        client.force_login(viewer)
        detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
        assert "A1-SECRET-INVOICE" not in detail.content.decode()
        assert client.get(download_url).status_code == 403

    client.force_login(finance)
    assert "A1-SECRET-INVOICE" in client.get(
        reverse("assets:asset-detail", args=[asset.pk])
    ).content.decode()


def test_authorized_download_is_attachment_nosniff_and_audited(client, tmp_path):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    with override_settings(MEDIA_ROOT=tmp_path):
        link = add_photo(actor, asset)
        client.force_login(actor)
        response = client.get(
            reverse("assets:attachment-download", args=[asset.pk, link.pk])
        )
        body = b"".join(response.streaming_content)

    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert body == JPEG_BYTES
    assert AuditLog.objects.filter(action="asset_attachment_download").count() == 1


@pytest.mark.parametrize(
    "scan_status",
    [Attachment.MalwareScanStatus.PENDING, Attachment.MalwareScanStatus.REJECTED],
)
def test_unapproved_scan_status_is_hidden_from_detail_and_download(client, tmp_path, scan_status):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    attachment = direct_attachment(
        company,
        actor,
        key=f"private/assets/{scan_status}.jpg",
        filename=f"HIDDEN-{scan_status}.jpg",
        available=False,
    )
    attachment.malware_scan_status = scan_status
    attachment.save(update_fields=["malware_scan_status"])
    link = AttachmentLink.objects.create(
        company=company,
        attachment=attachment,
        asset=asset,
        role=AttachmentLink.Role.PHOTO,
        security_class=AttachmentLink.SecurityClass.A0,
        created_by=actor,
    )
    client.force_login(actor)

    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    download = client.get(
        reverse("assets:attachment-download", args=[asset.pk, link.pk])
    )

    assert detail.status_code == 200
    assert f"HIDDEN-{scan_status}" not in detail.content.decode()
    assert download.status_code == 404
    assert not AuditLog.objects.filter(action="asset_attachment_download").exists()


def test_submit_action_stops_at_pending_finance_without_formal_code_or_button(
    client, tmp_path
):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    with override_settings(MEDIA_ROOT=tmp_path):
        add_photo(actor, asset)
        client.force_login(actor)
        response = client.post(
            reverse("assets:asset-submit", args=[asset.pk]), {"confirm": "on"}
        )

    assert response.status_code == 302
    asset.refresh_from_db()
    assert asset.asset_status == "pending_finance"
    assert asset.asset_code is None
    assert asset.current_issued_code_id is None
    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    html = detail.content.decode()
    assert "当前等待 finance 明确会计认定" in html
    assert "确认并生成正式编号" not in html
    assert reverse("finance:finance-confirm", args=[asset.pk]) not in html
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AssetCodeHistory.objects.count() == 0


def test_repeat_http_submit_is_idempotent_and_one_audit(client, tmp_path):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    with override_settings(MEDIA_ROOT=tmp_path):
        add_photo(actor, asset)
        client.force_login(actor)
        url = reverse("assets:asset-submit", args=[asset.pk])
        assert client.post(url, {"confirm": "on"}).status_code == 302
        assert client.post(url, {"confirm": "on"}).status_code == 302

    assert AuditLog.objects.filter(action="asset_submit_finance").count() == 1


def test_system_admin_has_only_requested_scheme_action_not_edit_action(client):
    equipment, company, department, employee, category, _site, _area, location = make_context()
    admin = make_user("admin", "system_admin")
    asset = make_asset(
        actor=equipment,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    client.force_login(admin)

    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    html = detail.content.decode()

    assert detail.status_code == 200
    assert "指定编码方案" in html
    assert "编辑草稿" not in html
    assert client.get(reverse("assets:asset-edit", args=[asset.pk])).status_code == 403


def test_finance_can_see_pending_asset_and_sprint4_formalization_route(client):
    actor, company, department, employee, category, _site, _area, location = make_context()
    finance = make_user("finance", "finance")
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    add_photo(actor, asset)
    asset = submit_asset_for_finance(actor=actor, asset=asset)
    client.force_login(finance)

    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    html = detail.content.decode()

    assert detail.status_code == 200
    assert "待财务确认" in html
    assert "财务信息" in html
    assert "进入财务确认" in html
    assert reverse("finance:finance-confirm", args=[asset.pk]) in html


def test_action_gets_render_explicit_confirmation_forms(client, tmp_path):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    client.force_login(actor)
    submit_page = client.get(reverse("assets:asset-submit", args=[asset.pk]))
    delete_page = client.get(reverse("assets:asset-delete", args=[asset.pk]))

    assert submit_page.status_code == delete_page.status_code == 200
    assert "确认提交" in submit_page.content.decode()
    assert "删除原因" in delete_page.content.decode()


def test_upload_and_void_http_paths_preserve_evidence(client, tmp_path):
    actor, company, department, employee, category, _site, _area, location = make_context()
    asset = make_asset(
        actor=actor,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    client.force_login(actor)
    with override_settings(MEDIA_ROOT=tmp_path):
        uploaded = client.post(
            reverse("assets:attachment-upload", args=[asset.pk]),
            {
                "role": "photo",
                "security_class": "A0",
                "file": jpeg_upload(),
            },
        )
        assert uploaded.status_code == 302
        link = AttachmentLink.objects.get()
        path = tmp_path / link.attachment.storage_key
        assert path.exists()
        voided = client.post(
            reverse("assets:attachment-void", args=[asset.pk, link.pk]),
            {"reason": "图片错误"},
        )

    assert voided.status_code == 302
    link.refresh_from_db()
    assert link.status == "voided"
    assert path.exists()
    assert client.get(
        reverse("assets:attachment-download", args=[asset.pk, link.pk])
    ).status_code == 403
