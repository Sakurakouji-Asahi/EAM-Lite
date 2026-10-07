"""Readable projections of saved theoretical runs; never recalculates a schedule."""
from decimal import Decimal
from urllib.parse import urlencode, urlsplit

from django.urls import reverse

from apps.audit.display import localize_audit_payload
from .models import DepreciationMethod, PostingPeriod, SalvageMode, StartRule


PARAMETER_LABELS = {
    "original_cost": "原值", "method": "试算方法", "posting_period": "计提周期",
    "commissioning_date": "投入使用日期", "start_rule": "起算规则",
    "specified_start": "指定起算日", "useful_life_months": "使用寿命（月）",
    "salvage_mode": "残值方式", "salvage_rate": "残值率（小数）",
    "salvage_amount": "残值金额", "annual_posting_month": "年度计提月",
    "opening_actual_accumulated_depreciation": "期初累计折旧",
    "opening_impairment": "期初减值", "opening_book_value": "期初账面净值",
    "actual_continuation_date": "实际接续日", "stop_date": "停止计提日（不含当天）",
    "suspensions": "暂停期间（结束日期不含当天）",
}
CHOICES = {"method": dict(DepreciationMethod.choices), "posting_period": dict(PostingPeriod.choices),
           "salvage_mode": dict(SalvageMode.choices), "start_rule": dict(StartRule.choices)}


def _display_parameter(key, value):
    if value is None or value == "" or value == []:
        return "未设置"
    if key in CHOICES:
        return CHOICES[key].get(value, value)
    if key == "suspensions" and isinstance(value, list):
        return "；".join(" 至 ".join(map(str, interval)) for interval in value)
    return localize_audit_payload({key: value}).popitem()[1]


def parameter_rows(run):
    return [{"key": key, "label": PARAMETER_LABELS.get(key, key),
             "value": _display_parameter(key, value)}
            for key, value in run.parameter_snapshot_json.items() if key != "digest"]


def parameter_comparison(a, b):
    left, right = a.parameter_snapshot_json, b.parameter_snapshot_json
    return [{"label": PARAMETER_LABELS.get(key, key),
             "a": _display_parameter(key, left.get(key)), "b": _display_parameter(key, right.get(key)),
             "changed": left.get(key) != right.get(key)}
            for key in dict.fromkeys((*left, *right)) if key != "digest"]


def saved_result(run, lines=None):
    lines = list(run.lines.all()) if lines is None else lines
    completed = run.status == "completed"
    last = max(lines, key=lambda line: (line.period_end, line.period_start)) if lines and completed else None
    return {"completed": completed, "period_count": len(lines),
            "amount": sum((line.theoretical_amount for line in lines), Decimal("0.00")) if completed else None,
            "through": last.period_end_inclusive if last else None,
            "accumulated": last.theoretical_accumulated if last else None,
            "book_value": last.theoretical_book_value if last else None}


def result_comparison(a, b):
    left, right = saved_result(a), saved_result(b)
    rows = [{"label": "记录状态", "a": a.get_status_display(), "b": b.get_status_display(), "changed": a.status != b.status},
            {"label": "试算截止日期", "a": a.as_of_date.isoformat(), "b": b.as_of_date.isoformat(), "changed": a.as_of_date != b.as_of_date}]
    for key, label in (("period_count", "已保存期间数"), ("through", "末期截至日期"),
                       ("amount", "本次试算折旧合计"), ("accumulated", "末期理论累计折旧"), ("book_value", "末期理论净值")):
        values = []
        for result in (left, right):
            value = result[key]
            if value is None:
                value = "无完整结果" if not result["completed"] else "无期间记录"
            elif isinstance(value, Decimal):
                value = format(value, ".2f")
            elif key == "through":
                value = value.isoformat()
            values.append(value)
        rows.append({"label": label, "a": values[0], "b": values[1], "changed": left[key] != right[key]})
    return rows


def history_row_links(request, asset, runs):
    origin = request.get_full_path()
    for run in runs:
        run.ui_detail_url = reverse("finance:theoretical-detail", args=[asset.pk, run.pk]) + "?" + urlencode({"return_to": origin})
        for side in ("a", "b"):
            query = request.GET.copy()
            query.pop("page", None)
            query["compare_" + side] = str(run.pk)
            setattr(run, "ui_compare_" + side, request.path + "?" + query.urlencode() + "#theory-comparison")


def detail_context(request, asset, run):
    fallback = reverse("finance:theoretical-history", args=[asset.pk])
    target = request.GET.get("return_to", "")
    return_url = fallback
    if target and len(target) <= 6000 and "\\" not in target and not any(ord(char) < 32 for char in target):
        try:
            parts = urlsplit(target)
            if not parts.scheme and not parts.netloc and not parts.fragment and parts.path == fallback and not target.startswith("//"):
                return_url = target
        except ValueError:
            pass
    lines = list(run.lines.order_by("period_start", "period_end"))
    return {"lines": lines, "theory_result": saved_result(run, lines),
            "theory_parameters": parameter_rows(run), "theory_return_url": return_url}
