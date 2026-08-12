"""Controlled Sprint 3 asset-master services.

This module deliberately stops at ``pending_finance``.  It never allocates an
official code and never writes SequenceCounter, IssuedCode or AssetCodeHistory.
"""

from __future__ import annotations

import hashlib
import io
import uuid
import warnings
import zipfile
from collections.abc import Mapping
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import get_valid_filename
from PIL import Image, UnidentifiedImageError

from apps.assets.permissions import (
    can_create_asset_draft,
    can_create_attachment_link,
    can_delete_asset_draft,
    can_set_requested_coding_scheme,
    can_submit_asset,
    can_void_attachment_link,
    can_withdraw_asset,
    require_edit_asset_draft,
    require_view_asset,
)
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company
from apps.masterdata.services import get_system_setting


ASSET_EDIT_FIELDS = (
    "asset_name",
    "category",
    "brand",
    "model",
    "manufacturer",
    "serial_number",
    "factory_number",
    "historical_code",
    "unit",
    "description",
    "department",
    "responsible_employee",
    "location",
    "acquisition_date",
    "commissioning_date",
    "is_maintenance_required",
    "notes",
)
FINANCIAL_FIELD_NAMES = frozenset(
    {
        "accounting_treatment",
        "fixed_asset_category",
        "original_cost",
        "capitalization_date",
        "depreciation_method",
        "useful_life_months",
        "salvage_rate",
        "salvage_amount",
        "opening_accumulated_depreciation",
        "impairment",
        "book_value",
        "finance_remark",
    }
)

MIME_BY_EXTENSION = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_IMAGE_PIXELS = 40_000_000


def _require_current_company(company=None):
    """Resolve the authoritative V1 company; never trust a caller's object."""
    active = current_company()
    if active is None or not active.is_active:
        raise PermissionDenied("当前没有启用公司。")
    if company is not None and getattr(company, "pk", None) != active.pk:
        raise PermissionDenied("目标记录不属于当前公司。")
    return active


def _lock_current_asset(asset):
    from apps.assets.models import Asset

    company = _require_current_company()
    asset_id = getattr(asset, "pk", None)
    try:
        return Asset.objects.select_for_update().select_related("company").get(
            pk=asset_id, company=company
        )
    except (Asset.DoesNotExist, TypeError, ValueError) as exc:
        raise PermissionDenied("目标资产不存在或不属于当前公司。") from exc


def _lock_current_attachment_link(link):
    from apps.assets.models import AttachmentLink

    company = _require_current_company()
    link_id = getattr(link, "pk", None)
    try:
        return (
            AttachmentLink.objects.select_for_update()
            .select_related("asset", "attachment", "company")
            .get(pk=link_id, company=company, asset__company=company)
        )
    except (AttachmentLink.DoesNotExist, TypeError, ValueError) as exc:
        raise PermissionDenied("目标附件关联不存在或不属于当前公司。") from exc


def _require_initialization_completed(company):
    from apps.masterdata.models import InitializationSetting

    if not InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists():
        raise PermissionDenied("系统初始化尚未完成，资产建账入口暂不可用。")


def _snapshot(instance, fields=ASSET_EDIT_FIELDS):
    result = {}
    for field in fields:
        value = getattr(instance, field)
        result[field] = str(value.pk) if hasattr(value, "pk") else value
    return result


def _custom_values_snapshot(asset):
    """Return stable, non-executable typed values for the business audit."""
    result = {}
    for value in asset.custom_values.select_related("custom_field").order_by(
        "custom_field__normalized_code"
    ):
        field_type = value.custom_field.field_type
        stored = {
            "text": value.value_text,
            "select": value.value_text,
            "decimal": value.value_decimal,
            "date": value.value_date,
            "boolean": value.value_boolean,
        }.get(field_type)
        result[value.custom_field.code] = stored
    return result


def _audit(*, actor, action, instance, old_data=None, new_data=None, request=None):
    company = instance.company
    return write_business_audit_log(
        company=company,
        user=actor,
        action=action,
        object_type=instance._meta.object_name,
        object_id=instance.pk,
        old_data=old_data or {},
        new_data=new_data or {},
        **request_audit_context(request),
    )


def _save(instance):
    instance.full_clean()
    try:
        instance.save()
    except IntegrityError as exc:
        raise ValidationError("保存失败：数据与现有记录或数据库约束冲突。") from exc
    return instance


def _controlled_update(model, *, pk, values):
    """Bypass model convenience guards inside one audited domain transaction."""
    from django.db import connection

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
    updated = models_query_update(model, pk=pk, values=values)
    if updated != 1:
        raise ValidationError("受控状态更新未命中唯一目标记录。")


def models_query_update(model, *, pk, values):
    # Call Django's base QuerySet implementation explicitly.  Public managers
    # reject these fields so callers cannot casually bypass the Service.
    from django.db.models.query import QuerySet

    queryset = model.objects.filter(pk=pk)
    return QuerySet.update(queryset, **values)


def _controlled_delete_asset_draft(*, pk):
    """Consume one transaction-local capability to delete one locked draft."""
    from django.db import connection
    from django.db.models.query import QuerySet

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s, %s, true)",
                ["eam_lite.controlled_asset_mutation", "on"],
            )
    from apps.assets.models import Asset

    queryset = Asset.objects.filter(pk=pk)
    deleted, detail = QuerySet.delete(queryset)
    if deleted != 1 or detail.get(Asset._meta.label, 0) != 1:
        raise ValidationError("受控草稿删除未命中唯一且无引用的资产。")


def _reject_forbidden_payload(data):
    forbidden = FINANCIAL_FIELD_NAMES.intersection(data)
    if forbidden:
        labels = "、".join(sorted(forbidden))
        raise PermissionDenied(f"资产实物表单不得提交财务字段：{labels}。")
    if "asset_status" in data or "record_status" in data:
        raise PermissionDenied("资产状态只能通过受控状态 Service 修改。")
    if "asset_code" in data or "current_issued_code" in data:
        raise PermissionDenied("正式编号字段不能通过资产草稿表单修改。")
    if "requested_coding_scheme" in data:
        raise PermissionDenied("指定编码方案必须使用 system_admin 专用 Service。")
    if "quantity" in data and str(data["quantity"]).strip() != "1":
        raise ValidationError({"quantity": "V1 每条资产记录数量必须为 1。"})
    if "tracking_mode" in data and data["tracking_mode"] != "single_item":
        raise ValidationError({"tracking_mode": "V1 只允许单件追踪。"})


def _apply_asset_data(asset, data):
    _reject_forbidden_payload(data)
    for field in ASSET_EDIT_FIELDS:
        if field in data:
            setattr(asset, field, data[field])
    asset.quantity = 1
    asset.tracking_mode = "single_item"
    return asset


def _validate_create_scope(actor, company, data):
    department = data.get("department")
    if not can_create_asset_draft(actor, company, department):
        raise PermissionDenied("您没有在此范围新建资产草稿的权限。")


def _custom_value_payload(custom_field, value):
    from apps.assets.models import AssetCustomField

    payload = {
        "value_text": None,
        "value_decimal": None,
        "value_date": None,
        "value_boolean": None,
    }
    column = {
        AssetCustomField.FieldType.TEXT: "value_text",
        AssetCustomField.FieldType.SELECT: "value_text",
        AssetCustomField.FieldType.DECIMAL: "value_decimal",
        AssetCustomField.FieldType.DATE: "value_date",
        AssetCustomField.FieldType.BOOLEAN: "value_boolean",
    }.get(custom_field.field_type)
    if column is None:
        raise ValidationError("不支持的动态字段类型。")
    payload[column] = value
    return payload


def _replace_custom_values(*, asset, custom_values):
    from apps.assets.models import AssetCustomField, AssetCustomValue

    if custom_values is None:
        return
    custom_values = {str(key): value for key, value in custom_values.items()}
    supplied = set(custom_values)
    fields = {
        str(field.pk): field
        for field in AssetCustomField.objects.filter(
            company=asset.company, category=asset.category, is_active=True
        )
    }
    unknown = supplied.difference(fields)
    if unknown:
        raise ValidationError("包含不适用于当前分类的动态字段。")

    AssetCustomValue.objects.filter(asset=asset).exclude(
        custom_field_id__in=supplied
    ).delete()
    for field_id, raw_value in custom_values.items():
        field = fields[str(field_id)]
        if raw_value in (None, ""):
            AssetCustomValue.objects.filter(asset=asset, custom_field=field).delete()
            continue
        defaults = _custom_value_payload(field, raw_value)
        value, _ = AssetCustomValue.objects.get_or_create(
            company=asset.company,
            asset=asset,
            custom_field=field,
            defaults=defaults,
        )
        if not _:
            for name, item in defaults.items():
                setattr(value, name, item)
        _save(value)


@transaction.atomic
def create_asset_draft(
    *,
    actor,
    company,
    data,
    custom_values=None,
    initialization_source="manual",
    request=None,
):
    from apps.assets.models import Asset

    company = _require_current_company(company)
    _require_initialization_completed(company)
    if initialization_source not in {"manual", "excel_import"}:
        raise ValidationError(
            {"initialization_source": "初始化来源只能是手工录入或受控 Excel 导入。"}
        )
    _validate_create_scope(actor, company, data)
    asset = _apply_asset_data(Asset(company=company), data)
    asset.asset_status = Asset.AssetStatus.DRAFT
    asset.record_status = Asset.RecordStatus.ACTIVE
    asset.asset_code = None
    asset.current_issued_code = None
    asset.initialization_source = initialization_source
    asset.initialization_date = timezone.localdate()
    asset.initialized_by = actor
    asset.created_by = actor
    asset.updated_by = actor
    _save(asset)
    _replace_custom_values(asset=asset, custom_values=custom_values)
    _audit(
        actor=actor,
        action="asset_draft_create",
        instance=asset,
        new_data={
            **_snapshot(asset),
            "quantity": 1,
            "asset_status": "draft",
            "initialization_source": initialization_source,
            "custom_values": _custom_values_snapshot(asset),
        },
        request=request,
    )
    return asset


@transaction.atomic
def update_asset_draft(
    *, actor, asset, data, custom_values=None, request=None
):
    from apps.assets.models import Asset

    asset = _lock_current_asset(asset)
    _require_initialization_completed(asset.company)
    require_edit_asset_draft(actor, asset)
    old = _snapshot(asset)
    old["custom_values"] = _custom_values_snapshot(asset)
    old_scope = (
        asset.department_id,
        asset.responsible_employee_id,
        asset.location_id,
        asset.category_id,
    )
    old_category_id = asset.category_id
    _apply_asset_data(asset, data)
    # The existing object may be in a department_manager's scope while the
    # requested destination is not. Re-authorize against the post-change
    # department before persisting, independently of ModelForm querysets.
    if not can_create_asset_draft(actor, asset.company, asset.department):
        raise PermissionDenied("您没有把资产改挂到目标部门的权限。")
    if asset.category_id != old_category_id:
        existing_values = asset.custom_values.all()
        if custom_values is None and existing_values.exists():
            raise ValidationError(
                {"category": "分类变更会使已有动态值失去适用范围，请明确提交新分类的动态值。"}
            )
        if custom_values is not None:
            existing_values.delete()
    asset.updated_by = actor
    _save(asset)
    _replace_custom_values(asset=asset, custom_values=custom_values)
    _audit(
        actor=actor,
        action="asset_draft_update",
        instance=asset,
        old_data=old,
        new_data={**_snapshot(asset), "custom_values": _custom_values_snapshot(asset)},
        request=request,
    )
    new_scope = (
        asset.department_id,
        asset.responsible_employee_id,
        asset.location_id,
        asset.category_id,
    )
    if new_scope != old_scope:
        _audit(
            actor=actor,
            action="asset_draft_scope_update",
            instance=asset,
            old_data={
                "department": old_scope[0],
                "responsible_employee": old_scope[1],
                "location": old_scope[2],
                "category": old_scope[3],
            },
            new_data={
                "department": new_scope[0],
                "responsible_employee": new_scope[1],
                "location": new_scope[2],
                "category": new_scope[3],
            },
            request=request,
        )
    return asset


@transaction.atomic
def set_requested_coding_scheme(
    *, actor, asset, coding_scheme, request=None
):
    from apps.assets.models import Asset

    asset = _lock_current_asset(asset)
    _require_initialization_completed(asset.company)
    if not can_set_requested_coding_scheme(actor, asset):
        raise PermissionDenied("只有 system_admin 可在正式化前指定编码方案版本。")
    if coding_scheme is not None:
        from apps.masterdata.models import AssetCodingScheme

        try:
            coding_scheme = AssetCodingScheme.objects.select_for_update().get(
                pk=coding_scheme.pk, company=asset.company
            )
        except (AttributeError, AssetCodingScheme.DoesNotExist) as exc:
            raise ValidationError(
                {"requested_coding_scheme": "编码方案不存在或已被删除。"}
            ) from exc
        today = timezone.localdate()
        if (
            coding_scheme.company_id != asset.company_id
            or coding_scheme.status != "active"
            or coding_scheme.effective_from is None
            or coding_scheme.effective_from > today
            or (
                coding_scheme.effective_to is not None
                and coding_scheme.effective_to < today
            )
        ):
            raise ValidationError(
                {"requested_coding_scheme": "编码方案必须属于同公司且当前生效。"}
            )
    old_id = asset.requested_coding_scheme_id
    requested_id = coding_scheme.pk if coding_scheme is not None else None
    _controlled_update(
        Asset,
        pk=asset.pk,
        values={
            "requested_coding_scheme_id": requested_id,
            "updated_by": actor,
            "updated_at": timezone.now(),
        },
    )
    asset.requested_coding_scheme = coding_scheme
    asset.updated_by = actor
    if old_id != asset.requested_coding_scheme_id:
        _audit(
            actor=actor,
            action="asset_coding_scheme_select",
            instance=asset,
            old_data={"requested_coding_scheme": old_id},
            new_data={"requested_coding_scheme": asset.requested_coding_scheme_id},
            request=request,
        )
    return asset


def _validate_submission(asset):
    from apps.assets.models import AssetCustomField, AttachmentLink

    errors = {}
    for field in ("asset_name", "category", "unit", "department", "responsible_employee", "location"):
        value = getattr(asset, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors[field] = "提交财务确认前必须填写此字段。"
    if asset.quantity != 1:
        errors["quantity"] = "V1 每条资产记录数量必须为 1。"
    if asset.asset_code is not None or asset.current_issued_code_id is not None:
        errors["asset_code"] = "Sprint 3 提交不得包含正式编号。"
    if asset.responsible_employee_id and (
        asset.responsible_employee.department_id != asset.department_id
        or asset.responsible_employee.employment_status != "active"
        or not asset.responsible_employee.is_active
    ):
        errors["responsible_employee"] = "责任人必须属于当前部门且在职启用。"
    if asset.location_id and not asset.location.is_active:
        errors["location"] = "位置必须处于启用状态。"
    elif asset.location_id and asset.location.children.exists():
        errors["location"] = "提交财务确认时必须选择位置树的叶级节点。"

    has_photo = AttachmentLink.objects.filter(
        asset=asset,
        role__in=(AttachmentLink.Role.COVER, AttachmentLink.Role.PHOTO),
        security_class=AttachmentLink.SecurityClass.A0,
        status=AttachmentLink.Status.ACTIVE,
        attachment__is_available=True,
        attachment__malware_scan_status__in=("policy_limited", "clean"),
        attachment__mime_type__startswith="image/",
    ).exists()
    if not has_photo:
        errors["attachments"] = "提交财务确认前至少需要一张有效资产照片。"

    required_fields = AssetCustomField.objects.filter(
        company=asset.company,
        category=asset.category,
        required=True,
        is_active=True,
    )
    existing_ids = set(
        asset.custom_values.filter(custom_field__in=required_fields).values_list(
            "custom_field_id", flat=True
        )
    )
    missing = [field.name for field in required_fields if field.pk not in existing_ids]
    if missing:
        errors["custom_values"] = f"缺少必填动态字段：{'、'.join(missing)}。"
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def submit_asset_for_finance(
    *, actor, asset, request=None
):
    from apps.assets.models import Asset

    asset = _lock_current_asset(asset)
    _require_initialization_completed(asset.company)
    if asset.asset_status == Asset.AssetStatus.PENDING_FINANCE:
        require_view_asset(actor, asset)
        return asset
    if not can_submit_asset(actor, asset):
        raise PermissionDenied("您没有提交此资产财务确认的权限。")
    _validate_submission(asset)
    old = {"asset_status": asset.asset_status}
    submitted_at = timezone.now()
    _controlled_update(
        Asset,
        pk=asset.pk,
        values={
            "asset_status": Asset.AssetStatus.PENDING_FINANCE,
            "submitted_by": actor,
            "submitted_at": submitted_at,
            "updated_by": actor,
            "updated_at": timezone.now(),
        },
    )
    asset.asset_status = Asset.AssetStatus.PENDING_FINANCE
    asset.submitted_by = actor
    asset.submitted_at = submitted_at
    asset.updated_by = actor
    _audit(
        actor=actor,
        action="asset_submit_finance",
        instance=asset,
        old_data=old,
        new_data={
            "asset_status": asset.asset_status,
            "submitted_by": actor.pk,
            "submitted_at": asset.submitted_at,
            "asset_code": None,
        },
        request=request,
    )
    return asset


@transaction.atomic
def withdraw_asset_to_draft(
    *, actor, asset, reason, request=None
):
    from apps.assets.models import Asset

    asset = _lock_current_asset(asset)
    _require_initialization_completed(asset.company)
    if not str(reason or "").strip():
        raise ValidationError({"reason": "撤回或退回更正必须填写原因。"})
    if asset.asset_status == Asset.AssetStatus.DRAFT:
        require_view_asset(actor, asset)
        return asset
    if not can_withdraw_asset(actor, asset):
        raise PermissionDenied("只有原提交人或 finance 可以撤回/退回此资产。")
    old = {
        "asset_status": asset.asset_status,
        "submitted_by": asset.submitted_by_id,
        "submitted_at": asset.submitted_at,
    }
    _controlled_update(
        Asset,
        pk=asset.pk,
        values={
            "asset_status": Asset.AssetStatus.DRAFT,
            "submitted_by": None,
            "submitted_at": None,
            "updated_by": actor,
            "updated_at": timezone.now(),
        },
    )
    asset.asset_status = Asset.AssetStatus.DRAFT
    asset.submitted_by = None
    asset.submitted_at = None
    asset.updated_by = actor
    _audit(
        actor=actor,
        action="asset_withdraw_to_draft",
        instance=asset,
        old_data=old,
        new_data={"asset_status": "draft", "reason": str(reason).strip()},
        request=request,
    )
    return asset


@transaction.atomic
def delete_asset_draft(
    *, actor, asset, reason, request=None
):
    from apps.assets.models import Asset

    asset = _lock_current_asset(asset)
    _require_initialization_completed(asset.company)
    if not str(reason or "").strip():
        raise ValidationError({"reason": "删除草稿必须填写原因。"})
    if not can_delete_asset_draft(actor, asset):
        raise PermissionDenied("您没有删除此资产草稿的权限。")
    if asset.attachment_links.exists() or asset.custom_values.exists():
        raise ValidationError("只有没有附件或其他业务引用的草稿可以删除。")
    snapshot = {**_snapshot(asset), "reason": str(reason).strip()}
    _audit(
        actor=actor,
        action="asset_draft_delete",
        instance=asset,
        old_data=snapshot,
        new_data={},
        request=request,
    )
    _controlled_delete_asset_draft(pk=asset.pk)


def _read_upload(uploaded_file, limit):
    if getattr(uploaded_file, "size", 0) > limit:
        raise ValidationError(f"文件超过当前上限 {limit} 字节。")
    chunks = []
    total = 0
    for chunk in uploaded_file.chunks():
        total += len(chunk)
        if total > limit:
            raise ValidationError(f"文件超过当前上限 {limit} 字节。")
        chunks.append(chunk)
    if not total:
        raise ValidationError("上传文件不能为空。")
    return b"".join(chunks)


def _is_office_container(data, expected_content_type):
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 1_000:
                return False
            total_expanded = 0
            for member in members:
                total_expanded += member.file_size
                if member.file_size > 20 * 1024 * 1024:
                    return False
                if total_expanded > 100 * 1024 * 1024:
                    return False
                if member.compress_size == 0:
                    if member.file_size:
                        return False
                elif member.file_size / member.compress_size > 100:
                    return False
            names = {member.filename.lower() for member in members}
            if any("vbaproject.bin" in name for name in names):
                return False
            content_types = archive.read("[Content_Types].xml")
            return expected_content_type in content_types
    except (KeyError, zipfile.BadZipFile):
        return False


def _validate_image_dimensions(extension, data):
    expected_format = {
        "jpg": "JPEG",
        "jpeg": "JPEG",
        "png": "PNG",
        "webp": "WEBP",
    }[extension]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != expected_format:
                    raise ValidationError("图片实际格式与扩展名不一致。")
                width, height = image.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise ValidationError("图片像素数量超过安全上限。")
                image.verify()
            # ``verify`` checks the container without decoding pixels. Reopen
            # and fully load once so a forged header or truncated stream cannot
            # be published as an available attachment.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except ValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ValidationError("图片无法安全解码或文件结构不完整。") from exc


def _detect_mime(extension, data):
    if extension in {"jpg", "jpeg", "png", "webp"}:
        _validate_image_dimensions(extension, data)
        return MIME_BY_EXTENSION[extension]
    if extension == "pdf" and data.startswith(b"%PDF-"):
        if (
            b"%%EOF" not in data[-2048:]
            or b" obj" not in data
            or b"endobj" not in data
            or b"startxref" not in data[-4096:]
        ):
            raise ValidationError("PDF 文件结构不完整。")
        return "application/pdf"
    if extension == "xlsx" and _is_office_container(
        data, b"spreadsheetml.sheet.main+xml"
    ):
        return MIME_BY_EXTENSION[extension]
    if extension == "docx" and _is_office_container(
        data, b"wordprocessingml.document.main+xml"
    ):
        return MIME_BY_EXTENSION[extension]
    raise ValidationError("文件实际内容与扩展名不匹配或包含不允许的宏/对象。")


def _validate_filename(filename):
    name = Path(str(filename or "")).name
    if not name or name != str(filename) or "\x00" in name:
        raise ValidationError("文件名包含危险路径或空字符。")
    suffixes = [part.lower() for part in Path(name).suffixes]
    if len(suffixes) != 1:
        raise ValidationError("不允许无扩展名或双扩展名文件。")
    return name, suffixes[0].lstrip(".")


@transaction.atomic
def upload_asset_attachment(
    *,
    actor,
    asset,
    uploaded_file,
    role,
    security_class,
    request=None,
):
    from apps.assets.models import AttachmentLink
    from apps.masterdata.models import Attachment

    asset = _lock_current_asset(asset)
    _require_initialization_completed(asset.company)
    if not can_create_attachment_link(actor, asset, security_class):
        raise PermissionDenied("您没有上传此安全分类附件的权限。")
    if role in {AttachmentLink.Role.COVER, AttachmentLink.Role.PHOTO} and security_class != "A0":
        raise ValidationError("封面和资产照片只能使用 A0 普通分类。")
    original_name, extension = _validate_filename(uploaded_file.name)
    allowed = set(
        get_system_setting(company=asset.company, key="attachment_allowed_extensions")
    )
    if extension not in allowed or extension not in MIME_BY_EXTENSION:
        raise ValidationError("当前公司未允许该附件扩展名。")
    limit = get_system_setting(company=asset.company, key="attachment_max_size_bytes")
    data = _read_upload(uploaded_file, limit)
    detected_mime = _detect_mime(extension, data)
    client_mime = str(getattr(uploaded_file, "content_type", "") or "").lower()
    if client_mime and client_mime != detected_mime:
        raise ValidationError("客户端 MIME 与文件实际类型不一致。")

    storage_key = f"private/assets/{asset.company_id}/{uuid.uuid4().hex}.{extension}"
    # All scope, authorization and content validation has completed while the
    # authoritative asset row remains locked by this outer atomic Service.
    saved_key = default_storage.save(storage_key, ContentFile(data))
    linked = False
    try:
        attachment = Attachment(
            company=asset.company,
            storage_key=saved_key,
            original_filename=original_name[:255],
            safe_filename=(get_valid_filename(original_name) or f"attachment.{extension}")[:255],
            file_size=len(data),
            mime_type=detected_mime,
            sha256=hashlib.sha256(data).hexdigest(),
            uploaded_by=actor,
            malware_scan_status=Attachment.MalwareScanStatus.POLICY_LIMITED,
            is_available=False,
        )
        _save(attachment)
        link = AttachmentLink(
            company=asset.company,
            attachment=attachment,
            asset=asset,
            role=role,
            security_class=security_class,
            created_by=actor,
        )
        _save(link)
        attachment.is_available = True
        _save(attachment)
        _audit(
            actor=actor,
            action="asset_attachment_create",
            instance=link,
            new_data={
                "asset": str(asset.pk),
                "role": role,
                "security_class": security_class,
                "file_size": len(data),
                "mime_type": detected_mime,
                "sha256": attachment.sha256,
            },
            request=request,
        )
        linked = True
        return link
    finally:
        if not linked and default_storage.exists(saved_key):
            default_storage.delete(saved_key)


@transaction.atomic
def void_asset_attachment(
    *, actor, link, reason, request=None
):
    from apps.assets.models import AttachmentLink

    link = _lock_current_attachment_link(link)
    _require_initialization_completed(link.company)
    if not str(reason or "").strip():
        raise ValidationError({"reason": "作废附件必须填写原因。"})
    if link.status == AttachmentLink.Status.VOIDED:
        return link
    if not can_void_attachment_link(actor, link):
        raise PermissionDenied("您没有作废此附件的权限。")
    void_reason = str(reason).strip()
    voided_at = timezone.now()
    _controlled_update(
        AttachmentLink,
        pk=link.pk,
        values={
            "status": AttachmentLink.Status.VOIDED,
            "void_reason": void_reason,
            "voided_by": actor,
            "voided_at": voided_at,
        },
    )
    link.status = AttachmentLink.Status.VOIDED
    link.void_reason = void_reason
    link.voided_by = actor
    link.voided_at = voided_at
    _audit(
        actor=actor,
        action="asset_attachment_void",
        instance=link,
        old_data={"status": "active"},
        new_data={
            "status": "voided",
            "reason": link.void_reason,
            "security_class": link.security_class,
        },
        request=request,
    )
    return link
