"""Pure functions for the configurable asset-code domain.

Nothing in this module allocates an official number or mutates a counter.  A
caller that wants a preview must pass the counter value it has read (if any);
the functions below only simulate subsequent values in memory.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_CODE_LENGTH = 64
MAX_PREVIEW_COUNT = 10
SEPARATORS = frozenset({"-", "_", ".", "/"})
RESET_MODES = frozenset(
    {"never", "yearly", "monthly", "category_yearly", "category_monthly"}
)
CATEGORY_SCOPE_LEVELS = frozenset({"major", "minor", "leaf"})
SEGMENT_TYPES = frozenset(
    {
        "fixed_text",
        "company_code",
        "major_category_code",
        "minor_category_code",
        "category_code",
        "department_code",
        "year",
        "year_month",
        "full_date",
        "sequence",
        "custom_text",
        "separator",
    }
)


def _get(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _business_date(value, *, field_name="effective_date") -> date:
    if value is None:
        raise ValidationError({field_name: "必须显式提供上海业务日期。"})
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            raise ValidationError({field_name: "日期时间必须包含时区。"})
        return value.astimezone(SHANGHAI).date()
    if not isinstance(value, date):
        raise ValidationError({field_name: "必须是有效日期。"})
    return value


def _identifier(value, *, field_name):
    if value is None or str(value).strip() == "":
        raise ValidationError({field_name: "缺少必需的稳定标识。"})
    return str(value)


def _contains_forbidden_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _validate_constant(value, *, field_name):
    if not isinstance(value, str) or not value:
        raise ValidationError({field_name: "必须填写非空固定文本。"})
    if value != value.strip():
        raise ValidationError({field_name: "不得包含首尾空白。"})
    if "{" in value or "}" in value:
        raise ValidationError({field_name: "不得包含模板花括号。"})
    if _contains_forbidden_character(value):
        raise ValidationError({field_name: "不得包含控制字符或不可见字符。"})


def validate_segment_fields(
    segment_type,
    *,
    fixed_value=None,
    format_string=None,
    sequence_length=None,
    zero_pad=None,
):
    """Validate and return the approved V1 segment-field combination."""

    if segment_type not in SEGMENT_TYPES:
        raise ValidationError({"segment_type": "不是 V1 支持的编码片段类型。"})
    if format_string is not None:
        raise ValidationError({"format_string": "V1 保留字段必须为 NULL。"})

    if segment_type in {"fixed_text", "custom_text"}:
        _validate_constant(fixed_value, field_name="fixed_value")
        if sequence_length is not None or zero_pad is not None:
            raise ValidationError("固定文本片段不能配置流水字段。")
    elif segment_type == "separator":
        if fixed_value not in SEPARATORS:
            raise ValidationError(
                {"fixed_value": "分隔符必须是单个 -、_、. 或 /。"}
            )
        if sequence_length is not None or zero_pad is not None:
            raise ValidationError("分隔符片段不能配置流水字段。")
    elif segment_type == "sequence":
        if fixed_value is not None:
            raise ValidationError({"fixed_value": "流水片段固定值必须为 NULL。"})
        if (
            isinstance(sequence_length, bool)
            or not isinstance(sequence_length, int)
            or not 1 <= sequence_length <= 12
        ):
            raise ValidationError({"sequence_length": "流水位数必须为 1–12。"})
        if not isinstance(zero_pad, bool):
            raise ValidationError({"zero_pad": "流水片段必须明确选择是否补零。"})
    else:
        if fixed_value is not None:
            raise ValidationError({"fixed_value": "来源片段固定值必须为 NULL。"})
        if sequence_length is not None or zero_pad is not None:
            raise ValidationError("来源片段不能配置流水字段。")

    return {
        "segment_type": segment_type,
        "fixed_value": fixed_value,
        "format_string": format_string,
        "sequence_length": sequence_length,
        "zero_pad": zero_pad,
    }


def _related_segments(scheme):
    if isinstance(scheme, (list, tuple)):
        return list(scheme)
    if isinstance(scheme, Mapping) and "segments" in scheme:
        return list(scheme["segments"])
    for name in ("segments", "coding_segments", "assetcodingsegment_set"):
        related = getattr(scheme, name, None)
        if related is not None:
            return list(related.all() if hasattr(related, "all") else related)
    raise ValidationError("未提供编码片段。")


def validate_scheme_structure(segments_or_scheme):
    """Validate ordering, segment matrix and scheme-level reset settings."""

    segments = _related_segments(segments_or_scheme)
    if not segments:
        raise ValidationError({"segments": "编码方案至少需要一个片段。"})
    ordered = sorted(segments, key=lambda item: _get(item, "sequence_order", 0))
    orders = [_get(item, "sequence_order") for item in ordered]
    if orders != list(range(1, len(ordered) + 1)):
        raise ValidationError({"sequence_order": "片段顺序必须从 1 连续且唯一。"})

    sequence_count = 0
    for segment in ordered:
        segment_type = _get(segment, "segment_type")
        validate_segment_fields(
            segment_type,
            fixed_value=_get(segment, "fixed_value"),
            format_string=_get(segment, "format_string"),
            sequence_length=_get(segment, "sequence_length"),
            zero_pad=_get(segment, "zero_pad"),
        )
        sequence_count += segment_type == "sequence"
    if sequence_count != 1:
        raise ValidationError({"segments": "编码方案必须且只能包含一个流水片段。"})

    if not isinstance(segments_or_scheme, (list, tuple)):
        status = _get(segments_or_scheme, "status")
        if status is not None and status not in {"draft", "active", "retired"}:
            raise ValidationError({"status": "编码方案状态无效。"})
        reset_mode = _get(segments_or_scheme, "reset_mode")
        if reset_mode not in RESET_MODES:
            raise ValidationError({"reset_mode": "不是 V1 支持的流水重置模式。"})
        category_level = _get(segments_or_scheme, "category_scope_level")
        if reset_mode.startswith("category_"):
            if category_level not in CATEGORY_SCOPE_LEVELS:
                raise ValidationError(
                    {"category_scope_level": "分类重置必须选择大类、小类或叶级。"}
                )
        elif category_level is not None:
            raise ValidationError(
                {"category_scope_level": "非分类重置模式必须为 NULL。"}
            )
        sequence_start = _get(segments_or_scheme, "sequence_start")
        if (
            isinstance(sequence_start, bool)
            or not isinstance(sequence_start, int)
            or sequence_start < 0
        ):
            raise ValidationError({"sequence_start": "流水起始值必须为非负整数。"})
        if _get(segments_or_scheme, "status") == "active" and _get(
            segments_or_scheme, "effective_from"
        ) is None:
            raise ValidationError({"effective_from": "启用方案必须填写生效开始日。"})
        effective_from = _get(segments_or_scheme, "effective_from")
        effective_to = _get(segments_or_scheme, "effective_to")
        if (
            effective_from is not None
            and effective_to is not None
            and effective_to < effective_from
        ):
            raise ValidationError({"effective_to": "生效结束日不得早于开始日。"})
        if _get(segments_or_scheme, "is_default", False) and status != "active":
            raise ValidationError({"is_default": "只有有效版本可以成为默认方案。"})
    return ordered


def _category_path(category):
    path = []
    seen = set()
    current = category
    while current is not None:
        marker = _get(current, "pk", _get(current, "id", id(current)))
        if marker in seen:
            raise ValidationError({"category": "实物分类路径存在循环。"})
        seen.add(marker)
        path.append(current)
        current = _get(current, "parent")
    return list(reversed(path))


def _category_at_level(category, level):
    path = _category_path(category)
    if level == "major":
        index = 0
    elif level == "minor":
        index = 1
    elif level == "leaf":
        return path[-1]
    else:
        raise ValidationError({"category_scope_level": "分类作用域层级无效。"})
    if len(path) <= index:
        raise ValidationError({"category": "分类路径缺少编码方案要求的层级。"})
    return path[index]


def _context_value(context, *names):
    for name in names:
        value = _get(context, name)
        if value is not None:
            return value
    return None


def _source_code(context, source, *, label):
    direct = _context_value(context, f"{source}_code")
    obj = _context_value(context, source)
    value = direct if direct is not None else _get(obj, "code")
    if value is None or str(value) == "":
        raise ValidationError({source: f"缺少{label}编码。"})
    if obj is not None and _get(obj, "is_active", True) is False:
        raise ValidationError({source: f"{label}已停用，不能用于正式编码。"})
    return str(value)


def _validate_context_company(context, *objects):
    company = _context_value(context, "company")
    company_id = _get(company, "pk", _get(company, "id"))
    for field, obj in objects:
        object_company_id = _get(obj, "company_id")
        if (
            company_id is not None
            and object_company_id is not None
            and str(company_id) != str(object_company_id)
        ):
            raise ValidationError({field: "编码来源不属于当前公司。"})


def _validate_rendered_code(value):
    if len(value) > MAX_CODE_LENGTH:
        raise ValidationError(f"资产编号最长为 {MAX_CODE_LENGTH} 个字符。")
    normalized = unicodedata.normalize("NFKC", value)
    if len(normalized) > MAX_CODE_LENGTH:
        raise ValidationError(
            f"资产编号规范化后最长为 {MAX_CODE_LENGTH} 个字符。"
        )
    if not normalized.strip():
        raise ValidationError("资产编号规范化后不得为空。")
    if value != value.strip():
        raise ValidationError("资产编号不得包含首尾空白。")
    if "{" in value or "}" in value:
        raise ValidationError("资产编号不得包含模板花括号。")
    if _contains_forbidden_character(value):
        raise ValidationError("资产编号不得包含控制字符或不可见字符。")


def normalize_code(value):
    """Return the permanent, case-insensitive uniqueness representation."""

    if not isinstance(value, str):
        raise ValidationError("资产编号必须是字符串。")
    _validate_rendered_code(value)
    return unicodedata.normalize("NFKC", value).strip().casefold()


def render_code(segments, context, sequence_value):
    """Render one display code using the same validation as formal issuance."""

    ordered = validate_scheme_structure(list(segments))
    if (
        isinstance(sequence_value, bool)
        or not isinstance(sequence_value, int)
        or sequence_value < 0
    ):
        raise ValidationError({"sequence_value": "流水值必须为非负整数。"})

    effective_date = _business_date(_context_value(context, "effective_date"))
    category = _context_value(context, "category", "asset_category")
    department = _context_value(context, "department")
    _validate_context_company(
        context, ("category", category), ("department", department)
    )
    parts = []
    for segment in ordered:
        segment_type = _get(segment, "segment_type")
        if segment_type in {"fixed_text", "custom_text", "separator"}:
            part = _get(segment, "fixed_value")
        elif segment_type == "company_code":
            part = _source_code(context, "company", label="公司")
        elif segment_type == "department_code":
            part = _source_code(context, "department", label="部门")
        elif segment_type in {
            "major_category_code",
            "minor_category_code",
            "category_code",
        }:
            if category is None:
                raise ValidationError({"category": "缺少实物分类。"})
            if _get(category, "is_active", True) is False:
                raise ValidationError({"category": "实物分类已停用，不能用于编码。"})
            level = {
                "major_category_code": "major",
                "minor_category_code": "minor",
                "category_code": "leaf",
            }[segment_type]
            part = _get(_category_at_level(category, level), "code")
            if part is None or str(part) == "":
                raise ValidationError({"category": "实物分类缺少编码。"})
        elif segment_type == "year":
            part = f"{effective_date.year:04d}"
        elif segment_type == "year_month":
            part = f"{effective_date.year:04d}{effective_date.month:02d}"
        elif segment_type == "full_date":
            part = effective_date.strftime("%Y%m%d")
        elif segment_type == "sequence":
            length = _get(segment, "sequence_length")
            raw = str(sequence_value)
            if len(raw) > length:
                raise ValidationError({"sequence_value": "流水号已溢出。"})
            part = raw.zfill(length) if _get(segment, "zero_pad") else raw
        else:  # Defensive; validate_segment_fields normally catches this first.
            raise ValidationError({"segment_type": "不支持的编码片段类型。"})
        parts.append(str(part))

    display_code = "".join(parts)
    _validate_rendered_code(display_code)
    return display_code


def build_scope_key(
    company_id,
    scheme_id,
    reset_mode,
    effective_date,
    category=None,
    category_id=None,
    category_scope_level=None,
    level=None,
):
    """Build the stable, compact canonical JSON key for a counter scope."""

    if reset_mode not in RESET_MODES:
        raise ValidationError({"reset_mode": "不是 V1 支持的流水重置模式。"})
    business_date = _business_date(effective_date)
    scope = {
        "company_id": _identifier(company_id, field_name="company_id"),
        "scheme_id": _identifier(scheme_id, field_name="scheme_id"),
        "reset": reset_mode,
    }
    if reset_mode in {"yearly", "monthly", "category_yearly", "category_monthly"}:
        scope["year"] = business_date.year
    if reset_mode in {"monthly", "category_monthly"}:
        scope["month"] = business_date.month
    if reset_mode.startswith("category_"):
        category_level = category_scope_level or level
        if category_level not in CATEGORY_SCOPE_LEVELS:
            raise ValidationError(
                {"category_scope_level": "分类重置必须选择大类、小类或叶级。"}
            )
        resolved = _category_at_level(category, category_level) if category else None
        resolved_id = _get(resolved, "pk", _get(resolved, "id"))
        resolved_id = resolved_id if resolved_id is not None else category_id
        scope["category_level"] = category_level
        scope["category_id"] = _identifier(
            resolved_id, field_name="category_id"
        )
    elif category is not None or category_id is not None or category_scope_level or level:
        raise ValidationError("非分类重置模式不能带分类作用域。")
    return json.dumps(scope, ensure_ascii=True, separators=(",", ":"))


def is_effective(scheme, effective_date):
    """Return whether an active version covers the given Shanghai date."""

    target = _business_date(effective_date)
    if _get(scheme, "status") != "active":
        return False
    effective_from = _get(scheme, "effective_from")
    effective_to = _get(scheme, "effective_to")
    if effective_from is None:
        return False
    return effective_from <= target and (effective_to is None or target <= effective_to)


def preview_codes(scheme, context, count=1, current_value=None):
    """Return consecutive in-memory examples without consuming any counter."""

    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
        raise ValidationError({"count": f"预览数量必须为 1–{MAX_PREVIEW_COUNT}。"})
    segments = validate_scheme_structure(scheme)
    _business_date(_context_value(context, "effective_date"))
    if current_value is None:
        first = _get(scheme, "sequence_start")
    else:
        if (
            isinstance(current_value, bool)
            or not isinstance(current_value, int)
            or current_value < -1
        ):
            raise ValidationError({"current_value": "计数器快照必须是有效整数。"})
        sequence_start = _get(scheme, "sequence_start")
        if isinstance(sequence_start, int) and current_value < sequence_start - 1:
            raise ValidationError(
                {"current_value": "计数器快照不得小于方案起始值减 1。"}
            )
        first = current_value + 1
    if isinstance(first, bool) or not isinstance(first, int) or first < 0:
        raise ValidationError({"sequence_start": "流水起始值必须为非负整数。"})
    return [render_code(segments, context, first + offset) for offset in range(count)]


__all__ = [
    "build_scope_key",
    "is_effective",
    "normalize_code",
    "preview_codes",
    "render_code",
    "validate_scheme_structure",
    "validate_segment_fields",
]
