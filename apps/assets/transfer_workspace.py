"""Read-only presentation for checking an asset's assignment before saving."""
from apps.assets.form_options import location_path


ASSIGNMENT_FIELDS = (
    ("to_department", "部门", "department"),
    ("to_responsible_employee", "责任人", "responsible_employee"),
    ("to_location", "位置", "location"),
)


def assignment_label(kind, item):
    if item is None:
        return "未记录"
    if kind == "responsible_employee":
        return f"{item.employee_no} · {item.name}"
    name = location_path(item) if kind == "location" else item.name
    return f"{item.code} / {name}"


def configure_transfer_labels(form):
    for field_name, _label, kind in ASSIGNMENT_FIELDS:
        form.fields[field_name].label_from_instance = (
            lambda item, kind=kind: assignment_label(kind, item)
        )


def transfer_review_context(asset, form):
    rows = []
    for field_name, label, kind in ASSIGNMENT_FIELDS:
        original = getattr(asset, kind)
        original_id = str(getattr(asset, kind + "_id") or "")
        value = form[field_name].value()
        target = None
        try:
            target_id = int(str(value))
        except (ValueError, TypeError):
            target_id = None
        if target_id is not None:
            # Use the form's existing choices, including its company/active boundaries.
            target = form.fields[field_name].queryset.filter(pk=target_id).first()
        target_value = str(target.pk) if target else ""
        state = "missing" if target is None else "unchanged" if target_value == original_id else "changed"
        rows.append({"field_name": field_name, "label": label, "original_id": original_id,
                     "original_label": assignment_label(kind, original),
                     "target_label": assignment_label(kind, target) if target else "请选择有效目标",
                     "state": state, "changed": state == "changed",
                     "state_label": {"missing": "待选择", "unchanged": "保持不变", "changed": "将变更"}[state]})
    return {"rows": rows, "changed_count": sum(row["changed"] for row in rows),
            "missing_count": sum(row["state"] == "missing" for row in rows)}
