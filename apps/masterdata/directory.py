"""Expose existing, explicitly labeled employee notes without rewriting them."""


def employee_directory_metadata(remark):
    result = {"position": "", "source_mark": ""}
    labels = {"岗位": "position", "原表人员标记": "source_mark"}
    for line in (remark or "").splitlines():
        key, separator, value = line.replace("：", ":", 1).partition(":")
        field = labels.get(key.strip())
        if separator and field and not result[field]:
            result[field] = value.strip()
    return result


def prepare_employee_directory(queryset, *, position="", source_mark=""):
    metadata = {pk: employee_directory_metadata(remark) for pk, remark in queryset.values_list("pk", "remark")}
    positions = sorted({row["position"] for row in metadata.values() if row["position"]})
    sources = sorted({row["source_mark"] for row in metadata.values() if row["source_mark"]})
    if position or source_mark:
        identifiers = [pk for pk, row in metadata.items()
                       if (not position or row["position"] == position)
                       and (not source_mark or row["source_mark"] == source_mark)]
        queryset = queryset.filter(pk__in=identifiers)
    for employee in queryset:
        employee.directory_position = metadata[employee.pk]["position"]
        employee.directory_source_mark = metadata[employee.pk]["source_mark"]
    return queryset, positions, sources
