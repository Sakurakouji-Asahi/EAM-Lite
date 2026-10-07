"""Return from a record to the exact scoped list or its own plan history."""
from urllib.parse import urlsplit

from django.urls import reverse


def record_navigation_context(request, record):
    fallback = reverse("maintenance:record-list")
    value = request.GET.get("return_to", "")
    allowed = {
        fallback: "返回原记录查询",
        reverse("maintenance:plan-detail", args=[record.maintenance_plan_id]): "返回计划时间线",
    }
    if value and len(value) <= 6000 and "\\" not in value and not any(ord(char) < 32 for char in value):
        try:
            parts = urlsplit(value)
            if (not parts.scheme and not parts.netloc and not parts.fragment
                    and value.startswith("/") and not value.startswith("//")
                    and parts.path in allowed):
                return {"record_return_url": value, "record_return_label": allowed[parts.path]}
        except ValueError:
            pass
    return {"record_return_url": fallback, "record_return_label": "返回保养记录"}
