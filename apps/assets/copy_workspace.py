"""Prefill a new, unsaved asset from explicitly reusable physical information."""
import uuid

from django.core.exceptions import PermissionDenied
from django.http import Http404

from .access import asset_or_404
from .permissions import can_create_asset_draft, can_view_asset_p1


COPY_TEXT_FIELDS = (
    "asset_name", "brand", "model", "manufacturer", "unit", "description", "is_maintenance_required",
)
COPY_RELATION_FIELDS = ("category", "department", "responsible_employee", "location")


def can_copy_asset_basics(actor, asset):
    return can_view_asset_p1(actor, asset) and can_create_asset_draft(actor, asset.company, asset.department)


def copy_source_for_request(request, company):
    raw = request.POST.get("copy_from", request.GET.get("copy_from", "")) if request.method == "POST" else request.GET.get("copy_from", "")
    if not raw:
        return None
    try:
        source_id = uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Http404("复制来源标识格式无效。") from exc
    source = asset_or_404(request.user, company, source_id)
    if not can_copy_asset_basics(request.user, source):
        raise PermissionDenied("需要查看来源完整实物资料及在此范围新建资产的权限。")
    return source


def copy_basic_initial(source, form):
    initial = {name: getattr(source, name) for name in COPY_TEXT_FIELDS}
    omitted = []
    for name in COPY_RELATION_FIELDS:
        value = getattr(source, name)
        if value is not None and form.fields[name].queryset.filter(pk=value.pk).exists():
            initial[name] = value
        else:
            initial[name] = None
            if value is not None:
                omitted.append(form.fields[name].label)
    return initial, omitted
