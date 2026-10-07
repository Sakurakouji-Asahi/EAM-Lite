"""Exact multi-code lookup shared by the ledger and its existing export."""
import re

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db.models.functions import Lower


MAX_CODES = 50
MAX_INPUT_LENGTH = 5000


def normalize_lookup_codes(value):
    if len(value) > MAX_INPUT_LENGTH:
        raise ValidationError({"codes": "批量编号总长度不能超过 5000 个字符。"})
    codes, seen = [], set()
    for part in re.split(r"[\r\n\t,，;；]+", value):
        code = part.strip()
        if not code:
            continue
        if len(code) > 200 or any(ord(character) < 32 for character in code):
            raise ValidationError({"codes": "每个编号不得超过 200 字符，且不能包含不可见控制字符。"})
        key = code.lower()
        if key not in seen:
            seen.add(key)
            codes.append(code)
    if len(codes) > MAX_CODES:
        raise ValidationError({"codes": f"一次最多查询 {MAX_CODES} 个不同编号，请分批查询。"})
    return "\n".join(codes)


def filter_lookup_codes(queryset, value, *, include_equipment):
    keys = [code.lower() for code in value.splitlines()]
    queryset = queryset.annotate(_lookup_code=Lower("asset_code"))
    match = Q(_lookup_code__in=keys)
    if include_equipment:
        queryset = queryset.annotate(_lookup_equipment=Lower("equipment_number"))
        match |= Q(_lookup_equipment__in=keys)
    return queryset.filter(match)


def code_lookup_summary(queryset, value, *, include_equipment):
    if not value:
        return None
    codes = value.splitlines()
    matched = {code.lower(): set() for code in codes}
    fields = ["pk", "asset_code"] + (["equipment_number"] if include_equipment else [])
    for row in queryset.values_list(*fields):
        for identifier in row[1:]:
            key = str(identifier or "").lower()
            if key in matched:
                matched[key].add(row[0])
    missing = [code for code in codes if not matched[code.lower()]]
    multiple = [{"code": code, "count": len(matched[code.lower()])} for code in codes if len(matched[code.lower()]) > 1]
    return {"total": len(codes), "matched": len(codes) - len(missing), "missing": missing, "multiple": multiple}
