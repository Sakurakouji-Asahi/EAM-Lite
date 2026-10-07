"""Group the existing authorized due-instance list by its scheduled date."""
from collections import Counter
from itertools import groupby

from django import forms

from .workspaces import PlanQueryForm


class DueAgendaForm(PlanQueryForm):
    due_date = forms.DateField(label="计划日期", required=False,
        widget=forms.DateInput(attrs={"type":"date"}), help_text="只查看该日到期的待保养事项。")
    view = forms.ChoiceField(label="显示方式", required=False, initial="agenda",
        choices=(("agenda", "按日期日程"), ("list", "明细列表")))
    field_order = ("q", "department", "responsible_employee", "due_scope", "due_date", "view")

    def clean_view(self):
        return self.cleaned_data.get("view") or "agenda"


def sorted_due_items(items):
    return sorted(items, key=lambda item:(item["plan"].next_maintenance_date, item["plan"].name, str(item["plan"].pk)))


def agenda_context(request, *, form, items, page, today):
    """`items` already follows access, text, department, owner and due-state filters."""
    valid = form.is_valid()
    selected = form.cleaned_data.get("due_date") if valid else None
    view = form.cleaned_data.get("view", "agenda") if valid else "agenda"
    params = request.GET.copy()
    params.pop("page", None)

    def link(**updates):
        query = params.copy()
        for key, value in updates.items():
            if value in (None, ""):
                query.pop(key, None)
            else:
                query[key] = str(value)
        return "?" + query.urlencode() + "#maintenance-agenda" if query else request.path + "#maintenance-agenda"

    totals = Counter(item["plan"].next_maintenance_date for item in items)
    groups = []
    for due, group in groupby(page.object_list, key=lambda item:item["plan"].next_maintenance_date):
        rows = list(group)
        groups.append({"date":due, "items":rows, "count":totals[due], "visible_count":len(rows),
            "day_url":link(due_date=due.isoformat(), view="agenda"),
            "due_status":rows[0]["due_status"], "due_label":rows[0]["due_label"],
            "due_timing":rows[0]["due_timing"]})
    return {"agenda_groups":groups, "due_view":view, "selected_due_date":selected,
        "agenda_total_days":len(totals), "agenda_all_dates_url":link(due_date=None),
        "agenda_today_url":link(due_date=today.isoformat(), view="agenda"), "agenda_today_count":totals[today],
        "due_view_links":{"agenda":link(view="agenda"), "list":link(view="list")}}
