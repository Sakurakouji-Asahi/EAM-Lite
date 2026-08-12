"""Small factories shared by the Sprint 3 acceptance tests.

This module intentionally contains no pytest tests of its own.  The completed
initialization helper writes a real ``InitializationSetting`` row: Sprint 3
tests never mock or bypass the production entry guard.
"""

from __future__ import annotations

import hashlib
import io
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from PIL import Image

from apps.coding.services import activate_scheme, create_scheme
from apps.assets.models import Asset, AssetCustomField, AttachmentLink
from apps.assets.services import create_asset_draft, upload_asset_attachment
from apps.masterdata.models import (
    AssetCategory,
    Attachment,
    Company,
    Department,
    Employee,
    InitializationSetting,
    Location,
    UserDepartmentScope,
)


PASSWORD = "Valid-Password-2026!"
def _jpeg_fixture():
    stream = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(stream, format="JPEG")
    return stream.getvalue()


# Real, fully decodable 1x1 JPEG. A plausible marker stream is insufficient.
JPEG_BYTES = _jpeg_fixture()
PDF_BYTES = b"%PDF-1.7\n1 0 obj<<>>endobj\nstartxref\n0\n%%EOF\n"


def make_user(username: str, *roles: str):
    user = get_user_model().objects.create_user(
        username=username,
        password=PASSWORD,
        display_name=username,
    )
    # Transactional PostgreSQL tests flush role seed rows.  Recreate only the
    # fixed role rows required by the test instead of relying on test order.
    groups = [Group.objects.get_or_create(name=role)[0] for role in roles]
    user.groups.set(groups)
    return user


def make_company(code: str = "C1", *, active: bool = True):
    return Company.objects.create(
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 公司",
        short_name=code,
        is_active=active,
    )


def complete_initialization(company, actor):
    """Persist the explicit Sprint 3 acceptance fixture in the database."""
    return InitializationSetting.objects.create(
        company=company,
        initialization_completed=True,
        company_configured=True,
        departments_configured=True,
        employees_configured=True,
        categories_configured=True,
        locations_configured=True,
        coding_scheme_configured=True,
        finance_rules_configured=True,
        permissions_configured=True,
        users_configured=True,
        completed_by=actor,
        completed_at=timezone.now(),
    )


def make_active_scheme(*, actor, company, key="S3"):
    """Create a structurally valid current scheme through Sprint 2 services."""
    draft = create_scheme(
        actor=actor,
        company=company,
        data={
            "scheme_key": key,
            "name": f"{key} Sprint 3 scheme",
            "description": "Sprint 3 requested-scheme fixture",
            "reset_mode": "never",
            "sequence_start": 1,
            "category_scope_level": None,
            "effective_from": timezone.localdate(),
            "effective_to": None,
        },
        segments=[
            {
                "sequence_order": 1,
                "segment_type": "sequence",
                "fixed_value": None,
                "format_string": None,
                "sequence_length": 4,
                "zero_pad": True,
            }
        ],
    )
    return activate_scheme(actor=actor, scheme=draft)


def make_structurally_valid_active_scheme(*, company, actor, key="S3"):
    """Build a database-valid foreign/current scheme for scope tests.

    Coding services reject non-current-company mutations by design.  This
    fixture therefore creates the complete scheme+segment state in one atomic
    transaction so PostgreSQL's deferred structure guards remain exercised.
    """
    from apps.masterdata.models import AssetCodingScheme, AssetCodingSegment

    with transaction.atomic():
        scheme = AssetCodingScheme.objects.create(
            company=company,
            name=f"{key} Sprint 3 scheme",
            scheme_key=key,
            version=1,
            status=AssetCodingScheme.Status.DRAFT,
            reset_mode=AssetCodingScheme.ResetMode.NEVER,
            sequence_start=1,
            effective_from=timezone.localdate(),
            created_by=actor,
        )
        AssetCodingSegment.objects.create(
            coding_scheme=scheme,
            sequence_order=1,
            segment_type=AssetCodingSegment.SegmentType.SEQUENCE,
            sequence_length=4,
            zero_pad=True,
        )
        AssetCodingScheme.objects.filter(pk=scheme.pk).update(
            status=AssetCodingScheme.Status.ACTIVE
        )
    scheme.refresh_from_db()
    return scheme


def make_department(company, code: str = "D1", *, parent=None, active=True):
    return Department.objects.create(
        company=company,
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 部门",
        parent=parent,
        is_active=active,
    )


def make_employee(
    company,
    department,
    number: str = "E001",
    *,
    user=None,
    status: str = "active",
    active: bool = True,
):
    return Employee.objects.create(
        company=company,
        employee_no=number,
        normalized_employee_no=number.casefold(),
        name=f"员工 {number}",
        department=department,
        user=user,
        employment_status=status,
        is_active=active,
        termination_date=date(2026, 8, 1) if status == "resigned" else None,
    )


def make_category(
    company,
    code: str = "EQ",
    *,
    parent=None,
    category_type: str = "equipment",
    active: bool = True,
):
    return AssetCategory.objects.create(
        company=company,
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 实物分类",
        parent=parent,
        category_type=category_type,
        is_active=active,
    )


def make_location(
    company,
    code: str,
    *,
    parent=None,
    location_type: str = "position",
    active: bool = True,
):
    return Location.objects.create(
        company=company,
        code=code,
        normalized_code=code.casefold(),
        name=f"{code} 位置",
        parent=parent,
        location_type=location_type,
        is_active=active,
    )


def make_location_tree(company, prefix: str = "L"):
    site = make_location(company, f"{prefix}1", location_type="site")
    area = make_location(
        company,
        f"{prefix}2",
        parent=site,
        location_type="workshop",
    )
    leaf = make_location(
        company,
        f"{prefix}3",
        parent=area,
        location_type="position",
    )
    return site, area, leaf


def grant_scope(user, company, department, *, descendants=True, assigned_by=None):
    return UserDepartmentScope.objects.create(
        company=company,
        user=user,
        department=department,
        include_descendants=descendants,
        assigned_by=assigned_by,
    )


def make_custom_field(
    company,
    category,
    code: str,
    field_type: str,
    *,
    required=False,
    options=None,
    active=True,
):
    field = AssetCustomField(
        company=company,
        category=category,
        name=f"{code} 字段",
        code=code,
        field_type=field_type,
        required=required,
        options_json=options,
        is_active=active,
    )
    field.full_clean()
    field.save()
    return field


def complete_asset_data(category, department, employee, location, **overrides):
    data = {
        "asset_name": "Sprint 3 测试设备",
        "category": category,
        "brand": "EAM",
        "model": "M-3",
        "manufacturer": "测试制造商",
        "serial_number": "SN-S3",
        "factory_number": "FN-S3",
        "historical_code": "OLD-S3",
        "quantity": 1,
        "unit": "台",
        "description": "Sprint 3 实物资料",
        "department": department,
        "responsible_employee": employee,
        "location": location,
        "acquisition_date": date(2026, 7, 1),
        "commissioning_date": date(2026, 7, 2),
        "is_maintenance_required": True,
        "notes": "非财务备注",
    }
    data.update(overrides)
    return data


def make_asset(
    *,
    actor,
    company,
    category,
    department=None,
    employee=None,
    location=None,
    custom_values=None,
    **overrides,
):
    data = complete_asset_data(
        category,
        department,
        employee,
        location,
        **overrides,
    )
    return create_asset_draft(
        actor=actor,
        company=company,
        data=data,
        custom_values=custom_values,
    )


def jpeg_upload(name: str = "asset.jpg", *, content_type="image/jpeg"):
    return SimpleUploadedFile(name, JPEG_BYTES, content_type=content_type)


def pdf_upload(name: str = "invoice.pdf", *, content_type="application/pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type=content_type)


def add_photo(actor, asset, *, role=AttachmentLink.Role.PHOTO):
    return upload_asset_attachment(
        actor=actor,
        asset=asset,
        uploaded_file=jpeg_upload(),
        role=role,
        security_class=AttachmentLink.SecurityClass.A0,
    )


def direct_attachment(
    company,
    actor,
    *,
    key="private/assets/test.jpg",
    filename="test.jpg",
    mime="image/jpeg",
    data=JPEG_BYTES,
    available=True,
):
    return Attachment.objects.create(
        company=company,
        storage_key=key,
        original_filename=filename,
        safe_filename=filename,
        file_size=len(data),
        mime_type=mime,
        sha256=hashlib.sha256(data).hexdigest(),
        uploaded_by=actor,
        malware_scan_status=Attachment.MalwareScanStatus.POLICY_LIMITED,
        is_available=available,
    )


def direct_draft(company, category, *, actor=None, **overrides):
    values = {
        "company": company,
        "asset_name": "直接模型草稿",
        "category": category,
        "quantity": 1,
        "tracking_mode": Asset.TrackingMode.SINGLE_ITEM,
        "created_by": actor,
        "updated_by": actor,
        "initialized_by": actor,
    }
    values.update(overrides)
    return Asset.objects.create(**values)
