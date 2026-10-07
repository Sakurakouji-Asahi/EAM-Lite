"""Readable, scoped endpoints and reversal links for custody history."""
from urllib.parse import urlencode

from django.urls import reverse


def _endpoint(movement, side, custody, return_to):
    record_id = getattr(movement, side + "_custody_id")
    if record_id is None:
        labels = {("issue", "from"): "领用来源", ("opening", "from"): "期初来源",
            ("return", "to"): "归还仓库", ("loss", "to"): "报损", ("scrap", "to"): "报废"}
        return {"label": labels.get((movement.action, side), "未关联保管"), "url": ""}
    if not getattr(movement, side + "_custody_visible"):
        return {"label": "范围外保管（不可查看）", "url": ""}
    record = getattr(movement, side + "_custody")
    label = f"{record.department} / {record.employee or '部门保管'}"
    if record_id == custody.pk:
        return {"label": label, "url": "#custody-current-balance", "current": True}
    return {"label": label, "url": reverse("supplies:custody-detail", args=[record_id]) + "?" + urlencode({"return_to": return_to})}


def attach_custody_history(movements, custody, return_to):
    by_id = {movement.pk: movement for movement in movements}
    for index, movement in enumerate(movements, start=1):
        movement.history_index = index
        movement.history_anchor = "custody-movement-" + str(movement.pk)
        movement.history_from = _endpoint(movement, "from", custody, return_to)
        movement.history_to = _endpoint(movement, "to", custody, return_to)
        movement.history_original = None
        movement.history_reversal = None
    for movement in movements:
        original = by_id.get(movement.reverses_movement_id)
        if original is not None:
            movement.history_original = {"url": "#" + original.history_anchor,
                "label": f"第 {original.history_index} 次 · {original.get_action_display()}"}
            original.history_reversal = {"url": "#" + movement.history_anchor,
                "label": f"第 {movement.history_index} 次 · 冲销"}
