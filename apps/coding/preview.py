"""Permission-bound read-only next-number preview, never reserving a number."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Max
from django.utils import timezone

from apps.assets.permissions import can_create_asset_draft
from apps.coding.domain import build_scope_key, render_code, validate_scheme_structure
from apps.coding.issuance import _resolve_coding_scheme
from apps.coding.standard import identity_scope, is_standard_segments
from apps.coding.standard_issuance import _identity_context, _prepare_identity_parts


def preview_next_asset_code(*, actor, asset):
    from apps.assets.models import AssetIdentity
    from apps.masterdata.models import SequenceCounter
    from apps.masterdata.permissions import current_company

    company = current_company()
    if company is None or asset.company_id != company.pk or not can_create_asset_draft(actor, company, asset.department):
        raise PermissionDenied("您没有在当前部门建立资产或预览编号的权限。")
    today = timezone.localdate()
    scheme = _resolve_coding_scheme(asset=asset, effective_date=today, lock=False)
    segments = validate_scheme_structure(scheme)
    if is_standard_segments(segments):
        values = _prepare_identity_parts(actor=actor, asset=asset, lock_parent=False, validate_details=False)
        if values["parent_identity_id"] is not None:
            subitem = (AssetIdentity.objects.filter(parent_identity_id=values["parent_identity_id"])
                       .aggregate(last=Max("subitem_number"))["last"] or 0) + 1
            if subitem > 99:
                raise ValidationError("该主资产的组件子项号已经用完。")
            sequence = values["sequence_value"]
        else:
            subitem = 0
            scope = identity_scope(company.pk, values["management_attribute"], values["category_code"], values["coding_year"])
            current = SequenceCounter.objects.filter(company=company, scope_key=scope).values_list("current_value", flat=True).first()
            sequence = (current or 0) + 1
        context = _identity_context(asset, values, effective_date=today, subitem=subitem)
    else:
        scoped = scheme.reset_mode in {"category_yearly", "category_monthly"}
        scope = build_scope_key(company.pk, scheme.pk, scheme.reset_mode, today,
            category=asset.category if scoped else None, category_scope_level=scheme.category_scope_level if scoped else None)
        current = SequenceCounter.objects.filter(company=company, coding_scheme=scheme, scope_key=scope).values_list("current_value", flat=True).first()
        sequence = scheme.sequence_start if current is None else current + 1
        context = {"company": company, "category": asset.category, "department": asset.department, "effective_date": today}
    return render_code(segments, context, sequence)
