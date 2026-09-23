"""Shared identity checks for imported drafts, without reserving any number."""
from django.core.exceptions import ValidationError
from django.utils import timezone

IDENTITY_COLUMNS = (
    ("首次管理属性", "management_attribute"),
    ("取得年份", "coding_year"),
    ("取得年份依据", "coding_year_note"),
    ("主资产编号", "parent_asset_code"),
    ("车牌号", "vehicle_plate"),
    ("车架号", "chassis_number"),
    ("校准编号", "calibration_number"),
)
IDENTITY_COLUMN_KEYS = frozenset(key for _, key in IDENTITY_COLUMNS)


def validate_import_identity(*, actor, company, data, parent_code="", lock_parent=False):
    from apps.assets.models import Asset
    from apps.assets.permissions import scoped_assets_p1
    from apps.coding.domain import normalize_code, validate_scheme_structure
    from apps.coding.issuance import _resolve_coding_scheme
    from apps.coding.standard import MANAGEMENT_CODES, is_standard_segments
    from apps.coding.standard_issuance import _prepare_identity_parts
    from apps.masterdata.models import AssetCodingScheme

    attribute = str(data.get("management_attribute") or "").strip().upper()
    year = data.get("coding_year")
    if attribute and attribute not in MANAGEMENT_CODES:
        raise ValidationError({"management_attribute": "首次管理属性只能为 FA、LV、IA、LS 或 OT。"})
    if year is not None and not 1000 <= year <= timezone.localdate().year:
        raise ValidationError({"coding_year": "取得年份须为四位年份，且不能晚于当前年份。"})
    parent = data.get("component_of")
    parent_identifier = data.get("component_of_id") or getattr(parent, "pk", None)
    if parent_code or parent_identifier:
        parents = scoped_assets_p1(actor, company, Asset.objects.select_related(
            "company", "department", "current_issued_code__identity",
            "current_issued_code__coding_scheme",
        ))
        if lock_parent:
            parents = parents.select_for_update(of=("self",))
        parent = parents.filter(
            **({"current_issued_code__normalized_code": normalize_code(parent_code)}
               if parent_code else {"pk": parent_identifier})
        ).first()
        if parent is None:
            raise ValidationError({"component_of": "主资产编号无效或不在当前账号可用范围内；只能引用已建档的主资产。"})
    result = {"management_attribute": attribute, "coding_year": year,
              "coding_year_note": data.get("coding_year_note") or "", "component_of": parent}
    candidate = Asset(company=company, category=data["category"], department=data["department"],
                      acquisition_date=data.get("acquisition_date"), serial_number=data.get("serial_number") or "",
                      **result)
    # Existing generic setups can still import physical drafts before choosing a rule.
    if not parent and not candidate.category.default_coding_scheme_id and not AssetCodingScheme.objects.filter(
        company=company, status="active", is_default=True
    ).exists():
        return result
    scheme = _resolve_coding_scheme(asset=candidate, effective_date=timezone.localdate(), lock=False)
    if is_standard_segments(validate_scheme_structure(scheme)):
        parts = _prepare_identity_parts(actor=actor, asset=candidate, lock_parent=lock_parent)
        if parent is not None:
            result.update(management_attribute=parts["management_attribute"], coding_year=parts["coding_year"],
                          coding_year_note=parts["year_note"])
    elif parent is not None:
        raise ValidationError({"component_of": "组件必须沿用主资产的统一编码规则。"})
    return result
