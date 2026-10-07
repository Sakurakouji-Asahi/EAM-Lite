"""Continue entry with a valid department/parent and the original list."""
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.return_navigation import safe_return_url


CONTEXT_FIELDS = {
    "department": "parent", "employee": "department",
    "location": "parent", "asset_category": "parent",
}


def resource_slug(resource):
    return "category" if resource == "asset_category" else resource


def apply_create_context(request, form, resource):
    """Use the form's existing allowed choices, never trust a query ID."""
    name = CONTEXT_FIELDS.get(resource)
    if request.method != "GET" or form.is_bound or not name or not request.GET.get(name):
        return ""
    field = form.fields[name]
    value = request.GET[name]
    try:
        if not value.isdecimal() or len(value) > 19 or int(value) > 9223372036854775807:
            raise ValidationError("无效选择")
        selected = field.clean(value)
    except ValidationError:
        return f"预选的{field.label}已失效或超出当前范围，请重新选择。"
    form.initial[name] = selected.pk
    return ""


def next_create_url(request, resource, obj):
    slug = resource_slug(resource)
    source = safe_return_url(request, reverse(f"masterdata:{slug}-list"))
    query = {"return_to": source}
    field = CONTEXT_FIELDS.get(resource)
    selected_id = getattr(obj, f"{field}_id", None) if field else None
    if selected_id:
        query[field] = selected_id
    return reverse(f"masterdata:{slug}-create") + "?" + urlencode(query)
