"""Transactional management services for Sprint 2 coding configuration.

This module deliberately contains no official-code issuance or allocation
primitive.  SequenceCounter and IssuedCode remain empty schema foundations
until Asset formalisation connects them in Sprint 4.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from functools import wraps
import unicodedata

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.coding.domain import is_effective, validate_scheme_structure
from apps.masterdata.permissions import current_company, require_roles


SCHEME_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "scheme_key",
        "description",
        "reset_mode",
        "sequence_start",
        "category_scope_level",
        "effective_from",
        "effective_to",
    }
)
CLONE_OVERRIDE_FIELDS = frozenset(
    {
        "name",
        "description",
        "reset_mode",
        "sequence_start",
        "category_scope_level",
        "effective_from",
        "effective_to",
    }
)
SEGMENT_FIELDS = (
    "sequence_order",
    "segment_type",
    "fixed_value",
    "format_string",
    "sequence_length",
    "zero_pad",
)
SCHEME_SNAPSHOT_FIELDS = (
    "name",
    "scheme_key",
    "version",
    "description",
    "status",
    "is_default",
    "reset_mode",
    "sequence_start",
    "category_scope_level",
    "effective_from",
    "effective_to",
    "previous_version_id",
)


def _serializable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "pk"):
        return str(value.pk)
    return value


def _snapshot(instance, fields=SCHEME_SNAPSHOT_FIELDS):
    return {field: _serializable(getattr(instance, field)) for field in fields}


def _segment_snapshot(segment):
    return {field: _serializable(getattr(segment, field)) for field in SEGMENT_FIELDS}


def _audit(
    *, company, actor, action, instance, old_data=None, new_data=None, request=None
):
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


def _require_system_admin(actor):
    require_roles(actor, {"system_admin"}, "只有 system_admin 可以维护编码方案。")


def _require_current_company(company):
    selected = current_company(include_inactive=True)
    if (
        company is None
        or selected is None
        or getattr(company, "pk", None) != selected.pk
    ):
        raise PermissionDenied("目标记录不属于当前公司。")
    return company


def _models():
    # Delayed import avoids a masterdata -> coding domain import cycle while
    # models are being registered by Django.
    from apps.masterdata.models import (
        AssetCategory,
        AssetCodingScheme,
        AssetCodingSegment,
        Company,
        InitializationSetting,
        IssuedCode,
    )

    return {
        "AssetCategory": AssetCategory,
        "AssetCodingScheme": AssetCodingScheme,
        "AssetCodingSegment": AssetCodingSegment,
        "Company": Company,
        "InitializationSetting": InitializationSetting,
        "IssuedCode": IssuedCode,
    }


def _lock_company(company):
    Company = _models()["Company"]
    try:
        locked = Company.objects.select_for_update().get(pk=company.pk)
    except Company.DoesNotExist as exc:
        raise ValidationError("公司不存在。") from exc
    _require_current_company(locked)
    return locked


def _lock_scheme(scheme):
    AssetCodingScheme = _models()["AssetCodingScheme"]
    try:
        company_id = AssetCodingScheme.objects.values_list("company_id", flat=True).get(
            pk=scheme.pk
        )
    except AssetCodingScheme.DoesNotExist as exc:
        raise ValidationError("编码方案不存在。") from exc
    # Every coding mutation uses the company row as its first lock.  Concurrent
    # default switches on two different scheme rows therefore serialize in one
    # order instead of deadlocking while each transaction holds the other row.
    company = _models()["Company"].objects.get(pk=company_id)
    _lock_company(company)
    locked = (
        AssetCodingScheme.objects.select_for_update()
        # previous_version is nullable. PostgreSQL rejects FOR UPDATE on the
        # nullable side of that outer join; the company row already serializes
        # every mutation and the scheme row itself is locked here.
        .select_related("company")
        .get(pk=scheme.pk)
    )
    return locked


def _save(instance, *, update_fields=None):
    instance.full_clean()
    try:
        instance.save(update_fields=update_fields)
    except IntegrityError as exc:
        raise ValidationError("保存失败：编码方案约束与现有数据冲突。") from exc
    return instance


def _coding_mutation(function):
    """Run a mutation atomically and map immediate/deferred DB conflicts."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with transaction.atomic():
                return function(*args, **kwargs)
        except IntegrityError as exc:
            raise ValidationError(
                "保存失败：编码方案被并发修改或违反数据库完整性约束，请刷新后重试。"
            ) from exc

    return wrapped


def _apply(instance, data, allowed_fields):
    unknown = set(data).difference(allowed_fields)
    if unknown:
        raise ValidationError(
            {field: "此字段不允许通过编码方案服务修改。" for field in unknown}
        )
    for field in allowed_fields:
        if field in data:
            setattr(instance, field, data[field])
    return instance


def _ensure_draft_mutable(scheme):
    IssuedCode = _models()["IssuedCode"]
    if scheme.status != "draft":
        raise ValidationError("只有草稿版本可以原地修改；请克隆为新版本。")
    if IssuedCode.objects.filter(coding_scheme=scheme).exists():
        raise ValidationError("已使用版本不可修改；请克隆为新版本。")
    if scheme.next_versions.exists():
        raise ValidationError("已有后续版本的旧版本不可修改；请在最新版本上继续克隆。")


def _closed_intervals_overlap(first_start, first_end, second_start, second_end):
    if first_start is None or second_start is None:
        return False
    return (first_end is None or second_start <= first_end) and (
        second_end is None or first_start <= second_end
    )


def _validate_no_active_overlap(scheme):
    AssetCodingScheme = _models()["AssetCodingScheme"]
    candidates = AssetCodingScheme.objects.select_for_update().filter(
        company=scheme.company,
        scheme_key=scheme.scheme_key,
        status="active",
    ).exclude(pk=scheme.pk)
    for other in candidates:
        if _closed_intervals_overlap(
            scheme.effective_from,
            scheme.effective_to,
            other.effective_from,
            other.effective_to,
        ):
            raise ValidationError(
                {
                    "effective_from": (
                        f"与 {other.name} v{other.version} 的闭区间生效期重叠。"
                    )
                }
            )


def _static_maximum_length(scheme, segments):
    length = 0
    for segment in segments:
        segment_type = segment.segment_type
        if segment_type in {"fixed_text", "custom_text", "separator"}:
            length += _maximum_normalized_length(segment.fixed_value)
        elif segment_type == "sequence":
            length += segment.sequence_length
        elif segment_type == "year":
            length += 4
        elif segment_type == "year_month":
            length += 6
        elif segment_type == "full_date":
            length += 8
        elif segment_type == "company_code":
            length += _maximum_normalized_length(scheme.company.code)
        # Category and department master-data codes vary by issuance context;
        # exact length is rechecked by render_code for every preview/issuance.
    if length > 64:
        raise ValidationError("编码方案已确定片段的最短渲染长度超过 64 个字符。")


def _validate_active_scheme(scheme):
    if scheme.effective_from is None:
        raise ValidationError({"effective_from": "启用方案必须填写生效开始日。"})
    segments = validate_scheme_structure(scheme)
    _validate_no_active_overlap(scheme)
    _validate_all_context_lengths(scheme, segments)


def _maximum_normalized_length(value):
    value = value or ""
    return max(len(value), len(unicodedata.normalize("NFKC", value)))


def _validate_all_context_lengths(scheme, segments):
    """Prove the configured scheme cannot exceed the 64-character ceiling."""

    AssetCategory = _models()["AssetCategory"]
    from apps.masterdata.models import Department

    _static_maximum_length(scheme, segments)
    segment_types = [item.segment_type for item in segments]
    sequence_segment = next(
        item for item in segments if item.segment_type == "sequence"
    )
    if len(str(scheme.sequence_start)) > sequence_segment.sequence_length:
        raise ValidationError({"sequence_start": "流水起始值已超出配置的流水位数。"})
    department_count = segment_types.count("department_code")
    department_length = 0
    if department_count:
        codes = Department.objects.filter(
            company=scheme.company, is_active=True
        ).values_list("code", flat=True)
        department_length = department_count * max(
            (_maximum_normalized_length(value) for value in codes), default=0
        )
    categories = None
    category_types = {
        "major_category_code",
        "minor_category_code",
        "category_code",
    }
    if category_types.intersection(segment_types):
        categories = list(
            AssetCategory.objects.filter(company=scheme.company, is_active=True)
            .order_by("pk")
        )
    category_length = 0
    for category in categories or ():
        path = []
        seen = set()
        current = category
        while current is not None:
            if current.pk in seen:
                raise ValidationError("实物分类路径存在循环，不能启用编码方案。")
            seen.add(current.pk)
            path.append(current)
            current = current.parent
        path.reverse()
        if "major_category_code" in segment_types and not path:
            continue
        if "minor_category_code" in segment_types and len(path) < 2:
            continue
        candidate = (
            segment_types.count("major_category_code")
            * _maximum_normalized_length(path[0].code)
            + segment_types.count("minor_category_code")
            * (
                _maximum_normalized_length(path[1].code)
                if len(path) >= 2
                else 0
            )
            + segment_types.count("category_code")
            * _maximum_normalized_length(category.code)
        )
        category_length = max(category_length, candidate)

    fixed_length = 0
    for segment in segments:
        if segment.segment_type in {"fixed_text", "custom_text", "separator"}:
            fixed_length += _maximum_normalized_length(segment.fixed_value)
        elif segment.segment_type == "sequence":
            fixed_length += segment.sequence_length
        elif segment.segment_type == "year":
            fixed_length += 4
        elif segment.segment_type == "year_month":
            fixed_length += 6
        elif segment.segment_type == "full_date":
            fixed_length += 8
        elif segment.segment_type == "company_code":
            fixed_length += _maximum_normalized_length(scheme.company.code)
    if fixed_length + department_length + category_length > 64:
        raise ValidationError("编码方案对当前主数据的最大渲染长度超过 64 个字符。")


def _currently_valid_default(company):
    AssetCodingScheme = _models()["AssetCodingScheme"]
    today = timezone.localdate()
    candidates = list(
        AssetCodingScheme.objects.filter(
            company=company,
            status="active",
            is_default=True,
            effective_from__lte=today,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .prefetch_related("segments")
    )
    if len(candidates) != 1:
        return None
    try:
        _validate_active_scheme(candidates[0])
    except ValidationError:
        return None
    return candidates[0]


def _refresh_coding_progress(*, company, actor, request=None):
    InitializationSetting = _models()["InitializationSetting"]
    setting, _ = InitializationSetting.objects.select_for_update().get_or_create(
        company=company
    )
    if setting.initialization_completed:
        raise ValidationError("Sprint 2 不得设置或改写整体初始化完成状态。")
    configured = _currently_valid_default(company) is not None
    if setting.coding_scheme_configured == configured:
        return setting
    old_value = setting.coding_scheme_configured
    setting.coding_scheme_configured = configured
    setting.save(update_fields=["coding_scheme_configured"])
    _audit(
        company=company,
        actor=actor,
        action="setup_coding_progress_update",
        instance=setting,
        old_data={
            "coding_scheme_configured": old_value,
            "initialization_completed": False,
        },
        new_data={
            "coding_scheme_configured": configured,
            "initialization_completed": False,
        },
        request=request,
    )
    return setting


def _segment_instances(scheme, definitions):
    AssetCodingSegment = _models()["AssetCodingSegment"]
    instances = []
    for definition in definitions:
        unknown = set(definition).difference(SEGMENT_FIELDS)
        if unknown:
            raise ValidationError(
                {field: "不是 V1 编码片段字段。" for field in unknown}
            )
        values = {field: definition.get(field) for field in SEGMENT_FIELDS}
        instances.append(AssetCodingSegment(coding_scheme=scheme, **values))
    validate_scheme_structure(instances)
    for instance in instances:
        instance.full_clean(validate_unique=False, validate_constraints=False)
    return instances


@_coding_mutation
def create_scheme(*, actor, company, data, segments=None, request=None):
    """Create the first draft version of a new stable scheme key."""

    AssetCodingScheme = _models()["AssetCodingScheme"]
    AssetCodingSegment = _models()["AssetCodingSegment"]
    _require_system_admin(actor)
    company = _lock_company(company)
    data = dict(data)
    unknown = set(data).difference(SCHEME_EDITABLE_FIELDS | {"version"})
    if unknown:
        raise ValidationError(
            {field: "此字段不能在创建编码方案时设置。" for field in unknown}
        )
    if data.pop("version", 1) != 1:
        raise ValidationError({"version": "新编码方案必须从版本 1 开始。"})
    scheme_key = str(data.get("scheme_key", "")).strip()
    if not scheme_key:
        raise ValidationError({"scheme_key": "方案稳定键不能为空。"})
    if AssetCodingScheme.objects.select_for_update().filter(
        company=company, scheme_key=scheme_key
    ).exists():
        raise ValidationError({"scheme_key": "该稳定键已存在；请使用克隆版本。"})
    data["scheme_key"] = scheme_key
    scheme = _apply(
        AssetCodingScheme(
            company=company,
            version=1,
            status="draft",
            is_default=False,
            created_by=actor,
        ),
        data,
        SCHEME_EDITABLE_FIELDS,
    )
    _save(scheme)
    if segments is not None:
        instances = _segment_instances(scheme, list(segments))
        AssetCodingSegment.objects.bulk_create(instances)
    _audit(
        company=company,
        actor=actor,
        action="coding_scheme_create",
        instance=scheme,
        new_data={
            **_snapshot(scheme),
            "segments": [
                _segment_snapshot(item)
                for item in scheme.segments.order_by("sequence_order")
            ],
        },
        request=request,
    )
    return scheme


@_coding_mutation
def update_draft_scheme(*, actor, scheme, data, request=None):
    """Edit rule-level fields on an unused draft version only."""

    _require_system_admin(actor)
    scheme = _lock_scheme(scheme)
    _ensure_draft_mutable(scheme)
    old = _snapshot(scheme)
    _apply(scheme, dict(data), SCHEME_EDITABLE_FIELDS)
    if scheme.previous_version_id and scheme.scheme_key != scheme.previous_version.scheme_key:
        raise ValidationError({"scheme_key": "克隆版本不能改变方案稳定键。"})
    _save(scheme)
    _audit(
        company=scheme.company,
        actor=actor,
        action="coding_scheme_update",
        instance=scheme,
        old_data=old,
        new_data=_snapshot(scheme),
        request=request,
    )
    return scheme


@_coding_mutation
def replace_segments(*, actor, scheme, segments, request=None):
    """Atomically replace every segment on an unused draft version."""

    AssetCodingSegment = _models()["AssetCodingSegment"]
    _require_system_admin(actor)
    scheme = _lock_scheme(scheme)
    _ensure_draft_mutable(scheme)
    old = [
        _segment_snapshot(item)
        for item in scheme.segments.select_for_update().order_by("sequence_order")
    ]
    instances = _segment_instances(scheme, list(segments))
    scheme.segments.all().delete()
    AssetCodingSegment.objects.bulk_create(instances)
    new = [_segment_snapshot(item) for item in instances]
    _audit(
        company=scheme.company,
        actor=actor,
        action="coding_segments_replace",
        instance=scheme,
        old_data={"segments": old},
        new_data={"segments": new},
        request=request,
    )
    return list(scheme.segments.order_by("sequence_order"))


@_coding_mutation
def clone_scheme(*, actor, scheme, data=None, request=None):
    """Clone one concrete version and its segments into a new draft version."""

    AssetCodingScheme = _models()["AssetCodingScheme"]
    AssetCodingSegment = _models()["AssetCodingSegment"]
    _require_system_admin(actor)
    source = _lock_scheme(scheme)
    versions = list(
        AssetCodingScheme.objects.select_for_update()
        .filter(company=source.company, scheme_key=source.scheme_key)
        .order_by("version")
    )
    latest = versions[-1]
    if latest.pk != source.pk:
        raise ValidationError("只能从当前最新版本克隆，以保持版本链连续且不可分叉。")
    overrides = dict(data or {})
    unknown = set(overrides).difference(CLONE_OVERRIDE_FIELDS)
    if unknown:
        raise ValidationError(
            {field: "此字段不能覆盖克隆版本。" for field in unknown}
        )
    values = {
        "name": source.name,
        "description": source.description,
        "reset_mode": source.reset_mode,
        "sequence_start": source.sequence_start,
        "category_scope_level": source.category_scope_level,
        # A clone is not silently scheduled into its source's interval.
        "effective_from": None,
        "effective_to": None,
    }
    values.update(overrides)
    clone = AssetCodingScheme(
        company=source.company,
        scheme_key=source.scheme_key,
        version=source.version + 1,
        status="draft",
        is_default=False,
        previous_version=source,
        created_by=actor,
        **values,
    )
    _save(clone)
    source_segments = list(source.segments.order_by("sequence_order"))
    clones = [
        AssetCodingSegment(
            coding_scheme=clone,
            **{field: getattr(item, field) for field in SEGMENT_FIELDS},
        )
        for item in source_segments
    ]
    if clones:
        validate_scheme_structure(clones)
        AssetCodingSegment.objects.bulk_create(clones)
    _audit(
        company=source.company,
        actor=actor,
        action="coding_scheme_clone",
        instance=clone,
        old_data={"source_scheme_id": str(source.pk), "source_version": source.version},
        new_data={
            **_snapshot(clone),
            "segments": [_segment_snapshot(item) for item in clones],
        },
        request=request,
    )
    return clone


@_coding_mutation
def activate_scheme(*, actor, scheme, request=None):
    """Activate a structurally valid draft without changing another version."""

    _require_system_admin(actor)
    scheme = _lock_scheme(scheme)
    if scheme.status == "active":
        _validate_active_scheme(scheme)
        return scheme
    if scheme.status != "draft":
        raise ValidationError("历史版本不能重新启用；请克隆新版本。")
    old = _snapshot(scheme)
    _validate_active_scheme(scheme)
    scheme.status = "active"
    scheme.is_default = False
    _save(scheme, update_fields=["status", "is_default", "updated_at"])
    _audit(
        company=scheme.company,
        actor=actor,
        action="coding_scheme_activate",
        instance=scheme,
        old_data=old,
        new_data=_snapshot(scheme),
        request=request,
    )
    _refresh_coding_progress(company=scheme.company, actor=actor, request=request)
    return scheme


@_coding_mutation
def retire_scheme(*, actor, scheme, request=None):
    """Move an active version to its terminal retired state."""

    _require_system_admin(actor)
    scheme = _lock_scheme(scheme)
    if scheme.status == "retired":
        return scheme
    if scheme.status != "active":
        raise ValidationError("只有有效版本可以停用；未使用草稿保持草稿状态。")
    old = _snapshot(scheme)
    scheme.status = "retired"
    scheme.is_default = False
    _save(scheme, update_fields=["status", "is_default", "updated_at"])
    _audit(
        company=scheme.company,
        actor=actor,
        action="coding_scheme_retire",
        instance=scheme,
        old_data=old,
        new_data=_snapshot(scheme),
        request=request,
    )
    _refresh_coding_progress(company=scheme.company, actor=actor, request=request)
    return scheme


@_coding_mutation
def set_default_scheme(*, actor, scheme, request=None):
    """Atomically switch the company's sole active default version."""

    AssetCodingScheme = _models()["AssetCodingScheme"]
    _require_system_admin(actor)
    scheme = _lock_scheme(scheme)
    today = timezone.localdate()
    if not is_effective(scheme, today):
        raise ValidationError("公司默认方案必须是当前生效的有效版本。")
    _validate_active_scheme(scheme)
    all_schemes = list(
        AssetCodingScheme.objects.select_for_update()
        .filter(company=scheme.company, is_default=True)
        .order_by("pk")
    )
    changed = [item for item in all_schemes if item.pk != scheme.pk]
    for item in changed:
        old = _snapshot(item)
        item.is_default = False
        _save(item, update_fields=["is_default", "updated_at"])
        _audit(
            company=scheme.company,
            actor=actor,
            action="coding_scheme_default_unset",
            instance=item,
            old_data=old,
            new_data=_snapshot(item),
            request=request,
        )
    if not scheme.is_default:
        old = _snapshot(scheme)
        scheme.is_default = True
        _save(scheme, update_fields=["is_default", "updated_at"])
        _audit(
            company=scheme.company,
            actor=actor,
            action="coding_scheme_default_set",
            instance=scheme,
            old_data=old,
            new_data=_snapshot(scheme),
            request=request,
        )
    _refresh_coding_progress(company=scheme.company, actor=actor, request=request)
    return scheme


@_coding_mutation
def set_category_default_scheme(*, actor, category, scheme, request=None):
    """Bind or clear one category's same-company, currently effective version."""

    AssetCategory = _models()["AssetCategory"]
    AssetCodingScheme = _models()["AssetCodingScheme"]
    _require_system_admin(actor)
    try:
        company_id = AssetCategory.objects.values_list("company_id", flat=True).get(
            pk=category.pk
        )
    except AssetCategory.DoesNotExist as exc:
        raise ValidationError("实物分类不存在。") from exc
    company = _models()["Company"].objects.get(pk=company_id)
    _lock_company(company)
    try:
        category = (
            AssetCategory.objects.select_for_update()
            # default_coding_scheme is nullable; locking an outer-joined row is
            # unsupported by PostgreSQL. Lock the category itself and load the
            # candidate scheme separately below.
            .select_related("company")
            .get(pk=category.pk)
        )
    except AssetCategory.DoesNotExist as exc:
        raise ValidationError("实物分类不存在。") from exc
    if scheme is not None:
        try:
            scheme = AssetCodingScheme.objects.select_for_update().get(pk=scheme.pk)
        except AssetCodingScheme.DoesNotExist as exc:
            raise ValidationError("编码方案不存在。") from exc
        if scheme.company_id != category.company_id:
            raise ValidationError({"scheme": "默认编码方案必须属于同一公司。"})
        if not is_effective(scheme, timezone.localdate()):
            raise ValidationError({"scheme": "只能绑定当前生效的有效版本。"})
        _validate_active_scheme(scheme)
    old_scheme_id = category.default_coding_scheme_id
    if old_scheme_id == (scheme.pk if scheme else None):
        return category
    category.default_coding_scheme = scheme
    _save(category, update_fields=["default_coding_scheme", "updated_at"])
    _audit(
        company=category.company,
        actor=actor,
        action="category_default_coding_scheme_set",
        instance=category,
        old_data={"default_coding_scheme_id": _serializable(old_scheme_id)},
        new_data={
            "default_coding_scheme_id": _serializable(scheme.pk if scheme else None)
        },
        request=request,
    )
    return category


__all__ = [
    "activate_scheme",
    "clone_scheme",
    "create_scheme",
    "replace_segments",
    "retire_scheme",
    "set_category_default_scheme",
    "set_default_scheme",
    "update_draft_scheme",
]
