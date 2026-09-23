"""Private allocation helpers for controlled registration/code correction only."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Max
from django.utils import timezone

from apps.coding.domain import _category_at_level, render_code
from apps.coding.standard import identity_scope, resolve_year, validate_parts


def _prepare_identity_parts(*, actor, asset, lock_parent=True, validate_details=True):
    from apps.assets.models import Asset
    from apps.assets.services import _validate_component_scope

    today = timezone.localdate()
    if asset.current_issued_code_id is not None and asset.identity is None:
        raise ValidationError("旧编号资产不能通过改号自动推断首次管理属性；原编号和历史继续保留。")
    if asset.acquisition_date is not None and asset.acquisition_date > today:
        raise ValidationError({"acquisition_date": "取得日期不能晚于本次纳入管理日期。"})
    if asset.component_of_id:
        parents = Asset.objects.select_related("company", "department", "current_issued_code__identity")
        if lock_parent:
            parents = parents.select_for_update(of=("self",))
        parent = parents.filter(pk=asset.component_of_id, company=asset.company).first()
        if parent is None:
            raise PermissionDenied("不能关联其他公司或不存在的主资产。")
        _validate_component_scope(actor, asset.company, parent)
        identity = parent.identity
        if asset.management_attribute and asset.management_attribute != identity.management_attribute:
            raise ValidationError({"management_attribute": "组件的编码管理属性必须沿用主资产。"})
        if asset.coding_year is not None and asset.coding_year != identity.coding_year:
            raise ValidationError({"coding_year": "组件的编码年份必须沿用主资产；本件购置日期可单独记录。"})
        return {
            "management_attribute": identity.management_attribute,
            "category_code": identity.category_code, "coding_year": identity.coding_year,
            "year_source": "parent", "year_note": identity.year_note,
            "parent_asset_id": parent.pk, "parent_identity_id": identity.pk,
            "sequence_value": identity.sequence_value,
        }
    existing_identity = asset.identity
    if existing_identity is not None:
        return {name: getattr(existing_identity, name) for name in (
            "management_attribute", "category_code", "coding_year", "year_source", "year_note",
            "parent_asset_id", "parent_identity_id",
        )}
    root = _category_at_level(asset.category, "major")
    if root.company_id != asset.company_id or not root.is_active:
        raise ValidationError({"category": "一级实物分类必须在当前公司启用。"})
    year, source, note = resolve_year(
        requested_year=asset.coding_year, acquisition_date=asset.acquisition_date,
        note=asset.coding_year_note, today=today, require_evidence=validate_details,
    )
    validate_parts(asset.management_attribute, root.code, year)
    if validate_details and root.code == "04" and not str(asset.serial_number or "").strip():
        raise ValidationError({"serial_number": "电子及信息设备须填写序列号；电子许可可填写许可标识。"})
    return {"management_attribute": asset.management_attribute, "category_code": root.code,
            "coding_year": year, "year_source": source, "year_note": note,
            "parent_asset_id": None, "parent_identity_id": None}


def _identity_context(asset, values, *, effective_date, subitem):
    return {"company": asset.company, "category": asset.category, "department": asset.department,
            "effective_date": effective_date, "management_attribute": values["management_attribute"],
            "coding_year": values["coding_year"], "identity_category_code": values["category_code"],
            "subitem_number": subitem}


def _allocate_standard_code(*, actor, asset, scheme, effective_date):
    from apps.assets.models import AssetIdentity
    from apps.coding.issuance import _insert_counter_if_missing

    if not connection.in_atomic_block:
        raise ValidationError("统一编码只能随受控资产事务分配。")
    values = _prepare_identity_parts(actor=actor, asset=asset)
    if asset.current_issued_code_id is None:
        asset.management_attribute = values["management_attribute"]
        asset.coding_year = values["coding_year"]
        asset.coding_year_note = values["year_note"]
        asset.save(update_fields=["management_attribute", "coding_year", "coding_year_note"])
    if values["parent_identity_id"] is not None:
        subitem = (AssetIdentity.objects.filter(parent_identity_id=values["parent_identity_id"])
                   .aggregate(last=Max("subitem_number"))["last"] or 0) + 1
        if subitem > 99:
            raise ValidationError({"component_of": "主资产的 99 个组件子项号已经用完，历史子项号不会复用。"})
        sequence = values["sequence_value"]
        scope = identity_scope(asset.company_id, values["management_attribute"], values["category_code"],
                               values["coding_year"], subitem=subitem, parent_id=values["parent_identity_id"])
        counter = None
    else:
        subitem = 0
        scope = identity_scope(asset.company_id, values["management_attribute"], values["category_code"], values["coding_year"])
        counter = _insert_counter_if_missing(company=asset.company, scheme=scheme, scope_key=scope, standard_scope=True)
        sequence = counter.current_value + 1
    display = render_code(list(scheme.segments.order_by("sequence_order")),
                          _identity_context(asset, values, effective_date=effective_date, subitem=subitem), sequence)
    if counter is not None:
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('eam_lite.controlled_sequence_counter_increment','on',true)")
        counter.current_value = sequence
        counter.save(update_fields=["current_value", "updated_at"])
    values.update({"sequence_value": sequence, "subitem_number": subitem})
    return scope, sequence, display, values


def _save_identity(*, asset, issued, values):
    from apps.assets.models import AssetIdentity
    if not connection.in_atomic_block:
        raise ValidationError("编码身份必须随建档或受控更正事务保存。")
    result = AssetIdentity(company=asset.company, asset=asset, issued_code=issued, **values)
    result.full_clean()
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('eam_lite.controlled_asset_identity','on',true)")
    result.save()
    return result
