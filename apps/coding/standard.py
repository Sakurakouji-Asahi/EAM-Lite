"""The user's AA-CC-YYYY-NNNNNN-SS identity standard, independent of finance."""
from __future__ import annotations

import json
import re
from datetime import date

from django.core.exceptions import ValidationError

STANDARD_KEY = "asset_identity_v1"
SCOPE_PREFIX = '{"standard":"asset_identity_v1",'
MANAGEMENT_CHOICES = (
    ("FA", "FA · 固定资产实物管理"), ("LV", "LV · 逐件低值耐用品"),
    ("IA", "IA · 无形资产"), ("LS", "LS · 租入或受托资产"), ("OT", "OT · 其他受控资产"),
)
MANAGEMENT_CODES = frozenset(code for code, _ in MANAGEMENT_CHOICES)
CATEGORY_DEFAULTS = (
    ("01", "房屋及建筑物"), ("02", "机器设备"),
    ("03", "运输设备"), ("04", "电子及信息设备"),
    ("05", "办公家具"), ("06", "仪器仪表"),
    ("07", "工器具"), ("08", "安防消防设备"),
    ("09", "文化及陈列资产"), ("99", "其他"),
)
STANDARD_SEGMENT_TYPES = (
    "management_attribute", "separator", "major_category_code", "separator",
    "coding_year", "separator", "sequence", "separator", "subitem_number",
)
STANDARD_SOURCE_TYPES = frozenset({"management_attribute", "coding_year", "subitem_number"})


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def standard_segments():
    return [{
        "sequence_order": order, "segment_type": kind,
        "fixed_value": "-" if kind == "separator" else None, "format_string": None,
        "sequence_length": 6 if kind == "sequence" else None,
        "zero_pad": True if kind == "sequence" else None,
    } for order, kind in enumerate(STANDARD_SEGMENT_TYPES, 1)]


def is_standard_segments(segments):
    return any(_get(segment, "segment_type") in STANDARD_SOURCE_TYPES for segment in segments)


def validate_standard_structure(scheme, segments):
    if not is_standard_segments(segments):
        return False
    if tuple(_get(item, "segment_type") for item in segments) != STANDARD_SEGMENT_TYPES:
        raise ValidationError({"segments": "统一编码必须采用属性-大类-取得年份-六位流水-子项号的完整结构。"})
    for item in segments:
        kind = _get(item, "segment_type")
        if kind == "separator" and _get(item, "fixed_value") != "-":
            raise ValidationError({"segments": "统一编码的分隔符固定为连字符。"})
        if kind == "sequence" and (_get(item, "sequence_length") != 6 or _get(item, "zero_pad") is not True):
            raise ValidationError({"segments": "统一编码使用六位补零流水。"})
    if not isinstance(scheme, (list, tuple)) and (
        _get(scheme, "sequence_start") != 1 or _get(scheme, "reset_mode") != "category_yearly"
        or _get(scheme, "category_scope_level") != "major"
    ):
        raise ValidationError({"reset_mode": "统一编码从 1 起，按管理属性、大类及取得年份分别计数。"})
    return True


def validate_parts(attribute, category_code, coding_year, subitem=0):
    errors = {}
    if attribute not in MANAGEMENT_CODES:
        errors["management_attribute"] = "请选择首次建档管理属性；该属性不代替财务认定。"
    if not isinstance(category_code, str) or re.fullmatch(r"(?:0[1-9]|[1-9][0-9])", category_code) is None:
        errors["category"] = "统一编码要求一级实物分类编码为 01—99 的两位数字。"
    if isinstance(coding_year, bool) or not isinstance(coding_year, int) or not 1000 <= coding_year <= 9999:
        errors["coding_year"] = "取得年份必须为四位年份。"
    if isinstance(subitem, bool) or not isinstance(subitem, int) or not 0 <= subitem <= 99:
        errors["component_of"] = "子项号范围为 00—99。"
    if errors:
        raise ValidationError(errors)


def resolve_year(*, requested_year, acquisition_date, note, today, require_evidence=True):
    if not isinstance(today, date):
        raise ValidationError("缺少本次纳入管理日期。")
    if requested_year is not None:
        year, source, explanation = requested_year, "specified", str(note or "").strip()
        if (acquisition_date is None or year != acquisition_date.year) and not explanation and require_evidence:
            raise ValidationError({"coding_year_note": "单独指定取得年份时，请说明验收、合同、历史资料等依据。"})
        if not explanation:
            if acquisition_date is not None and year == acquisition_date.year:
                source, explanation = "acquisition_date", "按已填写的购置日期确定取得年份。"
            else:
                explanation = "编号格式预览；年份依据在保存时核对。"
    elif acquisition_date is not None:
        year, source = acquisition_date.year, "acquisition_date"
        explanation = "按已填写的购置日期确定取得年份。"
    else:
        year, source = today.year, "inclusion_year"
        explanation = "取得日期未填写，按本次纳入管理年份编码。"
    if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= today.year:
        raise ValidationError({"coding_year": "取得年份必须是四位年份，且不能晚于当前年份。"})
    return year, source, explanation


def identity_scope(company_id, attribute, category_code, coding_year, *, subitem=0, parent_id=None):
    validate_parts(attribute, category_code, coding_year, subitem)
    values = {"standard": STANDARD_KEY, "company_id": str(company_id), "attribute": attribute,
              "category_code": category_code, "year": coding_year}
    if subitem:
        if parent_id is None:
            raise ValidationError("组件编码必须关联主资产。")
        values.update({"parent_id": str(parent_id), "subitem": subitem})
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))
