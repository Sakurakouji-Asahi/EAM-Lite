"""Controlled adoption and version replacement of the supplied coding standard."""
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.coding.domain import is_effective, validate_scheme_structure
from apps.coding.services import (
    _audit, _coding_mutation, _lock_company, _lock_scheme, _models,
    _refresh_coding_progress, _require_system_admin, _save, _snapshot,
    _validate_active_scheme, activate_scheme, create_scheme, set_default_scheme,
)
from apps.coding.standard import CATEGORY_DEFAULTS, is_standard_segments, standard_segments

PRESET_KEY = "unified_asset_identity"


@_coding_mutation
def install_standard_coding_rule(*, actor, company, include_categories=True, request=None):
    _require_system_admin(actor)
    company = _lock_company(company)
    from apps.masterdata.services import create_asset_category
    Category, Scheme = _models()["AssetCategory"], _models()["AssetCodingScheme"]
    if include_categories:
        for code, name in CATEGORY_DEFAULTS:
            existing = Category.objects.select_for_update().filter(company=company, normalized_code=code).first()
            if existing is not None:
                if existing.parent_id or existing.name != name or not existing.is_active:
                    raise ValidationError({"categories": f"现有分类 {code}（{existing.name}）与文档大类不一致，请先核对；本次未覆盖原分类。"})
            else:
                create_asset_category(actor=actor, company=company,
                    data={"code": code, "name": name}, request=request)
    scheme = Scheme.objects.select_for_update().prefetch_related("segments").filter(
        company=company, scheme_key=PRESET_KEY,
    ).order_by("-version").first()
    if scheme is None:
        scheme = create_scheme(actor=actor, company=company, data={
            "scheme_key": PRESET_KEY, "name": "统一资产编码",
            "description": "属性-实物大类-取得年份-六位流水-子项号；首次管理属性与财务认定独立，后续财务确认不改号。",
            "reset_mode": "category_yearly", "category_scope_level": "major", "sequence_start": 1,
            "effective_from": timezone.localdate(), "effective_to": None,
        }, segments=standard_segments(), request=request)
    if not is_standard_segments(validate_scheme_structure(scheme)):
        raise ValidationError("同名稳定键已用于其他规则，请先核对，不会覆盖已有方案。")
    if scheme.status == "retired":
        raise ValidationError("统一规则当前为历史版本，请从最新版本复制并启用新版本。")
    if scheme.status == "draft":
        scheme = activate_scheme(actor=actor, scheme=scheme, request=request)
    if not is_effective(scheme, timezone.localdate()):
        raise ValidationError("统一编码规则尚未生效，不能设置为当前默认。")
    scheme = set_default_scheme(actor=actor, scheme=scheme, request=request)
    _audit(company=company, actor=actor, action="coding_standard_adopted", instance=scheme,
           new_data={"include_categories": include_categories, "management_finance_independent": True}, request=request)
    return scheme


@_coding_mutation
def activate_standard_version(*, actor, scheme, request=None):
    """Replace an overlapping predecessor atomically, keeping the default usable."""
    _require_system_admin(actor)
    scheme = _lock_scheme(scheme)
    segments = validate_scheme_structure(scheme)
    if not is_standard_segments(segments) or not scheme.previous_version_id:
        return activate_scheme(actor=actor, scheme=scheme, request=request)
    if scheme.status == "active":
        return scheme
    if scheme.status != "draft" or scheme.effective_from is None or scheme.effective_from > timezone.localdate():
        raise ValidationError("请为新版本设置已到达的生效开始日，再办理替换。")
    previous = _lock_scheme(scheme.previous_version)
    if not is_standard_segments(validate_scheme_structure(previous)):
        raise ValidationError("旧版本不是统一规则，不能自动替换其业务含义。")
    was_default = previous.is_default
    if previous.status == "active":
        old = _snapshot(previous)
        previous.status, previous.is_default = "retired", False
        _save(previous, update_fields=["status", "is_default", "updated_at"])
        _audit(company=scheme.company, actor=actor, action="coding_scheme_retire", instance=previous,
               old_data=old, new_data=_snapshot(previous), request=request)
    old = _snapshot(scheme)
    scheme.status, scheme.is_default = "active", was_default
    _validate_active_scheme(scheme)
    _save(scheme, update_fields=["status", "is_default", "updated_at"])
    _audit(company=scheme.company, actor=actor, action="coding_scheme_activate", instance=scheme,
           old_data=old, new_data={**_snapshot(scheme), "shared_identity_sequence": True}, request=request)
    _refresh_coding_progress(company=scheme.company, actor=actor, request=request)
    return scheme
