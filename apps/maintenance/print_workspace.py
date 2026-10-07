"""Shared record evidence and read-only paper navigation."""
from django.urls import reverse
from apps.maintenance.permissions import can_manage_maintenance_attachment, can_view_maintenance_attachment


def record_attachment_rows(user, record, problem):
    rows = []
    targets = [(record, "完成记录证据")]
    if problem is not None:
        targets.append((problem, "问题处理证据"))
    for target, label in targets:
        for link in target.attachment_links.select_related("attachment", "created_by"):
            if can_view_maintenance_attachment(user, link):
                rows.append({"link": link, "source_label": label,
                    "can_void": can_manage_maintenance_attachment(user, target, security_class=link.security_class)})
    return rows


def record_paper_url(request, record, *, detail=False):
    name = "maintenance:record-detail" if detail else "maintenance:record-print"
    url = reverse(name, args=[record.pk])
    return url + ("?" + request.GET.urlencode() if request.GET else "")
