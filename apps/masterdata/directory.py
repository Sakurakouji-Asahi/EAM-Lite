"""Expose existing, explicitly labeled employee notes without rewriting them."""


def employee_department_tree(departments, employee_department_ids):
    """Build paths/filters from already permission-scoped department records."""
    by_id = {department.pk: department for department in departments}
    used_ids = set(employee_department_ids)
    option_ids, labels, descendants = set(), {}, {}
    for department_id in by_id:
        chain, visited = [], set()
        current_id = department_id
        while current_id in by_id and current_id not in visited:
            visited.add(current_id)
            chain.append(current_id)
            current_id = by_id[current_id].parent_id
        labels[department_id] = " / ".join(by_id[pk].name for pk in reversed(chain))
        for ancestor_id in chain:
            descendants.setdefault(ancestor_id, set()).add(department_id)
        if department_id in used_ids:
            option_ids.update(chain)
    options = [department for pk, department in by_id.items() if pk in option_ids]
    for department in options:
        department.directory_label = labels[department.pk]
    return options, descendants, labels


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
