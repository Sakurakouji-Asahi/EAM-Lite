"""Read-only directory navigation using existing employee fields and states."""
from django.http import QueryDict
from django.urls import reverse

from .directory import employee_directory_metadata
from .models import Employee
from .permissions import scoped_departments


EMPLOYEE_QUERY_FIELDS = ("q", "department", "position", "source_mark", "status", "employment_status")


def employee_directory_url(query, **changes):
    params = QueryDict(mutable=True)
    for key in EMPLOYEE_QUERY_FIELDS:
        if query.get(key):
            params[key] = str(query[key])
    for key, value in changes.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    return reverse("masterdata:employee-list") + ("?" + params.urlencode() if params else "")


def employee_filter_context(*, query, department_options):
    """Single-condition removal leaves all other applied conditions intact."""
    departments = {str(item.pk): item for item in department_options}
    employment_labels = dict(Employee.EmploymentStatus.choices)
    chips = []
    values = (("q", "关键词", query.get("q", "")),
              ("department", "部门（含下级）", departments[query["department"]].directory_label
               if query.get("department") in departments else "无效或超出范围" if query.get("department") else ""),
              ("position", "岗位", query.get("position", "")),
              ("source_mark", "人员标记", query.get("source_mark", "")),
              ("employment_status", "任职状态", employment_labels.get(query.get("employment_status"), "")))
    for key, label, value in values:
        if value:
            chips.append({"label": label, "value": value, "remove_url": employee_directory_url(query, **{key: None})})
    status = query.get("status", "active")
    if status != "all":
        chips.append({"label": "启用状态", "value": "停用" if status == "inactive" else "启用",
                      "remove_url": employee_directory_url(query, status="all")})
    shortcuts = [{"label": "全部任职状态", "url": employee_directory_url(query, status="all", employment_status=None),
                  "selected": status == "all" and not query.get("employment_status")}]
    shortcuts.extend({"label": label, "url": employee_directory_url(query, status="all", employment_status=value),
                      "selected": status == "all" and query.get("employment_status") == value}
                     for value, label in Employee.EmploymentStatus.choices)
    conflict = status == "active" and query.get("employment_status") in {"leaving", "resigned"}
    return {"employee_filter_chips": chips, "employee_status_shortcuts": shortcuts,
            "employee_status_conflict": conflict,
            "employee_department_valid": not query.get("department") or query["department"] in departments,
            "employee_include_inactive_url": employee_directory_url(query, status="all")}


def employee_profile_context(actor, employee):
    metadata = employee_directory_metadata(employee.remark)
    context = {**metadata, "mobile": employee.mobile}
    if scoped_departments(actor, employee.company).filter(pk=employee.department_id).exists():
        context["department_url"] = employee_directory_url({"department": employee.department_id, "status": "all"})
    if metadata["position"]:
        context["position_url"] = employee_directory_url({"position": metadata["position"], "status": "all"})
    if metadata["source_mark"]:
        context["source_url"] = employee_directory_url({"source_mark": metadata["source_mark"], "status": "all"})
    return context
