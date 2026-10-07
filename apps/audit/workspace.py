"""Read-only navigation and summaries over already-redacted audit projections."""
from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from apps.audit.display import audit_action_label


def _url(values):
    query = urlencode({key: value for key, value in values.items() if value not in (None, "")})
    return reverse("audit:log-list") + (f"?{query}" if query else "")


def history_workspace(form, *, now=None):
    values = dict(form.data.items())
    values.pop("page", None)
    end = timezone.localtime(now or timezone.now())
    end = end.replace(microsecond=end.microsecond // 1000 * 1000)
    starts = (
        ("今日", end.replace(hour=0, minute=0, second=0, microsecond=0)),
        ("最近 7 天", end - timedelta(days=7)),
        ("本月", end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
    )
    shortcuts = [{"label": label, "url": _url({**values,
        "start_at": start.isoformat(), "end_at": end.isoformat()})} for label, start in starts]
    labels = {"q": "关键词", "actor": "操作者", "action": "动作", "object_type": "对象类型",
        "object_id": "对象编号", "correlation_id": "同次操作"}
    chips = []
    for key, label in labels.items():
        value = form.cleaned_data.get(key)
        if value in (None, ""):
            continue
        if key == "actor":
            display = value.display_name or value.username
        elif key == "action":
            display = audit_action_label(value)
        elif key == "object_type":
            display = dict(form.fields[key].choices).get(value, value)
        else:
            display = str(value)
        chips.append({"label": label, "value": display,
            "url": _url({item: content for item, content in values.items() if item != key})})
    return {"audit_time_shortcuts": shortcuts, "audit_filter_chips": chips,
        "audit_time_start": form.cleaned_data["start_at"], "audit_time_end": form.cleaned_data["end_at"]}


def _brief(value):
    if isinstance(value, dict):
        return f"{len(value)} 项资料（展开查看）"
    if isinstance(value, list):
        return f"{len(value)} 条明细（展开查看）"
    text = str(value) if value != "" else "空白"
    return text if len(text) <= 120 else text[:117] + "…"


def enrich_projection(item, *, filters):
    """Never read raw AuditLog payloads here: all values are redacted upstream."""
    old = json.loads(item["old_data"])
    new = json.loads(item["new_data"])
    changes = []
    if isinstance(old, dict) and isinstance(new, dict):
        missing = object()
        for key in dict.fromkeys([*old, *new]):
            before, after = old.get(key, missing), new.get(key, missing)
            if before == after:
                continue
            changes.append({"label": key,
                "before": "未记录" if before is missing else _brief(before),
                "after": "未记录" if after is missing else _brief(after)})
    item["changes"] = changes[:6]
    item["extra_change_count"] = max(len(changes) - 6, 0)
    values = {key: value for key, value in filters.data.items() if key not in {"page", "q", "action"}}
    if item["object_id"] and item["object_type"] in dict(filters.fields["object_type"].choices):
        object_values = {key: value for key, value in values.items() if key != "correlation_id"}
        item["object_history_url"] = _url({**object_values,
            "object_type": item["object_type"], "object_id": item["object_id"]})
    if item["correlation_id"]:
        correlation_values = {key: value for key, value in values.items() if key not in {"object_type", "object_id"}}
        item["correlation_history_url"] = _url({**correlation_values, "correlation_id": item["correlation_id"]})
    return item
