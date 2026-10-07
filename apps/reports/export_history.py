"""Read-only presentation of historical export files and list navigation."""

from datetime import timedelta

from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from .models import ExportLog
from .permissions import can_download_export


HISTORY_QUERY_FIELDS = frozenset({"q", "report_type", "status", "date_from", "date_to", "mine", "page"})


def history_date_ranges(today):
    month_start = today.replace(day=1)
    previous_end = month_start - timedelta(days=1)
    return (("今天", today, today), ("近 7 天", today - timedelta(days=6), today),
        ("本月", month_start, today), ("上月", previous_end.replace(day=1), previous_end))


def history_search_context(form, *, today=None):
    """Use valid applied fields; date shortcuts can also repair an invalid date range."""
    valid = form.is_valid()
    data = form.cleaned_data
    query = QueryDict(mutable=True)
    for name in HISTORY_QUERY_FIELDS - {"page"}:
        value = data.get(name)
        if value not in (None, "", False):
            query[name] = value.isoformat() if name in {"date_from", "date_to"} else "on" if name == "mine" else str(value)

    def url(params):
        return reverse("reports:export-history") + ("?" + params.urlencode() if params else "")

    shortcuts = []
    for label, start, end in history_date_ranges(today or timezone.localdate()):
        dates = query.copy()
        dates["date_from"], dates["date_to"] = start.isoformat(), end.isoformat()
        shortcuts.append({"label":label, "url":url(dates), "start":start, "end":end,
            "selected":valid and data.get("date_from") == start and data.get("date_to") == end})

    chips = []
    if valid:
        for name in ("q", "report_type", "date_from", "status", "mine"):
            value = data.get(name)
            if name == "date_from":
                start, end = data.get("date_from"), data.get("date_to")
                if not (start or end):
                    continue
                label = "请求日期"
                display = (f"{start.isoformat()} 至 {end.isoformat()}" if start and end
                    else f"{start.isoformat()}起" if start else f"截至{end.isoformat()}")
                removed = {"date_from", "date_to"}
            elif value not in (None, "", False):
                label = form.fields[name].label
                display = "是" if name == "mine" else dict(form.fields[name].choices).get(value, value) if name in {"status", "report_type"} else value
                removed = {name}
            else:
                continue
            remaining = query.copy()
            for key in removed:
                remaining.pop(key, None)
            chips.append({"label":label, "value":display, "url":url(remaining)})
    return {"history_date_shortcuts":shortcuts, "history_filter_chips":chips}


def export_file_context(actor, export_log):
    """Describe published metadata; the download service still checks access."""
    attachment = export_log.output_attachment
    result = {"can_download": False, "filename": attachment.safe_filename if attachment else ""}
    if export_log.status == ExportLog.Status.PENDING:
        result.update(file_note="文件正在生成", file_tone="info")
    elif export_log.status == ExportLog.Status.FAILED:
        result.update(file_note=export_log.error_summary or "文件生成失败", file_tone="danger")
    elif export_log.status == ExportLog.Status.EXPIRED:
        result.update(file_note="文件已过期，请重新查询后导出", file_tone="warning")
    elif not (attachment and attachment.is_available and attachment.company_id == export_log.company_id
              and attachment.sha256 == export_log.output_sha256):
        result.update(file_note="文件暂不可用，请联系管理员核对", file_tone="warning")
    elif can_download_export(actor, export_log):
        result.update(can_download=True, file_note="可下载 Excel", file_tone="success")
    else:
        result.update(file_note="当前权限仅可查看记录", file_tone="secondary")
    return result


def export_history_return_url(request):
    """Always return to this list, preserving only its supported parameters."""
    fallback = reverse("reports:export-history")
    raw = request.GET.get("history_query", "")
    if not raw or len(raw) > 3000:
        return fallback
    params = QueryDict(raw)
    query = QueryDict(mutable=True)
    for key in HISTORY_QUERY_FIELDS:
        if params.get(key):
            query[key] = params[key]
    return fallback + ("?" + query.urlencode() if query else "")
