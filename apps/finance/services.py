"""Transactional finance services for Sprint 4.

Every public mutation in this module enforces the finance role itself.  Views,
forms and management commands must not be able to widen the matrix by calling
an unguarded helper.  Imports of finance models are delayed because the app is
also imported while Django builds the migration state.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.coding.domain import (
    build_scope_key,
    is_effective,
    normalize_code,
    render_code,
    validate_scheme_structure,
)
from apps.finance.permissions import require_manage_finance
from apps.masterdata.permissions import current_company


SHANGHAI = ZoneInfo("Asia/Shanghai")
CENT = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")
DEPRECIATION_ENGINE_VERSION = "sprint4-v1"
CONTROLLED_NON_FIXED_DEPRECIATION_ERROR = (
    "该资产属于逐件低值耐用品/受控非固定资产，不计提折旧。"
)
POLICY_EDITABLE_FIELDS = frozenset(
    {
        "policy_key",
        "name",
        "method",
        "posting_period",
        "start_rule",
        "stop_rule",
        "default_useful_life_months",
        "default_salvage_mode",
        "default_salvage_rate",
        "default_salvage_amount",
        "annual_posting_month",
        "work_unit",
        "effective_from",
        "effective_to",
    }
)
PROFILE_INPUT_FIELDS = frozenset(
    {
        "depreciation_policy",
        "depreciation_policy_id",
        "method",
        "posting_period",
        "start_rule",
        "stop_rule",
        "specified_start",
        "start_date",
        "actual_continuation_date",
        "useful_life_months",
        "salvage_mode",
        "salvage_rate",
        "salvage_amount",
        "opening_book_value",
        "opening_actual_accumulated_depreciation",
        "opening_impairment",
        "expected_total_units",
        "work_unit",
        "annual_posting_month",
        "effective_from",
        "effective_to",
        "change_reason",
        "allow_historical_start",
    }
)
FINANCE_DRAFT_FIELDS = frozenset(
    {
        "accounting_treatment",
        "accounting_treatment_reason",
        "fixed_asset_category",
        "fixed_asset_category_id",
        "original_cost",
        "capitalization_date",
        "finance_remark",
    }
)
FIXED_ASSET_CATEGORY_EDITABLE_FIELDS = frozenset(
    {"code", "name", "useful_life_months_default", "note"}
)


def _models():
    from apps.assets.models import Asset, AssetCodeHistory, AssetQrIdentity
    from apps.finance.models import (
        AssetFinance,
        AssetDepreciationProfile,
        AssetValueAdjustment,
        AssetWorkUsage,
        DepreciationBatch,
        DepreciationBatchItem,
        DepreciationEntry,
        DepreciationPolicy,
        DepreciationProfileEvent,
        DepreciationSchedule,
        FinanceFormalizationRequest,
        TheoreticalDepreciationLine,
        TheoreticalDepreciationRun,
    )
    from apps.masterdata.models import (
        AssetCategory,
        AssetCodingScheme,
        Company,
        FixedAssetCategory,
        InitializationSetting,
        IssuedCode,
        SequenceCounter,
        SystemSetting,
    )

    result = locals()
    return {name: value for name, value in result.items() if isinstance(value, type)}


def depreciable_fixed_asset_filter(prefix=""):
    relation = f"{prefix}__" if prefix else ""
    return Q(
        **{
            f"{relation}finance__accounting_treatment": "fixed_asset",
            f"{relation}finance__finance_confirmed_at__isnull": False,
        }
    )


def is_depreciable_fixed_asset(asset, *, finance=None) -> bool:
    if finance is None:
        Finance = _models()["AssetFinance"]
        finance = Finance.objects.filter(
            asset_id=getattr(asset, "pk", None),
            company_id=getattr(asset, "company_id", None),
        ).first()
    return bool(
        finance is not None
        and finance.accounting_treatment == "fixed_asset"
        and finance.finance_confirmed_at is not None
    )


def ensure_asset_is_depreciable(
    asset, *, finance=None, finance_data=None, require_confirmed=True
):
    if finance_data is not None:
        treatment = finance_data.get("accounting_treatment")
        confirmed = not require_confirmed
    else:
        if finance is None:
            Finance = _models()["AssetFinance"]
            finance = Finance.objects.filter(
                asset_id=getattr(asset, "pk", None),
                company_id=getattr(asset, "company_id", None),
            ).first()
        treatment = getattr(finance, "accounting_treatment", None)
        confirmed = bool(
            finance is not None and finance.finance_confirmed_at is not None
        )
    if treatment == "controlled_non_fixed":
        raise ValidationError(CONTROLLED_NON_FIXED_DEPRECIATION_ERROR)
    if treatment != "fixed_asset" or (require_confirmed and not confirmed):
        raise ValidationError("只有经财务确认的固定资产可以执行折旧业务。")
    return finance


def _serializable(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if hasattr(value, "pk"):
        return str(value.pk)
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def _request_hash(payload) -> str:
    encoded = json.dumps(
        _serializable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _money(value, *, field_name="amount", allow_none=False) -> Decimal | None:
    if value is None:
        if allow_none:
            return None
        raise ValidationError({field_name: "金额不能为空。"})
    if isinstance(value, float):
        raise ValidationError({field_name: "金额不得经过 float；请提交十进制字符串。"})
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field_name: "必须是有效十进制金额。"}) from exc
    if not parsed.is_finite():
        raise ValidationError({field_name: "金额必须是有限十进制数。"})
    return parsed.quantize(CENT, rounding=ROUND_HALF_UP)


def _decimal(value, *, field_name, allow_none=False) -> Decimal | None:
    if value is None:
        if allow_none:
            return None
        raise ValidationError({field_name: "不能为空。"})
    if isinstance(value, float):
        raise ValidationError({field_name: "不得经过 float；请提交十进制字符串。"})
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field_name: "必须是有效十进制数。"}) from exc
    if not parsed.is_finite():
        raise ValidationError({field_name: "必须是有限十进制数。"})
    return parsed


def _business_date(value=None, *, field_name="effective_date") -> date:
    if value is None:
        return timezone.localdate(timezone=SHANGHAI)
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            raise ValidationError({field_name: "日期时间必须包含时区。"})
        return value.astimezone(SHANGHAI).date()
    if not isinstance(value, date):
        raise ValidationError({field_name: "必须是有效日期。"})
    return value


def _required_reason(value, *, field_name="reason") -> str:
    reason = str(value or "").strip()
    if not reason:
        raise ValidationError({field_name: "该受控业务动作必须填写原因。"})
    return reason


def _require_profile_continuation_reviewed(profile):
    if (
        profile.actual_continuation_review_required
        or profile.actual_continuation_date is None
    ):
        raise ValidationError(
            {
                "actual_continuation_date": (
                    f"Profile v{profile.version} 的实际接续日尚待财务复核；"
                    "请先在资产财务详情完成一次性复核。"
                )
            }
        )


def _for_update_self(queryset):
    """Lock only rows from the queryset's primary model on PostgreSQL.

    Several finance queries follow nullable evidence/profile relations.  A
    blanket ``FOR UPDATE`` asks PostgreSQL to lock the nullable side of those
    outer joins and is rejected before the service can run.  The service lock
    order names the authoritative row explicitly; related rows are locked by
    their own query when they are mutation inputs.
    """

    if connection.vendor == "postgresql":
        return queryset.select_for_update(of=("self",))
    return queryset.select_for_update()


def _effective_timestamp(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=SHANGHAI)


def _require_current_company(company):
    selected = current_company(include_inactive=True)
    if company is None or selected is None or company.pk != selected.pk:
        raise PermissionDenied("目标记录不属于当前公司。")
    return company


def _audit(*, actor, action, instance, old=None, new=None, request=None):
    return write_business_audit_log(
        company=instance.company,
        user=actor,
        action=action,
        object_type=instance._meta.object_name,
        object_id=instance.pk,
        old_data=old or {},
        new_data=new or {},
        **request_audit_context(request),
    )


def _apply(instance, data, allowed):
    data = dict(data)
    unknown = set(data).difference(allowed)
    if unknown:
        raise ValidationError({field: "此字段不允许通过该财务服务修改。" for field in unknown})
    for field, value in data.items():
        setattr(instance, field, value)
    return instance


def _save(instance, *, update_fields=None, validate=True):
    if validate:
        instance.full_clean()
    instance.save(update_fields=update_fields)
    return instance


def _policy_snapshot(policy):
    fields = (*sorted(POLICY_EDITABLE_FIELDS), "version", "status", "is_default", "previous_version_id")
    return {field: _serializable(getattr(policy, field)) for field in fields}


def _is_policy_effective(policy, target: date) -> bool:
    return bool(
        policy.status == "active"
        and policy.effective_from is not None
        and policy.effective_from <= target
        and (policy.effective_to is None or target <= policy.effective_to)
    )


def _validate_policy(policy):
    errors = {}
    if policy.posting_period not in {"monthly", "yearly"}:
        errors["posting_period"] = "只能使用 monthly 或 yearly。"
    if policy.default_salvage_mode not in {"rate", "amount"}:
        errors["default_salvage_mode"] = "只能使用 rate 或 amount。"
    if policy.default_salvage_mode == "rate":
        rate = _decimal(policy.default_salvage_rate, field_name="default_salvage_rate")
        if rate < 0 or rate > 1:
            errors["default_salvage_rate"] = "残值率必须在 0 至 1 之间。"
        if policy.default_salvage_amount is not None:
            errors["default_salvage_amount"] = "rate 模式不能填写残值金额。"
    elif policy.default_salvage_mode == "amount":
        amount = _money(policy.default_salvage_amount, field_name="default_salvage_amount")
        if amount < 0:
            errors["default_salvage_amount"] = "残值金额不得为负数。"
        if policy.default_salvage_rate is not None:
            errors["default_salvage_rate"] = "amount 模式不能填写残值率。"
    if policy.posting_period == "yearly":
        if not isinstance(policy.annual_posting_month, int) or not 1 <= policy.annual_posting_month <= 12:
            errors["annual_posting_month"] = "yearly 必须填写 1 至 12 的计提月份。"
        if policy.default_useful_life_months and policy.default_useful_life_months % 12:
            errors["default_useful_life_months"] = "年度计提的默认寿命必须为 12 的整数倍。"
    elif policy.annual_posting_month is not None:
        errors["annual_posting_month"] = "monthly 不能填写年度计提月份。"
    if policy.method == "sum_of_years_digits" and policy.default_useful_life_months and policy.default_useful_life_months % 12:
        errors["default_useful_life_months"] = "年数总和法寿命必须为 12 的整数倍。"
    if policy.method == "units_of_production" and not str(policy.work_unit or "").strip():
        errors["work_unit"] = "工作量法必须填写单位。"
    if policy.effective_from and policy.effective_to and policy.effective_to < policy.effective_from:
        errors["effective_to"] = "生效结束日不得早于开始日。"
    if errors:
        raise ValidationError(errors)


def _fixed_asset_category_snapshot(category):
    return {
        "code": category.code,
        "normalized_code": category.normalized_code,
        "name": category.name,
        "useful_life_months_default": category.useful_life_months_default,
        "note": category.note,
        "is_active": category.is_active,
    }


@transaction.atomic
def create_fixed_asset_category(*, actor, company, data, request=None):
    """Create Finance-owned fixed-asset accounting master data."""

    require_manage_finance(actor)
    models = _models()
    company = models["Company"].objects.select_for_update().get(pk=company.pk)
    _require_current_company(company)
    values = dict(data)
    unknown = set(values).difference(FIXED_ASSET_CATEGORY_EDITABLE_FIELDS | {"is_active"})
    if unknown:
        raise ValidationError(
            {field: "不是可编辑的固定资产会计类别字段。" for field in unknown}
        )
    category = models["FixedAssetCategory"](
        company=company,
        is_active=bool(values.pop("is_active", True)),
    )
    _apply(category, values, FIXED_ASSET_CATEGORY_EDITABLE_FIELDS)
    _save(category)
    _audit(
        actor=actor,
        action="fixed_asset_category_create",
        instance=category,
        new=_fixed_asset_category_snapshot(category),
        request=request,
    )
    return category


@transaction.atomic
def update_fixed_asset_category(*, actor, category, data, request=None):
    require_manage_finance(actor)
    Category = _models()["FixedAssetCategory"]
    category = Category.objects.select_for_update().select_related("company").get(
        pk=category.pk
    )
    _require_current_company(category.company)
    old = _fixed_asset_category_snapshot(category)
    _apply(category, data, FIXED_ASSET_CATEGORY_EDITABLE_FIELDS)
    _save(category)
    _audit(
        actor=actor,
        action="fixed_asset_category_update",
        instance=category,
        old=old,
        new=_fixed_asset_category_snapshot(category),
        request=request,
    )
    return category


@transaction.atomic
def deactivate_fixed_asset_category(*, actor, category, reason=None, request=None):
    require_manage_finance(actor)
    reason = _required_reason(reason)
    Category = _models()["FixedAssetCategory"]
    category = Category.objects.select_for_update().select_related("company").get(
        pk=category.pk
    )
    _require_current_company(category.company)
    if not category.is_active:
        return category
    category.is_active = False
    category.save(update_fields=["is_active", "updated_at"])
    _audit(
        actor=actor,
        action="fixed_asset_category_deactivate",
        instance=category,
        old={"is_active": True},
        new={"is_active": False, "reason": reason},
        request=request,
    )
    return category


@transaction.atomic
def delete_fixed_asset_category(*, actor, category, request=None):
    """Delete only unused categories; database PROTECT remains the final guard."""

    require_manage_finance(actor)
    Category = _models()["FixedAssetCategory"]
    category = Category.objects.select_for_update().select_related("company").get(
        pk=category.pk
    )
    _require_current_company(category.company)
    if category.asset_finances.exists():
        raise ValidationError("已被资产财务资料引用的会计类别只能停用，不得删除。")
    snapshot = _fixed_asset_category_snapshot(category)
    _audit(
        actor=actor,
        action="fixed_asset_category_delete",
        instance=category,
        old=snapshot,
        request=request,
    )
    category.delete()


@transaction.atomic
def create_depreciation_policy(*, actor, company, data, request=None):
    require_manage_finance(actor)
    company = _models()["Company"].objects.select_for_update().get(pk=company.pk)
    _require_current_company(company)
    Policy = _models()["DepreciationPolicy"]
    values = dict(data)
    unknown = set(values).difference(POLICY_EDITABLE_FIELDS | {"version", "status", "is_default"})
    if unknown:
        raise ValidationError({field: "不能在新建政策时设置。" for field in unknown})
    if values.pop("version", 1) != 1:
        raise ValidationError({"version": "新政策必须从版本 1 开始。"})
    if values.pop("status", "draft") != "draft" or values.pop("is_default", False):
        raise ValidationError("新政策必须先保存为非默认草稿。")
    key = str(values.get("policy_key", "")).strip()
    if not key:
        raise ValidationError({"policy_key": "政策稳定键不能为空。"})
    if Policy.objects.select_for_update().filter(company=company, policy_key=key).exists():
        raise ValidationError({"policy_key": "政策稳定键已存在；请克隆新版本。"})
    values["policy_key"] = key
    policy = Policy(company=company, version=1, status="draft", is_default=False, created_by=actor)
    _apply(policy, values, POLICY_EDITABLE_FIELDS)
    _validate_policy(policy)
    _save(policy)
    _audit(actor=actor, action="depreciation_policy_create", instance=policy, new=_policy_snapshot(policy), request=request)
    return policy


@transaction.atomic
def update_draft_depreciation_policy(*, actor, policy, data, request=None):
    require_manage_finance(actor)
    Policy = _models()["DepreciationPolicy"]
    policy = Policy.objects.select_for_update().select_related("company").get(pk=policy.pk)
    _require_current_company(policy.company)
    if policy.status != "draft" or policy.asset_profiles.exists():
        raise ValidationError("只有未被 Profile 使用的草稿政策可以原地修改。")
    old = _policy_snapshot(policy)
    _apply(policy, data, POLICY_EDITABLE_FIELDS)
    if policy.previous_version_id and policy.policy_key != policy.previous_version.policy_key:
        raise ValidationError({"policy_key": "克隆版本不能改变政策稳定键。"})
    _validate_policy(policy)
    _save(policy)
    _audit(actor=actor, action="depreciation_policy_update", instance=policy, old=old, new=_policy_snapshot(policy), request=request)
    return policy


@transaction.atomic
def clone_depreciation_policy(*, actor, policy, data=None, reason=None, request=None):
    require_manage_finance(actor)
    reason = _required_reason(reason)
    Policy = _models()["DepreciationPolicy"]
    source = Policy.objects.select_for_update().select_related("company").get(pk=policy.pk)
    _require_current_company(source.company)
    versions = list(Policy.objects.select_for_update().filter(company=source.company, policy_key=source.policy_key).order_by("version"))
    if not versions or versions[-1].pk != source.pk:
        raise ValidationError("只能从最新政策版本克隆，禁止形成分叉。")
    overrides = dict(data or {})
    unknown = set(overrides).difference(POLICY_EDITABLE_FIELDS - {"policy_key"})
    if unknown:
        raise ValidationError({field: "不能覆盖此克隆字段。" for field in unknown})
    values = {field: getattr(source, field) for field in POLICY_EDITABLE_FIELDS}
    values.update({"effective_from": None, "effective_to": None})
    values.update(overrides)
    clone = Policy(
        company=source.company,
        policy_key=source.policy_key,
        version=source.version + 1,
        status="draft",
        is_default=False,
        previous_version=source,
        created_by=actor,
    )
    _apply(clone, values, POLICY_EDITABLE_FIELDS)
    _validate_policy(clone)
    _save(clone)
    _audit(actor=actor, action="depreciation_policy_clone", instance=clone, new={**_policy_snapshot(clone), "reason": reason}, request=request)
    return clone


def _overlap(first_start, first_end, second_start, second_end):
    if first_start is None or second_start is None:
        return False
    return (first_end is None or second_start <= first_end) and (second_end is None or first_start <= second_end)


@transaction.atomic
def activate_depreciation_policy(*, actor, policy, make_default=False, reason=None, request=None):
    require_manage_finance(actor)
    reason = _required_reason(reason)
    models = _models()
    Policy = models["DepreciationPolicy"]
    Company = models["Company"]
    source_company_id = Policy.objects.values_list("company_id", flat=True).get(pk=policy.pk)
    company = Company.objects.select_for_update().get(pk=source_company_id)
    _require_current_company(company)
    policy = Policy.objects.select_for_update().get(pk=policy.pk)
    if policy.status != "draft":
        raise ValidationError("只有草稿政策可以启用。")
    if policy.effective_from is None:
        raise ValidationError({"effective_from": "启用政策必须填写生效日期。"})
    _validate_policy(policy)
    for other in Policy.objects.select_for_update().filter(company=company, policy_key=policy.policy_key, status="active").exclude(pk=policy.pk):
        if _overlap(policy.effective_from, policy.effective_to, other.effective_from, other.effective_to):
            raise ValidationError({"effective_from": f"与 {other.name} v{other.version} 的有效期重叠。"})
    today = _business_date()
    if make_default and not (
        policy.effective_from <= today
        and (policy.effective_to is None or today <= policy.effective_to)
    ):
        raise ValidationError("公司默认政策必须在当前上海业务日可用。")
    old = _policy_snapshot(policy)
    if make_default:
        for previous in Policy.objects.select_for_update().filter(company=company, status="active", is_default=True).exclude(pk=policy.pk):
            previous.is_default = False
            previous.save(update_fields=["is_default", "updated_at"])
    policy.status = "active"
    policy.is_default = bool(make_default)
    _save(policy, update_fields=["status", "is_default", "updated_at"])
    _audit(actor=actor, action="depreciation_policy_activate", instance=policy, old=old, new={**_policy_snapshot(policy), "reason": reason}, request=request)
    _refresh_finance_setup(company=company, actor=actor, request=request)
    return policy


@transaction.atomic
def retire_depreciation_policy(*, actor, policy, reason=None, request=None):
    """Retire an active version without changing profiles that already snapshot it."""

    require_manage_finance(actor)
    reason = _required_reason(reason)
    models = _models()
    Policy = models["DepreciationPolicy"]
    Company = models["Company"]
    company_id = Policy.objects.values_list("company_id", flat=True).get(pk=policy.pk)
    company = Company.objects.select_for_update().get(pk=company_id)
    _require_current_company(company)
    policy = Policy.objects.select_for_update().get(pk=policy.pk)
    if policy.status != "active":
        raise ValidationError("只有 active 政策可以退役。")
    referencing = list(
        models["AssetCategory"]
        .objects.select_for_update()
        .filter(company=company, default_depreciation_policy=policy)
        .values_list("code", flat=True)
    )
    if referencing:
        raise ValidationError(
            {
                "policy": "政策仍被实物分类设为当前默认："
                + "、".join(referencing)
                + "。请先为这些分类更换或清除默认政策。"
            }
        )
    old = _policy_snapshot(policy)
    policy.status = "retired"
    policy.is_default = False
    policy.save(update_fields=["status", "is_default", "updated_at"])
    _audit(
        actor=actor,
        action="depreciation_policy_retire",
        instance=policy,
        old=old,
        new={**_policy_snapshot(policy), "reason": reason},
        request=request,
    )
    _refresh_finance_setup(company=company, actor=actor, request=request)
    return policy


@transaction.atomic
def set_default_depreciation_policy(*, actor, policy, reason=None, request=None):
    require_manage_finance(actor)
    reason = _required_reason(reason)
    models = _models()
    Policy = models["DepreciationPolicy"]
    Company = models["Company"]
    company_id = Policy.objects.values_list("company_id", flat=True).get(pk=policy.pk)
    company = Company.objects.select_for_update().get(pk=company_id)
    _require_current_company(company)
    policy = Policy.objects.select_for_update().get(pk=policy.pk)
    if not _is_policy_effective(policy, _business_date()):
        raise ValidationError("只能设置当前可用的 active 政策为公司默认。")
    for previous in Policy.objects.select_for_update().filter(company=company, status="active", is_default=True).exclude(pk=policy.pk):
        previous.is_default = False
        previous.save(update_fields=["is_default", "updated_at"])
    old = policy.is_default
    policy.is_default = True
    _save(policy, update_fields=["is_default", "updated_at"])
    if not old:
        _audit(actor=actor, action="depreciation_policy_default_set", instance=policy, old={"is_default": False}, new={"is_default": True, "reason": reason}, request=request)
    _refresh_finance_setup(company=company, actor=actor, request=request)
    return policy


@transaction.atomic
def set_category_default_depreciation_policy(*, actor, category, policy, reason=None, request=None):
    require_manage_finance(actor)
    reason = _required_reason(reason)
    AssetCategory = _models()["AssetCategory"]
    category = AssetCategory.objects.select_for_update().select_related("company").get(pk=category.pk)
    _require_current_company(category.company)
    if policy is not None:
        Policy = _models()["DepreciationPolicy"]
        policy = Policy.objects.select_for_update().get(pk=policy.pk)
        if policy.company_id != category.company_id or not _is_policy_effective(policy, _business_date()):
            raise ValidationError({"policy": "分类默认政策必须同公司且当前可用。"})
    old_id = category.default_depreciation_policy_id
    category.default_depreciation_policy = policy
    category.save(update_fields=["default_depreciation_policy", "updated_at"])
    _audit(
        actor=actor,
        action="category_default_depreciation_policy_set",
        instance=category,
        old={"default_depreciation_policy_id": _serializable(old_id)},
        new={"default_depreciation_policy_id": _serializable(policy.pk if policy else None), "reason": reason},
        request=request,
    )
    return category


def resolve_depreciation_policy(*, asset, effective_date=None, requested_policy=None, lock=False):
    """Resolve the exact version: explicit -> physical category -> company."""

    Policy = _models()["DepreciationPolicy"]
    target = _business_date(effective_date)
    queryset = Policy.objects.select_for_update() if lock else Policy.objects.all()
    explicit = requested_policy
    if explicit is not None:
        explicit_id = getattr(explicit, "pk", explicit)
        try:
            candidate = queryset.get(pk=explicit_id)
        except Policy.DoesNotExist as exc:
            raise ValidationError({"depreciation_policy": "指定政策不存在。"}) from exc
        if candidate.company_id != asset.company_id or not _is_policy_effective(candidate, target):
            raise ValidationError({"depreciation_policy": "指定政策跨公司或在生效日不可用；不会静默回退。"})
        return candidate
    category = asset.category
    category_policy_id = getattr(category, "default_depreciation_policy_id", None)
    if category_policy_id:
        candidate = queryset.get(pk=category_policy_id)
        if candidate.company_id != asset.company_id or not _is_policy_effective(candidate, target):
            raise ValidationError({"depreciation_policy": "实物分类默认政策在生效日不可用。"})
        return candidate
    candidates = list(
        queryset.filter(company_id=asset.company_id, status="active", is_default=True, effective_from__lte=target)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=target))
    )
    if len(candidates) != 1:
        raise ValidationError({"depreciation_policy": "公司必须且只能解析出一个当前默认折旧政策。"})
    return candidates[0]


def _get_warning_amount(company) -> Decimal:
    SystemSetting = _models()["SystemSetting"]
    setting = SystemSetting.objects.filter(company=company, key="fixed_asset_warning_amount", value_type="decimal").first()
    if setting is None:
        return Decimal("5000.00")
    return _money(setting.value, field_name="fixed_asset_warning_amount")


def _refresh_finance_setup(*, company, actor, request=None):
    models = _models()
    Policy = models["DepreciationPolicy"]
    Setting = models["InitializationSetting"]
    SystemSetting = models["SystemSetting"]
    today = _business_date()
    defaults = list(
        Policy.objects.filter(company=company, status="active", is_default=True, effective_from__lte=today)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
    )
    warning_ok = SystemSetting.objects.filter(company=company, key="fixed_asset_warning_amount", value_type="decimal").exists()
    configured = len(defaults) == 1 and warning_ok
    if configured:
        try:
            _validate_policy(defaults[0])
        except ValidationError:
            configured = False
    setting, _ = Setting.objects.select_for_update().get_or_create(company=company)
    if setting.initialization_completed:
        return setting
    if setting.finance_rules_configured != configured:
        old = setting.finance_rules_configured
        setting.finance_rules_configured = configured
        setting.save(update_fields=["finance_rules_configured"])
        _audit(actor=actor, action="setup_finance_progress_update", instance=setting, old={"finance_rules_configured": old}, new={"finance_rules_configured": configured}, request=request)
    return setting


@transaction.atomic
def save_asset_finance_draft(*, actor, asset, data, request=None):
    require_manage_finance(actor)
    models = _models()
    Asset = models["Asset"]
    Finance = models["AssetFinance"]
    asset = Asset.objects.select_for_update().select_related("company").get(pk=asset.pk)
    _require_current_company(asset.company)
    if asset.asset_status != "pending_finance":
        raise ValidationError("只有待财务确认资产可以保存财务草稿。")
    values = dict(data)
    unknown = set(values).difference(FINANCE_DRAFT_FIELDS)
    if unknown:
        raise ValidationError({field: "不是可编辑的财务草稿字段。" for field in unknown})
    if "original_cost" in values:
        values["original_cost"] = _money(values["original_cost"], field_name="original_cost", allow_none=True)
        if values["original_cost"] is not None and values["original_cost"] < 0:
            raise ValidationError({"original_cost": "原值不得为负数。"})
    if values.get("fixed_asset_category") is not None:
        FixedAssetCategory = models["FixedAssetCategory"]
        category_id = getattr(
            values["fixed_asset_category"],
            "pk",
            values["fixed_asset_category"],
        )
        try:
            category = FixedAssetCategory.objects.select_for_update().get(
                pk=category_id,
                company=asset.company,
                is_active=True,
            )
        except FixedAssetCategory.DoesNotExist as exc:
            raise ValidationError(
                {"fixed_asset_category": "固定资产类别不存在、已停用或不属于当前公司。"}
            ) from exc
        values["fixed_asset_category"] = category
    finance, _ = Finance.objects.select_for_update().get_or_create(company=asset.company, asset=asset)
    if finance.finance_confirmed_at is not None:
        raise ValidationError("已确认财务资料不可原地修改。")
    old = {field: _serializable(getattr(finance, field)) for field in FINANCE_DRAFT_FIELDS if hasattr(finance, field)}
    _apply(finance, values, FINANCE_DRAFT_FIELDS)
    finance.full_clean()
    finance.save()
    _audit(actor=actor, action="asset_finance_draft_save", instance=finance, old=old, new={field: _serializable(getattr(finance, field)) for field in FINANCE_DRAFT_FIELDS if hasattr(finance, field)}, request=request)
    return finance


def _domain():
    from apps.finance import domain

    return domain


def _profile_spec(*, asset, finance_data, profile_data, policy):
    domain = _domain()
    values = dict(profile_data or {})
    unknown = set(values).difference(PROFILE_INPUT_FIELDS)
    if unknown:
        raise ValidationError({field: "不是折旧 Profile 输入字段。" for field in unknown})
    fixed_category = finance_data.get("fixed_asset_category")
    useful_life = values.get("useful_life_months")
    if useful_life is None and fixed_category is not None:
        useful_life = fixed_category.useful_life_months_default
    if useful_life is None:
        useful_life = policy.default_useful_life_months
    if useful_life is None or useful_life <= 0:
        raise ValidationError({"useful_life_months": "无法解析有效使用寿命。"})
    original_cost = _money(finance_data.get("original_cost"), field_name="original_cost")
    opening_ad = _money(values.get("opening_actual_accumulated_depreciation", ZERO_MONEY), field_name="opening_actual_accumulated_depreciation")
    opening_impairment = _money(values.get("opening_impairment", ZERO_MONEY), field_name="opening_impairment")
    opening_bv = values.get("opening_book_value")
    if opening_bv is None:
        opening_bv = original_cost - opening_ad - opening_impairment
    opening_bv = _money(opening_bv, field_name="opening_book_value")
    domain.validate_opening_balances(
        original_cost=original_cost,
        opening_actual_accumulated_depreciation=opening_ad,
        opening_impairment=opening_impairment,
        opening_book_value=opening_bv,
    )
    start_rule = values.get("start_rule", policy.start_rule)
    specified = values.get("specified_start", values.get("start_date"))
    allow_historical = bool(values.pop("allow_historical_start", False))
    resolved_start = domain.resolve_start_date(
        commissioning_date=asset.commissioning_date,
        start_rule=start_rule,
        specified_start=specified,
        allow_historical_override=allow_historical,
    )
    supplied_continuation = values.get("actual_continuation_date")
    has_opening_balance = opening_ad != ZERO_MONEY or opening_impairment != ZERO_MONEY
    requires_explicit_continuation = has_opening_balance or allow_historical
    if requires_explicit_continuation and supplied_continuation is None:
        raise ValidationError(
            {"actual_continuation_date": "旧资产初始化必须填写独立的实际接续日。"}
        )
    actual_continuation_date = (
        _business_date(
            supplied_continuation, field_name="actual_continuation_date"
        )
        if supplied_continuation is not None
        else resolved_start
    )
    if (
        not requires_explicit_continuation
        and actual_continuation_date != resolved_start
    ):
        raise ValidationError(
            {"actual_continuation_date": "无期初余额的新资产实际接续日必须等于折旧起算日。"}
        )
    method = values.get("method", policy.method)
    work_unit = values.get("work_unit", policy.work_unit)
    expected_total_units = values.get("expected_total_units")
    if method != "units_of_production":
        work_unit = None
        expected_total_units = None
    spec = domain.ScheduleInput(
        original_cost=original_cost,
        salvage_mode=values.get("salvage_mode", policy.default_salvage_mode),
        salvage_rate=values.get("salvage_rate", policy.default_salvage_rate),
        salvage_amount=values.get("salvage_amount", policy.default_salvage_amount),
        method=method,
        posting_period=values.get("posting_period", policy.posting_period),
        start_rule=start_rule,
        commissioning_date=asset.commissioning_date,
        useful_life_months=useful_life,
        specified_start=specified,
        annual_posting_month=values.get("annual_posting_month", policy.annual_posting_month),
        opening_actual_accumulated_depreciation=opening_ad,
        opening_impairment=opening_impairment,
        opening_book_value=opening_bv,
        actual_continuation_date=actual_continuation_date,
        expected_total_units=expected_total_units,
        work_unit=work_unit,
        allow_historical_start=allow_historical,
    )
    if method in {"units_of_production", "manual"}:
        # Future work usage/manual amount is unknowable at confirmation time.
        # Validate the parameter envelope without inventing zero inputs; these
        # two methods are calculated for one explicit period in batch service.
        salvage = domain.calculate_salvage(
            original_cost=spec.original_cost,
            salvage_mode=spec.salvage_mode,
            salvage_rate=spec.salvage_rate,
            salvage_amount=spec.salvage_amount,
        )
        start_date = resolved_start
        natural_end_date = domain.calculate_life_end(
            start_date=start_date, useful_life_months=useful_life
        )
        if actual_continuation_date < start_date:
            raise ValidationError(
                {"actual_continuation_date": "实际接续日不得早于原折旧起算日。"}
            )
        if actual_continuation_date > natural_end_date:
            raise ValidationError(
                {"actual_continuation_date": "实际接续日不得晚于原预计寿命终点。"}
            )
        if actual_continuation_date == natural_end_date and opening_bv > salvage:
            raise ValidationError(
                {
                    "actual_continuation_date": (
                        "实际接续日已到预计寿命终点，但账面价值尚未降至残值。"
                    )
                }
            )
        if method == "units_of_production":
            expected = _decimal(
                spec.expected_total_units,
                field_name="expected_total_units",
            )
            if expected <= 0:
                raise ValidationError(
                    {"expected_total_units": "工作量法预计总工作量必须大于 0。"}
                )
            if not str(spec.work_unit or "").strip():
                raise ValidationError({"work_unit": "工作量法必须填写单位。"})
        result = {
            "original_cost": original_cost,
            "salvage_value": salvage,
            "opening_book_value": opening_bv,
            "depreciable_amount": _money(
                max(opening_bv - salvage, ZERO_MONEY),
                field_name="depreciable_amount",
            ),
            "start_date": start_date,
            "actual_continuation_date": actual_continuation_date,
            "natural_end_date": natural_end_date,
            "lines": (),
            "requires_period_input": (
                "work_usage" if method == "units_of_production" else "manual_amount"
            ),
        }
        resolved_start = start_date
        resolved_continuation = actual_continuation_date
    else:
        result = domain.generate_schedule(spec)
        resolved_start = result.start_date
        resolved_continuation = result.actual_continuation_date
    effective_from = values.get("effective_from", resolved_continuation)
    if effective_from != resolved_continuation:
        raise ValidationError(
            {"effective_from": "首版 Profile 生效日必须等于实际接续日。"}
        )
    resolved = {
        "method": spec.method,
        "posting_period": spec.posting_period,
        "start_rule": spec.start_rule,
        "stop_rule": values.get("stop_rule", policy.stop_rule),
        "start_date": resolved_start,
        "actual_continuation_date": resolved_continuation,
        "useful_life_months": useful_life,
        "salvage_mode": spec.salvage_mode,
        "salvage_rate": spec.salvage_rate,
        "salvage_amount": spec.salvage_amount,
        "opening_book_value": opening_bv,
        "opening_actual_accumulated_depreciation": opening_ad,
        "opening_impairment": opening_impairment,
        "expected_total_units": spec.expected_total_units,
        "work_unit": str(spec.work_unit or "") if method == "units_of_production" else "",
        "annual_posting_month": spec.annual_posting_month,
        "effective_from": effective_from,
        "effective_to": values.get("effective_to"),
        "change_reason": values.get("change_reason", ""),
    }
    return spec, result, resolved


def preview_asset_depreciation(*, actor, asset, finance_data, profile_data=None):
    require_manage_finance(actor)
    if asset.company_id != getattr(current_company(include_inactive=True), "pk", None):
        raise PermissionDenied("目标资产不属于当前公司。")
    if asset.asset_status != "pending_finance":
        raise ValidationError("只有待财务确认资产可执行正式化前试算。")
    ensure_asset_is_depreciable(
        asset,
        finance_data=finance_data,
        require_confirmed=False,
    )
    requested = (profile_data or {}).get("depreciation_policy") or (profile_data or {}).get("depreciation_policy_id")
    policy = resolve_depreciation_policy(asset=asset, effective_date=(profile_data or {}).get("effective_from"), requested_policy=requested)
    spec, result, resolved = _profile_spec(
        asset=asset,
        finance_data=finance_data,
        profile_data=profile_data or {},
        policy=policy,
    )
    if resolved["method"] in {"units_of_production", "manual"}:
        return {
            "specification": spec,
            "summary": result,
            "resolved": resolved,
            "schedule_lines": (),
            "requires_period_input": result["requires_period_input"],
            "message": "未来期间需按期录入工作量/手工金额，批次生成时计算。",
        }
    return spec, result, resolved


def _validate_asset_physical(asset):
    errors = {}
    if asset.asset_status != "pending_finance":
        errors["asset_status"] = "资产必须处于待财务确认。"
    if asset.quantity != 1:
        errors["quantity"] = "V1 正式资产数量必须为 1。"
    for field in ("category", "department", "responsible_employee", "location"):
        obj = getattr(asset, field, None)
        if obj is None:
            errors[field] = "正式化前必须填写。"
        elif obj.company_id != asset.company_id:
            errors[field] = "必须属于同一公司。"
        elif hasattr(obj, "is_active") and not obj.is_active:
            errors[field] = "已停用，不能正式化。"
    if asset.responsible_employee_id and (
        asset.responsible_employee.employment_status != "active" or not asset.responsible_employee.is_active
    ):
        errors["responsible_employee"] = "责任人必须是在职且启用的员工。"
    if asset.location_id and asset.location.children.exists():
        errors["location"] = "正式资产必须选择叶级具体位置。"
    if errors:
        raise ValidationError(errors)


def _resolve_coding_scheme(*, asset, effective_date):
    Scheme = _models()["AssetCodingScheme"]
    queryset = Scheme.objects.select_for_update().prefetch_related("segments")
    if asset.requested_coding_scheme_id:
        scheme = queryset.get(pk=asset.requested_coding_scheme_id)
        if scheme.company_id != asset.company_id or not is_effective(scheme, effective_date):
            raise ValidationError({"coding_scheme": "指定编码方案在正式编号生效日不可用；不会静默回退。"})
        return scheme
    category_scheme_id = asset.category.default_coding_scheme_id
    if category_scheme_id:
        scheme = queryset.get(pk=category_scheme_id)
        if scheme.company_id != asset.company_id or not is_effective(scheme, effective_date):
            raise ValidationError({"coding_scheme": "实物分类默认编码方案在生效日不可用。"})
        return scheme
    schemes = list(
        queryset.filter(company=asset.company, status="active", is_default=True, effective_from__lte=effective_date)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_date))
    )
    if len(schemes) != 1:
        raise ValidationError({"coding_scheme": "公司必须且只能解析出一个生效的默认编码方案。"})
    return schemes[0]


def _insert_counter_if_missing(*, company, scheme, scope_key):
    Counter = _models()["SequenceCounter"]
    initial = scheme.sequence_start - 1
    table = connection.ops.quote_name(Counter._meta.db_table)
    now = timezone.now()
    # PostgreSQL's ON CONFLICT primitive is required for first-use concurrency.
    # The only interpolated identifier is ORM model metadata quoted by the
    # active database backend; every business value remains parameter-bound.
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (company_id, coding_scheme_id, scope_key, current_value, created_at, updated_at) "  # nosec B608
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [company.pk, scheme.pk, scope_key, initial, now, now],
        )
    return Counter.objects.select_for_update().get(company=company, coding_scheme=scheme, scope_key=scope_key)


def _issue_code(*, actor, asset, effective_date, reason, idempotency_key):
    models = _models()
    IssuedCode = models["IssuedCode"]
    History = models["AssetCodeHistory"]
    scheme = _resolve_coding_scheme(asset=asset, effective_date=effective_date)
    validate_scheme_structure(scheme)
    category_scoped = scheme.reset_mode in {"category_yearly", "category_monthly"}
    scope_key = build_scope_key(
        asset.company_id,
        scheme.pk,
        scheme.reset_mode,
        effective_date,
        category=asset.category if category_scoped else None,
        category_scope_level=(scheme.category_scope_level if category_scoped else None),
    )
    counter = _insert_counter_if_missing(company=asset.company, scheme=scheme, scope_key=scope_key)
    next_value = counter.current_value + 1
    display = render_code(
        list(scheme.segments.order_by("sequence_order")),
        {
            "company": asset.company,
            "category": asset.category,
            "department": asset.department,
            "effective_date": effective_date,
        },
        next_value,
    )
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_sequence_counter_increment', 'on', true)"
            )
    counter.current_value = next_value
    counter.save(update_fields=["current_value", "updated_at"])
    try:
        issued = IssuedCode.objects.create(
            company=asset.company,
            coding_scheme=scheme,
            scope_key=scope_key,
            sequence_value=next_value,
            display_code=display,
            normalized_code=normalize_code(display),
            effective_date=effective_date,
            effective_date_reason=reason,
            status="active",
            idempotency_key=idempotency_key,
            issued_by=actor,
        )
    except IntegrityError as exc:
        if connection.vendor == "postgresql" and getattr(
            exc.__cause__, "sqlstate", None
        ) != "23505":
            raise
        # A different scheme/version can legitimately render a code that was
        # issued in the past.  The permanent registry must keep rejecting that
        # value, but the controlled workflow should return a business error
        # and roll back the counter instead of leaking a database 500 response.
        raise ValidationError(
            {"asset_code": "生成的正式编号已被永久占用，请检查编码方案后重试。"}
        ) from exc
    History.objects.create(
        company=asset.company,
        asset=asset,
        event_type="issued",
        old_issued_code=None,
        new_issued_code=issued,
        reason="",
        effective_at=_effective_timestamp(effective_date),
        operated_by=actor,
    )
    return issued


def _create_profile_and_schedule(
    *, actor, asset, policy, resolved, result, version=1, existing_profile=None
):
    models = _models()
    Profile = models["AssetDepreciationProfile"]
    Schedule = models["DepreciationSchedule"]
    ensure_asset_is_depreciable(asset)
    if existing_profile is not None:
        profile = existing_profile
        if (
            profile.company_id != asset.company_id
            or profile.asset_id != asset.pk
            or profile.version != version
            or profile.status != "draft"
        ):
            raise ValidationError("导入的折旧 Profile 草稿状态或版本无效。")
        if profile.schedules.exists():
            raise ValidationError("导入的折旧 Profile 草稿不得预先包含折旧计划。")
    else:
        profile = Profile(
            company=asset.company,
            asset=asset,
            version=version,
            created_by=actor,
        )
    profile.depreciation_policy = policy
    profile.method = resolved["method"]
    profile.posting_period = resolved["posting_period"]
    profile.start_rule = resolved["start_rule"]
    profile.stop_rule = resolved["stop_rule"]
    profile.start_date = resolved["start_date"]
    profile.actual_continuation_date = resolved["actual_continuation_date"]
    profile.actual_continuation_review_required = False
    profile.useful_life_months = resolved["useful_life_months"]
    profile.salvage_mode = resolved["salvage_mode"]
    profile.salvage_rate = resolved["salvage_rate"]
    profile.salvage_amount = resolved["salvage_amount"]
    profile.opening_book_value = resolved["opening_book_value"]
    profile.opening_actual_accumulated_depreciation = resolved[
        "opening_actual_accumulated_depreciation"
    ]
    profile.expected_total_units = resolved["expected_total_units"]
    profile.work_unit = resolved["work_unit"] or ""
    profile.annual_posting_month = resolved["annual_posting_month"]
    profile.effective_from = resolved["effective_from"]
    profile.effective_to = resolved["effective_to"]
    profile.status = "active"
    profile.change_reason = resolved["change_reason"] or ""
    _save(profile)
    if resolved["method"] in {"units_of_production", "manual"}:
        # These methods need an explicit value in every posting period.  Their
        # Profile is confirmed now, but future Schedule rows are intentionally
        # absent so unknown inputs cannot be persisted as invented zeroes.
        return profile
    lines = []
    for line in result.lines:
        lines.append(
            Schedule(
                company=asset.company,
                asset=asset,
                depreciation_profile=profile,
                sequence_no=line.sequence_no,
                period_start=line.period_start,
                period_end=line.period_end,
                opening_book_value=line.opening_book_value,
                calculated_unrounded=line.calculated_unrounded,
                planned_amount=line.planned_amount,
                planned_accumulated=line.planned_accumulated,
                closing_book_value=line.closing_book_value,
                planned_units=line.planned_units,
                eligible_fraction=line.eligible_fraction,
                formula_snapshot_json=line.formula_snapshot,
                status="planned",
            )
        )
    Schedule.objects.bulk_create(lines)
    return profile


@transaction.atomic
def clone_asset_depreciation_profile(
    *, actor, profile, data, effective_from, reason, request=None
):
    """Create a prospective immutable Profile version and close the old one."""

    require_manage_finance(actor)
    models = _models()
    Profile = models["AssetDepreciationProfile"]
    Company = models["Company"]
    Asset = models["Asset"]
    Finance = models["AssetFinance"]
    identity = Profile.objects.values("company_id", "asset_id").get(pk=profile.pk)
    company = Company.objects.select_for_update().get(pk=identity["company_id"])
    _require_current_company(company)
    asset = _for_update_self(Asset.objects.all()).get(
        pk=identity["asset_id"], company=company
    )
    profile = _for_update_self(
        Profile.objects.select_related("depreciation_policy")
    ).get(pk=profile.pk, company=company, asset=asset)
    profile.asset = asset
    profile.company = company
    finance = Finance.objects.select_for_update().get(asset=asset, company=company)
    ensure_asset_is_depreciable(asset, finance=finance)
    _require_profile_continuation_reviewed(profile)
    if profile.status not in {"active", "suspended"}:
        raise ValidationError("只有当前 active/suspended Profile 可以克隆新版本。")
    effective_from = _business_date(effective_from, field_name="effective_from")
    if effective_from.day != 1:
        raise ValidationError({"effective_from": "普通会计估计变更只能从自然月首日生效。"})
    if effective_from <= profile.effective_from:
        raise ValidationError({"effective_from": "新版本生效日必须晚于原版本。"})
    reason = _required_reason(reason)
    next_unconfirmed = _next_unconfirmed_profile_month(
        asset=profile.asset, profile=profile, lock=True
    )
    if effective_from != next_unconfirmed:
        raise ValidationError(
            {
                "effective_from": (
                    "新版本只能从下一未确认自然月首生效；"
                    f"当前应为 {next_unconfirmed.isoformat()}。"
                )
            }
        )
    # A one-period prospective boundary is intentional: immediately closing
    # the old Profile is safe because batch/profile resolution is based on the
    # immutable effective interval, never merely on the current status flag.
    if _for_update_self(models["DepreciationBatch"].objects.filter(
        company=profile.company,
        period_start__gte=effective_from,
        batch_type="regular",
        status="confirmed",
        items__asset=profile.asset,
    )).exists():
        raise ValidationError("新版本生效日或之后已有确认批次，必须先按倒序冲销。")
    values = dict(data or {})
    forbidden_opening = {
        "opening_book_value",
        "opening_actual_accumulated_depreciation",
        "opening_impairment",
        "start_date",
        "actual_continuation_date",
        "specified_start",
        "effective_from",
        "effective_to",
        "change_reason",
    }.intersection(values)
    if forbidden_opening:
        raise ValidationError(
            {
                field: "Profile 版本的生效边界和期初余额由账面截止值计算，不接受覆盖。"
                for field in forbidden_opening
            }
        )
    requested_policy = values.get("depreciation_policy") or values.get(
        "depreciation_policy_id"
    )
    if requested_policy is not None:
        policy = resolve_depreciation_policy(
            asset=profile.asset,
            effective_date=effective_from,
            requested_policy=requested_policy,
            lock=True,
        )
    elif _is_policy_effective(profile.depreciation_policy, effective_from):
        policy = profile.depreciation_policy
    else:
        policy = resolve_depreciation_policy(
            asset=profile.asset,
            effective_date=effective_from,
            lock=True,
        )
    current_cost, current_impairment, current_ad_total, current_bv = _balances_before(
        profile.asset,
        effective_from,
        finance=finance,
        lock=True,
    )
    relevant_events = [
        item
        for item in _profile_events_snapshot(profile, lock=True)
        if date.fromisoformat(item["effective_date"]) <= effective_from
    ]
    suspensions, stop_date = _event_eligibility(
        profile, relevant_events, through_date=effective_from
    )
    old_life_end = _domain().calculate_life_end(
        start_date=profile.start_date,
        useful_life_months=profile.useful_life_months,
        suspensions=suspensions,
    )
    if stop_date is not None:
        old_life_end = min(old_life_end, stop_date)
    remaining_life = 0
    while _add_months(effective_from, remaining_life) < old_life_end:
        remaining_life += 1
    if remaining_life < 1:
        raise ValidationError("新版本生效时已无剩余有效期间，不能重启完整寿命。")
    # A new Profile is a prospective schedule over the already-reduced
    # opening BV.  Its local calculation origin is zero; the company ledger AD
    # remains available separately through cutoff Entries and is snapshotted
    # by the BatchItem.  Feeding lifetime AD together with current cost would
    # subtract historic depreciation twice.
    schedule_opening_ad = ZERO_MONEY
    schedule_opening_impairment = _money(current_cost - current_bv)
    inherited = {
        "method": profile.method,
        "posting_period": profile.posting_period,
        "start_rule": profile.start_rule,
        "stop_rule": profile.stop_rule,
        "start_date": profile.start_date,
        "actual_continuation_date": effective_from,
        "useful_life_months": remaining_life,
        "salvage_mode": profile.salvage_mode,
        "salvage_rate": profile.salvage_rate,
        "salvage_amount": profile.salvage_amount,
        "opening_actual_accumulated_depreciation": schedule_opening_ad,
        "opening_impairment": schedule_opening_impairment,
        "opening_book_value": current_bv,
        "expected_total_units": profile.expected_total_units,
        "work_unit": profile.work_unit,
        "annual_posting_month": profile.annual_posting_month,
        "effective_from": effective_from,
        "change_reason": str(reason).strip(),
    }
    inherited.update(values)
    # A prospective estimate change starts its remaining-life schedule at the
    # approved month boundary while preserving the actual opening balances.
    inherited["start_rule"] = "specified_month"
    inherited["specified_start"] = effective_from
    inherited["effective_from"] = effective_from
    inherited["change_reason"] = str(reason).strip()
    finance_data = {
        "original_cost": current_cost,
        "fixed_asset_category": finance.fixed_asset_category,
    }
    _, result, resolved = _profile_spec(
        asset=profile.asset,
        finance_data=finance_data,
        profile_data=inherited,
        policy=policy,
    )
    resolved["change_reason"] = str(reason).strip()
    old_snapshot = {
        "profile_id": str(profile.pk),
        "version": profile.version,
        "status": profile.status,
        "effective_to": _serializable(profile.effective_to),
    }
    _set_controlled_profile_status_mutation()
    updated = Profile.objects.filter(
        pk=profile.pk, status=profile.status
    ).update(status="completed", effective_to=effective_from - timedelta(days=1))
    if updated != 1:
        raise ValidationError("Profile 状态已改变，请刷新后重试。")
    profile.schedules.filter(
        status="planned", period_end__gt=effective_from
    ).update(status="superseded")
    new_profile = _create_profile_and_schedule(
        actor=actor,
        asset=profile.asset,
        policy=policy,
        resolved=resolved,
        result=result,
        version=profile.version + 1,
    )
    _audit(
        actor=actor,
        action="depreciation_profile_version_create",
        instance=new_profile,
        old=old_snapshot,
        new={
            "profile_id": str(new_profile.pk),
            "version": new_profile.version,
            "effective_from": effective_from,
            "reason": str(reason).strip(),
        },
        request=request,
    )
    return new_profile


@transaction.atomic
def review_profile_actual_continuation_date(
    *, actor, profile, actual_continuation_date, reason, request=None
):
    require_manage_finance(actor)
    models = _models()
    Profile = models["AssetDepreciationProfile"]
    Company = models["Company"]
    Asset = models["Asset"]
    Finance = models["AssetFinance"]
    Entry = models["DepreciationEntry"]
    Item = models["DepreciationBatchItem"]
    identity = Profile.objects.values("company_id", "asset_id").get(pk=profile.pk)
    company = Company.objects.select_for_update().get(pk=identity["company_id"])
    _require_current_company(company)
    asset = _for_update_self(Asset.objects.all()).get(
        pk=identity["asset_id"], company=company
    )
    profile = _for_update_self(Profile.objects.all()).get(
        pk=profile.pk, company=company, asset=asset
    )
    finance = Finance.objects.select_for_update().get(asset=asset, company=company)
    ensure_asset_is_depreciable(asset, finance=finance)
    if (
        not profile.actual_continuation_review_required
        or profile.actual_continuation_date is not None
    ):
        raise ValidationError("该 Profile 的实际接续日已经完成复核。")
    continuation = _business_date(
        actual_continuation_date, field_name="actual_continuation_date"
    )
    if continuation < profile.start_date:
        raise ValidationError(
            {"actual_continuation_date": "实际接续日不得早于原折旧起算日。"}
        )
    natural_end = _domain().calculate_life_end(
        start_date=profile.start_date,
        useful_life_months=profile.useful_life_months,
    )
    if continuation > natural_end:
        raise ValidationError(
            {"actual_continuation_date": "实际接续日不得晚于原预计寿命终点。"}
        )
    salvage = _calculate_profile_salvage(profile, finance.original_cost)
    if continuation == natural_end and profile.opening_book_value > salvage:
        raise ValidationError(
            {
                "actual_continuation_date": (
                    "实际接续日已到预计寿命终点，但账面价值尚未降至残值。"
                )
            }
        )
    prior_batch_items = _for_update_self(
        Item.objects.filter(
            asset=asset,
            batch__status__in=("confirmed", "reversed"),
            batch__period_start__lt=continuation,
        )
    )
    prior_batch_entries = _for_update_self(
        Entry.objects.filter(
            asset=asset,
            source_type="batch",
            batch_item__batch__status__in=("confirmed", "reversed"),
            period_start__lt=continuation,
        )
    )
    if prior_batch_items.exists() or prior_batch_entries.exists():
        raise ValidationError(
            "存在起始早于实际接续日的已确认或已冲销折旧期间；"
            "请先按完整历史更正方案处理后再复核。"
        )
    reason = _required_reason(reason)
    profile.actual_continuation_date = continuation
    profile.actual_continuation_review_required = False
    _save(
        profile,
        update_fields=(
            "actual_continuation_date",
            "actual_continuation_review_required",
        ),
    )
    _audit(
        actor=actor,
        action="depreciation_profile_continuation_review",
        instance=profile,
        old={"actual_continuation_date": None, "review_required": True},
        new={
            "actual_continuation_date": continuation,
            "review_required": False,
            "reason": reason,
        },
        request=request,
    )
    return profile


def _create_opening_effects(*, actor, asset, finance, profile, resolved):
    models = _models()
    Entry = models["DepreciationEntry"]
    Adjustment = models["AssetValueAdjustment"]
    opening_ad = resolved["opening_actual_accumulated_depreciation"]
    opening_impairment = resolved["opening_impairment"]
    if opening_ad:
        Entry.objects.create(
            company=asset.company,
            asset=asset,
            depreciation_profile=profile,
            entry_date=profile.actual_continuation_date,
            period_start=profile.actual_continuation_date,
            # Opening is a point-in-time carry-forward.  Entry periods are
            # nevertheless represented as non-empty half-open intervals so
            # they satisfy the common immutable ledger period invariant.
            period_end=profile.actual_continuation_date + timedelta(days=1),
            source_type="opening",
            opening_profile=profile,
            amount=opening_ad,
            accumulated_depreciation_after=opening_ad,
            book_value_after=resolved["opening_book_value"],
            posted_by=actor,
            posted_at=timezone.now(),
        )
    if opening_impairment:
        Adjustment.objects.create(
            company=asset.company,
            asset=asset,
            adjustment_type="opening_impairment",
            effective_date=profile.actual_continuation_date,
            amount=opening_impairment,
            old_values_json={"impairment": "0.00"},
            new_values_json={"impairment": str(opening_impairment)},
            reason="旧资产期初减值承接",
            status="confirmed",
            confirmed_by=actor,
            confirmed_at=timezone.now(),
            created_by=actor,
        )


def _set_controlled_asset_mutation():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('eam_lite.controlled_asset_mutation', 'on', true)")


def _set_controlled_profile_status_mutation():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_finance_profile_status', 'on', true)"
            )


def _set_controlled_finance_balance_mutation():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_finance_balance_mutation', 'on', true)"
            )


def _set_controlled_batch_reversal():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_finance_batch_reversal', 'on', true)"
            )


def _set_controlled_adjustment_reversal():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('eam_lite.controlled_finance_adjustment_reversal', 'on', true)"
            )


@transaction.atomic
def confirm_asset_finance(
    *,
    actor,
    asset,
    finance_data,
    profile_data=None,
    code_effective_date=None,
    code_effective_reason="",
    idempotency_key,
    reason=None,
    request=None,
):
    """Atomically formalize one pending asset and permanently issue its code."""

    require_manage_finance(actor)
    reason = _required_reason(reason)
    if not str(idempotency_key or "").strip():
        raise ValidationError({"idempotency_key": "正式化必须提供幂等键。"})
    models = _models()
    Asset = models["Asset"]
    Company = models["Company"]
    Finance = models["AssetFinance"]
    FormalizationRequest = models["FinanceFormalizationRequest"]
    QR = models["AssetQrIdentity"]
    IssuedCode = models["IssuedCode"]
    # Global lock order for finance workflows is Company -> Asset ->
    # Finance/Profile -> Counter.  Read the immutable company id without a row
    # lock first, then acquire both rows in that order and revalidate the link.
    asset_company_id = Asset.objects.values_list("company_id", flat=True).get(
        pk=asset.pk
    )
    company = Company.objects.select_for_update().get(pk=asset_company_id)
    _require_current_company(company)
    asset_queryset = Asset.objects.select_for_update()
    if connection.vendor == "postgresql":
        # requested_coding_scheme is nullable and therefore loaded by a LEFT
        # JOIN. PostgreSQL cannot apply a blanket FOR UPDATE to its nullable
        # side; only the Asset row belongs in this stage of the lock order.
        asset_queryset = Asset.objects.select_for_update(of=("self",))
    asset = (
        asset_queryset
        .select_related("company", "category", "department", "responsible_employee", "location", "requested_coding_scheme")
        .get(pk=asset.pk, company=company)
    )
    asset.company = company
    normalized_key = str(idempotency_key).strip()
    formalization_payload = {
        "asset_id": asset.pk,
        "finance_data": finance_data,
        "profile_data": profile_data or {},
        "code_effective_date": code_effective_date,
        "code_effective_reason": str(code_effective_reason or "").strip(),
        "reason": reason,
    }
    formalization_hash = _request_hash(formalization_payload)
    existing_for_asset = FormalizationRequest.objects.select_for_update().filter(
        asset=asset
    ).first()
    if existing_for_asset is not None:
        if (
            existing_for_asset.idempotency_key == normalized_key
            and existing_for_asset.request_hash == formalization_hash
        ):
            return asset
        raise ValidationError("该资产已经用不同参数完成正式化，不能重复确认。")
    existing_for_key = FormalizationRequest.objects.select_for_update().filter(
        company=company, idempotency_key=normalized_key
    ).first()
    if existing_for_key is not None:
        if (
            existing_for_key.asset_id == asset.pk
            and existing_for_key.request_hash == formalization_hash
        ):
            return asset
        raise ValidationError("相同幂等键已用于其他资产或不同正式化参数。")
    if asset.current_issued_code_id:
        raise ValidationError("资产已有正式编号但缺少正式化幂等结果，请停止并复核数据。")
    imported_profile_drafts = list(
        models["AssetDepreciationProfile"]
        .objects.select_for_update()
        .filter(asset=asset, company=company, status="draft")
        .order_by("version")
    )
    if len(imported_profile_drafts) > 1:
        raise ValidationError(
            "资产存在多个未确认折旧 Profile 草稿，必须先复核数据。"
        )
    _validate_asset_physical(asset)
    target_date = _business_date(code_effective_date, field_name="code_effective_date")
    today = _business_date()
    if target_date > today:
        raise ValidationError({"code_effective_date": "正式编号生效日不得为未来。"})
    if target_date < today and not str(code_effective_reason or "").strip():
        raise ValidationError({"code_effective_reason": "历史生效日期必须填写原因。"})
    values = dict(finance_data)
    unknown = set(values).difference(FINANCE_DRAFT_FIELDS)
    if unknown:
        raise ValidationError({field: "不是财务确认字段。" for field in unknown})
    treatment = values.get("accounting_treatment")
    if treatment not in {"fixed_asset", "controlled_non_fixed"}:
        raise ValidationError({"accounting_treatment": "必须明确选择固定资产或受控非固定资产。"})
    original = _money(values.get("original_cost"), field_name="original_cost")
    if original < 0:
        raise ValidationError({"original_cost": "原值不得为负数。"})
    values["original_cost"] = original
    threshold = _get_warning_amount(company)
    fixed_category = values.get("fixed_asset_category")
    fixed_category_id = (
        getattr(fixed_category, "pk", fixed_category)
        or values.get("fixed_asset_category_id")
    )
    if fixed_category_id:
        FixedAssetCategory = models["FixedAssetCategory"]
        try:
            fixed_category = FixedAssetCategory.objects.select_for_update().get(
                pk=fixed_category_id,
                company=company,
            )
        except FixedAssetCategory.DoesNotExist as exc:
            raise ValidationError(
                {"fixed_asset_category": "固定资产类别不存在或不属于当前公司。"}
            ) from exc
        values["fixed_asset_category"] = fixed_category
        values.pop("fixed_asset_category_id", None)
    policy = result = resolved = None
    if treatment == "fixed_asset":
        if fixed_category is None or fixed_category.company_id != company.pk or not fixed_category.is_active:
            raise ValidationError({"fixed_asset_category": "固定资产必须选择同公司启用的会计分类。"})
        if values.get("capitalization_date") is None or asset.commissioning_date is None:
            raise ValidationError("固定资产必须填写资本化日期和达到可使用状态日期。")
        profile_values = dict(profile_data or {})
        if imported_profile_drafts and "actual_continuation_date" not in profile_values:
            profile_values["actual_continuation_date"] = imported_profile_drafts[
                0
            ].actual_continuation_date
        requested_policy = profile_values.get("depreciation_policy") or profile_values.get("depreciation_policy_id")
        policy = resolve_depreciation_policy(asset=asset, effective_date=profile_values.get("effective_from") or target_date, requested_policy=requested_policy, lock=True)
        _, result, resolved = _profile_spec(asset=asset, finance_data={**values, "fixed_asset_category": fixed_category}, profile_data=profile_values, policy=policy)
    else:
        if fixed_category is not None:
            raise ValidationError({"fixed_asset_category": "受控非固定资产不得填写固定资产类别。"})
        if imported_profile_drafts:
            raise ValidationError(
                "资产已有固定资产折旧 Profile 草稿，不能认定为受控非固定资产。"
            )
        if original >= threshold and not str(values.get("accounting_treatment_reason", "")).strip():
            raise ValidationError({"accounting_treatment_reason": "达到提示阈值而认定为非固定资产时必须填写说明。"})
        if profile_data:
            forbidden = {key for key, value in profile_data.items() if value not in (None, "", ZERO_MONEY, 0)}
            if forbidden:
                raise ValidationError({key: "受控非固定资产不得建立折旧配置。" for key in forbidden})
    finance, _ = Finance.objects.select_for_update().get_or_create(company=company, asset=asset)
    if finance.finance_confirmed_at is not None:
        raise ValidationError("财务资料已经确认。")
    _apply(finance, values, FINANCE_DRAFT_FIELDS)
    finance.recognition_threshold_snapshot = threshold
    finance.impairment_balance_cache = (
        resolved["opening_impairment"] if treatment == "fixed_asset" else ZERO_MONEY
    )
    finance.finance_confirmed_by = actor
    finance.finance_confirmed_at = timezone.now()
    finance.full_clean()
    finance.save()
    profile = None
    if treatment == "fixed_asset":
        profile = _create_profile_and_schedule(
            actor=actor,
            asset=asset,
            policy=policy,
            resolved=resolved,
            result=result,
            existing_profile=(
                imported_profile_drafts[0] if imported_profile_drafts else None
            ),
        )
        _create_opening_effects(actor=actor, asset=asset, finance=finance, profile=profile, resolved=resolved)
    issued = _issue_code(
        actor=actor,
        asset=asset,
        effective_date=target_date,
        reason=str(code_effective_reason or "").strip(),
        idempotency_key=normalized_key,
    )
    token = secrets.token_urlsafe(32)
    qr = QR.objects.create(
        company=company,
        asset=asset,
        public_token=token,
        status="active",
        label_status="ready_to_print",
        issued_at=timezone.now(),
        issued_by=actor,
        version=1,
    )
    _set_controlled_asset_mutation()
    # Asset.save() intentionally refuses protected fields; the controlled SQL
    # update is additionally guarded by the PostgreSQL trigger/GUC.
    Asset.objects.filter(pk=asset.pk)._update(
        [
            (Asset._meta.get_field("asset_code"), None, issued.display_code),
            (Asset._meta.get_field("current_issued_code"), None, issued.pk),
            (Asset._meta.get_field("asset_status"), None, "pending_label"),
            (Asset._meta.get_field("updated_by"), None, actor.pk),
            (Asset._meta.get_field("updated_at"), None, timezone.now()),
        ]
    )
    asset.refresh_from_db()
    FormalizationRequest.objects.create(
        company=company,
        asset=asset,
        idempotency_key=normalized_key,
        request_hash=formalization_hash,
        status="completed",
        result_issued_code=issued,
        result_finance=finance,
        created_by=actor,
        completed_at=timezone.now(),
    )
    _audit(
        actor=actor,
        action="asset_finance_confirm",
        instance=finance,
        new={
            "asset_id": str(asset.pk),
            "accounting_treatment": treatment,
            "original_cost": str(original),
            "profile_id": str(profile.pk) if profile else None,
            "schedule_input_mode": (
                "period_input_required"
                if profile and profile.method in {"units_of_production", "manual"}
                else "precalculated"
            ),
            "reason": reason,
        },
        request=request,
    )
    _audit(actor=actor, action="asset_code_issue", instance=asset, new={"issued_code_id": str(issued.pk), "asset_code": issued.display_code}, request=request)
    _audit(actor=actor, action="asset_qr_identity_create", instance=asset, new={"qr_identity_id": str(qr.pk), "label_status": "ready_to_print"}, request=request)
    return asset


@transaction.atomic
def record_work_usage(*, actor, profile, period_start, period_end, current_units, work_unit, remark="", request=None):
    require_manage_finance(actor)
    models = _models()
    Profile = models["AssetDepreciationProfile"]
    Usage = models["AssetWorkUsage"]
    Company = models["Company"]
    Asset = models["Asset"]
    identity = Profile.objects.values("company_id", "asset_id").get(pk=profile.pk)
    company = Company.objects.select_for_update().get(pk=identity["company_id"])
    _require_current_company(company)
    asset = _for_update_self(Asset.objects.all()).get(
        pk=identity["asset_id"], company=company
    )
    profile = _for_update_self(Profile.objects.all()).get(
        pk=profile.pk, company=company, asset=asset
    )
    ensure_asset_is_depreciable(asset)
    profile.company = company
    profile.asset = asset
    _require_profile_continuation_reviewed(profile)
    period_start = _business_date(period_start, field_name="period_start")
    period_end = _business_date(period_end, field_name="period_end")
    applicable = (
        _for_update_self(Profile.objects.all())
        .filter(
            asset=profile.asset,
            effective_from__lt=period_end,
            actual_continuation_date__lt=period_end,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period_start))
        .order_by("-version")
        .first()
    )
    if applicable is None or applicable.pk != profile.pk:
        raise ValidationError("工作量必须写入该业务期间实际适用的 Profile 版本。")
    if profile.method != "units_of_production":
        raise ValidationError("只有工作量法 Profile 可以录入工作量。")
    if work_unit != profile.work_unit:
        raise ValidationError({"work_unit": "工作量单位必须与 Profile 一致。"})
    if period_end <= period_start:
        raise ValidationError({"period_end": "工作量期间必须是非空半开区间。"})
    if period_start.day != 1 or period_end != _add_months(period_start, 1):
        raise ValidationError("月度工作量必须对应完整自然月。")
    if _for_update_self(profile.work_usages.all()).filter(
        period_start__lt=period_end, period_end__gt=period_start
    ).exists():
        raise ValidationError("工作量期间不得重复或交叉。")
    units = _decimal(current_units, field_name="current_units")
    if units < 0:
        raise ValidationError({"current_units": "当期工作量不得为负数。"})
    previous = _for_update_self(Usage.objects.filter(
        depreciation_profile=profile, period_end__lte=period_start
    )).order_by("-period_end").first()
    opening = previous.closing_accumulated_units if previous else Decimal("0")
    closing = opening + units
    if profile.expected_total_units is None:
        raise ValidationError({"current_units": "Profile 缺少预计总工作量。"})
    requested_units = units
    if closing > profile.expected_total_units:
        units = max(profile.expected_total_units - opening, Decimal("0"))
        closing = opening + units
    following = _for_update_self(Usage.objects.filter(
        depreciation_profile=profile, period_start__gte=period_end
    )).order_by("period_start", "period_end", "pk").first()
    if (
        following is not None
        and following.opening_accumulated_units != closing
    ):
        raise ValidationError(
            "不得倒序插入会使后续累计工作量断链或重复计算的记录。"
        )
    usage = Usage(
        company=profile.company,
        asset=profile.asset,
        depreciation_profile=profile,
        period_start=period_start,
        period_end=period_end,
        work_unit=work_unit,
        opening_accumulated_units=opening,
        current_units=units,
        closing_accumulated_units=closing,
        entered_by=actor,
        entered_at=timezone.now(),
        remark=(
            f"{remark}\n录入 {requested_units} 超过剩余预计工作量，确认量封顶为 {units}。".strip()
            if requested_units != units
            else remark
        ),
    )
    _save(usage)
    _audit(actor=actor, action="asset_work_usage_record", instance=usage, new={"period_start": period_start, "period_end": period_end, "requested_units": str(requested_units), "current_units": str(units), "closing_accumulated_units": str(closing), "capped": requested_units != units}, request=request)
    return usage


def _entry_totals(asset):
    Entry = _models()["DepreciationEntry"]
    return Entry.objects.filter(asset=asset).aggregate(total=Sum("amount"))["total"] or ZERO_MONEY


def _entry_totals_before(asset, cutoff_date, *, lock=False):
    """Actual AD posted for business periods ending on/before ``cutoff_date``.

    The immutable ledger can be posted out of database insertion order.  A
    historic batch therefore must not see entries for later business periods.
    Point-in-time opening/adjustment entries use [date, date+1 day), so they
    naturally become part of the following day's opening balance.
    """

    Entry = _models()["DepreciationEntry"]
    queryset = Entry.objects.filter(asset=asset).filter(
        Q(source_type="batch", period_end__lte=cutoff_date)
        | Q(source_type="opening", entry_date__lte=cutoff_date)
        | Q(
            source_type="adjustment",
            value_adjustment__effective_date__lte=cutoff_date,
        )
    )
    if lock:
        list(_for_update_self(queryset).values_list("pk", flat=True))
    return queryset.aggregate(total=Sum("amount"))["total"] or ZERO_MONEY


def _impairment_total(asset):
    Finance = _models()["AssetFinance"]
    return Finance.objects.get(asset=asset).impairment_balance_cache


def _impairment_total_before(asset, cutoff_date, *, lock=False):
    """Rebuild impairment at a business-date cutoff from append-only facts."""

    Adjustment = _models()["AssetValueAdjustment"]
    queryset = Adjustment.objects.filter(
        asset=asset,
        effective_date__lte=cutoff_date,
        status__in=("confirmed", "reversed"),
    ).order_by("effective_date", "created_at", "id")
    if lock:
        queryset = _for_update_self(queryset)
    total = Decimal("0")
    for adjustment in queryset:
        effect = adjustment.amount
        if adjustment.adjustment_type in {"impairment_reversal"}:
            effect = -effect
        elif adjustment.adjustment_type not in {"opening_impairment", "impairment"}:
            continue
        # A reversed source remains in history, but its exact opposite
        # reversal row also exists and is the second algebraic side.
        total += effect
    return _money(total)


def _original_cost_before(asset, cutoff_date, *, finance=None, lock=False):
    """Rebuild original cost before a cutoff from current cost and later facts."""

    finance = finance or _models()["AssetFinance"].objects.get(asset=asset)
    Adjustment = _models()["AssetValueAdjustment"]
    queryset = Adjustment.objects.filter(
        asset=asset,
        adjustment_type="cost_correction",
        effective_date__gt=cutoff_date,
        status__in=("confirmed", "reversed"),
    )
    if lock:
        list(_for_update_self(queryset).values_list("pk", flat=True))
    later_effect = queryset.aggregate(total=Sum("amount"))["total"] or ZERO_MONEY
    return _money(finance.original_cost - later_effect)


def _balances_before(asset, cutoff_date, *, finance=None, lock=False):
    original = _original_cost_before(
        asset, cutoff_date, finance=finance, lock=lock
    )
    impairment = _impairment_total_before(asset, cutoff_date, lock=lock)
    accumulated = _money(_entry_totals_before(asset, cutoff_date, lock=lock))
    return original, impairment, accumulated, _money(
        original - impairment - accumulated
    )


def _batch_balance_as_of(profile, period_start):
    _require_profile_continuation_reviewed(profile)
    return max(period_start, profile.actual_continuation_date)


def _next_unconfirmed_profile_month(*, asset, profile, lock=False):
    """Return the only allowed prospective accounting-estimate boundary."""

    Entry = _models()["DepreciationEntry"]
    confirmed = Entry.objects.filter(
        asset=asset,
        batch_item__batch__batch_type="regular",
        batch_item__batch__status="confirmed",
    )
    if lock:
        list(_for_update_self(confirmed).values_list("pk", flat=True))
    last_confirmed_end = confirmed.aggregate(last=Max("period_end"))["last"]
    current_month = _business_date().replace(day=1)
    return max(
        current_month,
        last_confirmed_end or current_month,
        _add_months(profile.effective_from.replace(day=1), 1),
    )


def _unimpaired_book_value_ceiling(
    *, asset, effective_date, finance=None, lock=False
):
    """Conservative carrying amount had impairment never been recognized.

    The statutory ceiling cannot be inferred from the current impairment
    balance alone because post-impairment depreciation is normally lower.
    Rebuild every formula-computable approved Profile with zero impairment,
    deliberately ignoring suspensions (which cannot reduce hypothetical
    depreciation), and take the lower envelope.  This is conservative across
    estimate versions and can never authorize a value above ``C - actual AD``.
    Manual depreciation remains bounded by its explicit posted entries.
    """

    models = _models()
    Profile = models["AssetDepreciationProfile"]
    finance = finance or models["AssetFinance"].objects.get(asset=asset)
    target_cost = _original_cost_before(
        asset, effective_date, finance=finance, lock=lock
    )
    target_ad = _money(_entry_totals_before(asset, effective_date, lock=lock))
    candidates = [_money(max(target_cost - target_ad, ZERO_MONEY))]
    profiles = Profile.objects.filter(
        asset=asset,
        effective_from__lte=effective_date,
    ).exclude(status="draft").order_by("version")
    if lock:
        profiles = _for_update_self(profiles)
    domain = _domain()
    for profile in profiles:
        if profile.method in {"manual", "units_of_production"}:
            continue
        boundary_cost = _original_cost_before(
            asset, profile.effective_from, finance=finance, lock=lock
        )
        boundary_ad = _money(
            _entry_totals_before(asset, profile.effective_from, lock=lock)
        )
        opening_book = _money(max(boundary_cost - boundary_ad, ZERO_MONEY))
        try:
            result = domain.generate_schedule(
                domain.ScheduleInput(
                    original_cost=boundary_cost,
                    method=profile.method,
                    posting_period=profile.posting_period,
                    commissioning_date=profile.start_date,
                    start_rule="specified_date",
                    specified_start=profile.start_date,
                    useful_life_months=profile.useful_life_months,
                    salvage_mode=profile.salvage_mode,
                    salvage_rate=profile.salvage_rate,
                    salvage_amount=profile.salvage_amount,
                    annual_posting_month=profile.annual_posting_month,
                    opening_actual_accumulated_depreciation=boundary_ad,
                    opening_impairment=ZERO_MONEY,
                    opening_book_value=opening_book,
                    actual_continuation_date=profile.actual_continuation_date,
                    allow_historical_start=True,
                )
            )
        except ValueError as exc:
            raise ValidationError(
                "无法可靠重放未减值账面上限；请复核 Profile 参数。"
            ) from exc
        candidate = opening_book
        for line in result.lines:
            if effective_date <= line.period_start:
                candidate = line.opening_book_value
                break
            if line.period_start < effective_date < line.period_end:
                # Finance adjustments are month-boundary actions.  Keeping
                # the opening side here is conservative for any legacy date.
                candidate = line.opening_book_value
                break
            candidate = line.closing_book_value
        candidates.append(_money(candidate))
    return min(candidates)


def _profile_events_snapshot(profile, *, lock=False):
    Event = _models()["DepreciationProfileEvent"]
    queryset = Event.objects.filter(depreciation_profile=profile).order_by(
        "effective_date", "created_at", "id"
    )
    if lock:
        queryset = _for_update_self(queryset)
    return [
        {
            "id": str(event.pk),
            "event_type": event.event_type,
            "effective_date": event.effective_date.isoformat(),
            "source_disposal_id": (
                str(event.source_disposal_id)
                if event.source_disposal_id is not None
                else None
            ),
            "reverses_event_id": (
                str(event.reverses_event_id)
                if event.reverses_event_id is not None
                else None
            ),
        }
        for event in queryset
    ]


def _event_eligibility(profile, events, *, through_date):
    """Translate append-only events to domain suspension/stop inputs."""

    domain = _domain()
    suspensions = []
    suspended_from = None
    manual_stop_date = None
    disposal_stops = {
        event["id"]: {
            "effective_date": date.fromisoformat(event["effective_date"]),
            "source_disposal_id": event.get("source_disposal_id"),
        }
        for event in events
        if event["event_type"] == "disposal_stop"
    }
    restored_stop_ids = set()
    for event in events:
        if event["event_type"] != "disposal_restore":
            continue
        reversed_id = event.get("reverses_event_id")
        if not reversed_id or reversed_id not in disposal_stops:
            raise ValidationError("Profile 处置停止/恢复事件链无效。")
        if reversed_id in restored_stop_ids:
            raise ValidationError("同一处置停止事件存在重复恢复记录。")
        reversed_stop = disposal_stops[reversed_id]
        if (
            date.fromisoformat(event["effective_date"])
            != reversed_stop["effective_date"]
            or event.get("source_disposal_id")
            != reversed_stop["source_disposal_id"]
        ):
            raise ValidationError("处置恢复事件必须与原停止事件及来源处置完全一致。")
        restored_stop_ids.add(reversed_id)

    for event in events:
        effective = date.fromisoformat(event["effective_date"])
        if event["event_type"] == "suspend":
            if suspended_from is not None:
                raise ValidationError("Profile 暂停事件链无效。")
            suspended_from = effective
        elif event["event_type"] == "resume":
            if suspended_from is None or effective <= suspended_from:
                raise ValidationError("Profile 恢复事件链无效。")
            suspensions.append(domain.Period(suspended_from, effective))
            suspended_from = None
        elif event["event_type"] == "stop":
            manual_stop_date = domain.resolve_stop_date(
                event_date=effective, stop_rule=profile.stop_rule
            )
        elif event["event_type"] not in {
            "disposal_stop",
            "disposal_restore",
        }:
            raise ValidationError("Profile 包含无法识别的折旧事件类型。")

    active_disposal_stops = [
        stop["effective_date"]
        for event_id, stop in disposal_stops.items()
        if event_id not in restored_stop_ids
    ]
    if len(active_disposal_stops) > 1:
        raise ValidationError("Profile 同时存在多个未恢复的处置停止事件。")
    if manual_stop_date is not None and active_disposal_stops:
        raise ValidationError("Profile 同时存在人工停止和处置停止事件。")
    stop_date = manual_stop_date
    if stop_date is None and active_disposal_stops:
        stop_date = active_disposal_stops[0]
    if suspended_from is not None:
        suspension_end = max(through_date, suspended_from + timedelta(days=1))
        suspensions.append(domain.Period(suspended_from, suspension_end))
    return tuple(suspensions), stop_date


def _batch_work_usages(
    *, Usage, profile, period_start, period_end, lock=False
):
    accounting_window_start = (
        _add_months(period_end, -12)
        if profile.posting_period == "yearly"
        else period_start
    )
    if profile.posting_period == "yearly":
        window_start = max(
            accounting_window_start, profile.actual_continuation_date
        )
        queryset = Usage.objects.filter(
            depreciation_profile=profile,
            period_start__lt=period_end,
            period_end__gt=window_start,
            period_end__lte=period_end,
        )
    else:
        queryset = Usage.objects.filter(
            depreciation_profile=profile,
            period_start=period_start,
            period_end=period_end,
        )
    queryset = queryset.order_by("period_start", "period_end", "pk")
    if lock:
        queryset = _for_update_self(queryset)
    usages = list(queryset)
    if any(
        usage.period_start.day != 1
        or usage.period_end != _add_months(usage.period_start, 1)
        for usage in usages
    ):
        raise ValidationError("工作量必须对应年度计提窗口内的完整自然月。")
    return usages


def _batch_item_source_snapshot(
    *, profile, period_start, period_end, usages=(), finance=None, lock=False
):
    Adjustment = _models()["AssetValueAdjustment"]
    balance_as_of = _batch_balance_as_of(profile, period_start)
    events = _profile_events_snapshot(profile, lock=lock)
    adjustments = Adjustment.objects.filter(
        asset=profile.asset,
        status__in=("confirmed", "reversed"),
        effective_date__lte=balance_as_of,
    ).order_by("effective_date", "created_at", "id")
    if lock:
        adjustments = _for_update_self(adjustments)
    entries = _models()["DepreciationEntry"].objects.filter(
        asset=profile.asset
    ).filter(
        Q(source_type="batch", period_end__lte=balance_as_of)
        | Q(source_type="opening", entry_date__lte=balance_as_of)
        | Q(
            source_type="adjustment",
            value_adjustment__effective_date__lte=balance_as_of,
        )
    ).order_by("period_end", "created_at", "id")
    if lock:
        entries = _for_update_self(entries)
    cutoff_cost, cutoff_impairment, cutoff_ad, cutoff_bv = _balances_before(
        profile.asset,
        balance_as_of,
        finance=finance or profile.asset.finance,
        lock=lock,
    )
    payload = {
        "engine_version": DEPRECIATION_ENGINE_VERSION,
        "profile": {
            "id": str(profile.pk),
            "version": profile.version,
            "status": profile.status,
            "effective_from": profile.effective_from,
            "effective_to": profile.effective_to,
            "method": profile.method,
            "actual_continuation_date": profile.actual_continuation_date,
        },
        "events": events,
        "adjustments": [
            {
                "id": str(item.pk),
                "status": item.status,
                "type": item.adjustment_type,
                "amount": item.amount,
                "effective_date": item.effective_date,
            }
            for item in adjustments
        ],
        "entries": [
            {
                "id": str(item.pk),
                "amount": item.amount,
                "period_start": item.period_start,
                "period_end": item.period_end,
            }
            for item in entries
        ],
        "finance": {
            "original_cost": cutoff_cost,
            "impairment": cutoff_impairment,
            "accumulated_depreciation": cutoff_ad,
            "book_value": cutoff_bv,
        },
        "usages": [
            {
                "id": str(usage.pk),
                "period_start": usage.period_start,
                "period_end": usage.period_end,
                "current_units": usage.current_units,
                "closing_units": usage.closing_accumulated_units,
                "entered_at": usage.entered_at,
            }
            for usage in usages
        ],
        "balance_as_of": balance_as_of,
        "period_start": period_start,
        "period_end": period_end,
    }
    return payload, _request_hash(payload)


@transaction.atomic
def generate_depreciation_batch(*, actor, company, period_start, period_end, idempotency_key, manual_inputs=None, request=None):
    require_manage_finance(actor)
    models = _models()
    Company = models["Company"]
    Batch = models["DepreciationBatch"]
    Item = models["DepreciationBatchItem"]
    Profile = models["AssetDepreciationProfile"]
    Schedule = models["DepreciationSchedule"]
    Usage = models["AssetWorkUsage"]
    company = Company.objects.select_for_update().get(pk=company.pk)
    _require_current_company(company)
    if period_start.day != 1 or period_end != _add_months(period_start, 1):
        raise ValidationError("regular 月批次必须覆盖完整自然月。")
    payload = {"period_start": period_start, "period_end": period_end, "manual_inputs": manual_inputs or {}}
    digest = _request_hash(payload)
    existing = Batch.objects.select_for_update().filter(company=company, idempotency_key=idempotency_key).first()
    if existing:
        if existing.request_hash != digest:
            raise ValidationError("相同幂等键的批次参数不同。")
        return existing
    generation = (Batch.objects.filter(company=company, period_start=period_start, batch_type="regular").order_by("-generation_no").values_list("generation_no", flat=True).first() or 0) + 1
    previous = Batch.objects.filter(company=company, period_start=period_start, batch_type="regular", status="reversed").order_by("-generation_no").first()
    batch = Batch.objects.create(
        company=company,
        period_start=period_start,
        period_end=period_end,
        generation_no=generation,
        batch_type="regular",
        status="draft",
        idempotency_key=idempotency_key,
        request_hash=digest,
        generated_by=actor,
        generated_at=timezone.now(),
        supersedes_batch=previous,
    )
    manual_inputs = manual_inputs or {}
    profiles = (
        _for_update_self(Profile.objects.all())
        .select_related("asset", "asset__finance")
        .filter(
            company=company,
            status__in=("active", "suspended", "stopped", "completed"),
            effective_from__lt=period_end,
        )
        .filter(depreciable_fixed_asset_filter("asset"))
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period_start))
    )
    for profile in profiles:
        _require_profile_continuation_reviewed(profile)
        balance_as_of = _batch_balance_as_of(profile, period_start)
        cutoff_cost, cutoff_impairment, cutoff_ad, cutoff_book = _balances_before(
            profile.asset,
            balance_as_of,
            finance=profile.asset.finance,
            lock=True,
        )
        event_snapshot = _profile_events_snapshot(profile)
        suspensions, stop_date = _event_eligibility(
            profile, event_snapshot, through_date=period_end
        )
        natural_end = _domain().calculate_life_end(
            start_date=profile.start_date,
            useful_life_months=profile.useful_life_months,
            suspensions=suspensions,
        )
        eligibility_end = min(
            value for value in (natural_end, stop_date, period_end) if value is not None
        )
        active_intervals = _domain().active_intervals(
            start_date=profile.actual_continuation_date,
            end_date=eligibility_end,
            suspensions=suspensions,
        )
        eligibility_period_start = (
            _add_months(period_end, -12)
            if profile.posting_period == "yearly"
            else period_start
        )
        period_fraction = _domain().eligible_fraction(
            _domain().Period(eligibility_period_start, period_end),
            active_intervals,
        )
        schedule = Schedule.objects.filter(
            depreciation_profile=profile,
            period_start__lt=period_end,
            period_end__gt=period_start,
            status="planned",
        ).order_by("period_start").first()
        status = "ready"
        error = ""
        skip_reason = ""
        planned = ZERO_MONEY
        raw = Decimal("0")
        usage_units = None
        manual_amount = manual_reason = manual_by = manual_at = None
        usages = []
        if period_fraction == 0:
            status = "skipped"
            skip_reason = "折旧事件/寿命规则下当期无资格"
        elif profile.posting_period == "yearly" and profile.annual_posting_month != period_start.month:
            status = "skipped"
            skip_reason = "非年度计提月份"
        elif profile.method == "no_depreciation":
            status = "skipped"
            skip_reason = "不计提折旧"
        elif profile.method == "manual":
            value = manual_inputs.get(str(profile.asset_id), manual_inputs.get(profile.asset_id))
            if value is None:
                status, error = "error", "手工折旧金额和原因缺失"
            else:
                manual_amount = _money(value.get("amount"), field_name="manual_amount")
                manual_reason = str(value.get("reason", "")).strip()
                if not manual_reason:
                    raise ValidationError({"manual_reason": "手工折旧即使为 0 也必须填写原因。"})
                manual_by, manual_at = actor, timezone.now()
                planned = manual_amount
                raw = manual_amount
        elif profile.method == "units_of_production":
            usages = _batch_work_usages(
                Usage=Usage,
                profile=profile,
                period_start=period_start,
                period_end=period_end,
            )
            if not usages:
                status, error = "error", "缺少财务明确录入的当期工作量"
            else:
                usage_units = sum(
                    (usage.current_units for usage in usages), Decimal("0")
                )
                original_cost = cutoff_cost
                floor = _calculate_profile_salvage(profile, original_cost)
                base = max(original_cost - floor, ZERO_MONEY)
                raw = _domain().units_of_production_raw(
                    depreciable_amount=base,
                    expected_total_units=profile.expected_total_units,
                    current_units=usage_units,
                )
                # The final total-unit period consumes the exact remaining DB;
                # all other periods follow two-decimal HALF_UP posting.
                if usages[-1].closing_accumulated_units >= profile.expected_total_units:
                    planned = None  # resolved from current DB below
                else:
                    planned = _domain().post_depreciation(
                        calculated_unrounded=raw,
                        depreciable_balance_before=base,
                    )
        else:
            # Preserve full remaining-life context.  In particular, DDB needs
            # all future periods to decide its one-way switch to straight-line
            # and SYD needs the complete annual target; period_end is not a
            # synthetic stop event.
            profile_opening_impairment = max(
                cutoff_cost
                - profile.opening_actual_accumulated_depreciation
                - profile.opening_book_value,
                ZERO_MONEY,
            )
            event_spec = _domain().ScheduleInput(
                original_cost=cutoff_cost,
                method=profile.method,
                posting_period=profile.posting_period,
                commissioning_date=profile.start_date,
                start_rule="specified_date",
                specified_start=profile.start_date,
                useful_life_months=profile.useful_life_months,
                salvage_mode=profile.salvage_mode,
                salvage_rate=profile.salvage_rate,
                salvage_amount=profile.salvage_amount,
                annual_posting_month=profile.annual_posting_month,
                opening_actual_accumulated_depreciation=profile.opening_actual_accumulated_depreciation,
                opening_impairment=profile_opening_impairment,
                opening_book_value=profile.opening_book_value,
                actual_continuation_date=profile.actual_continuation_date,
                suspensions=suspensions,
                stop_date=stop_date,
            )
            event_result = _domain().generate_schedule(event_spec)
            event_line = next(
                (
                    line
                    for line in event_result.lines
                    if line.period_start < period_end
                    and line.period_end > period_start
                ),
                None,
            )
            if event_line is None:
                status = "skipped"
                skip_reason = "事件及剩余寿命计算后当期无可计提行"
            else:
                planned, raw = event_line.planned_amount, event_line.calculated_unrounded
                period_fraction = event_line.eligible_fraction
        opening = cutoff_book
        floor = _calculate_profile_salvage(profile, cutoff_cost)
        db = max(opening - floor, ZERO_MONEY)
        if profile.method == "units_of_production" and status == "ready" and planned is None:
            planned = db
        if planned > db:
            if profile.method == "manual":
                raise ValidationError(
                    {"manual_amount": "手工折旧金额不得超过当期剩余可折旧金额。"}
                )
            planned = db
        source_snapshot, source_hash = _batch_item_source_snapshot(
            profile=profile,
            period_start=period_start,
            period_end=period_end,
            usages=usages,
        )
        Item.objects.create(
            company=company,
            batch=batch,
            asset=profile.asset,
            depreciation_profile=profile,
            depreciation_schedule=schedule,
            calculation_method=profile.method,
            opening_book_value=opening,
            depreciable_floor=floor,
            eligible_fraction=period_fraction,
            usage_units=usage_units,
            manual_amount=manual_amount,
            manual_reason=manual_reason or "",
            manual_entered_by=manual_by,
            manual_entered_at=manual_at,
            calculated_unrounded=raw,
            planned_amount=planned,
            closing_book_value=opening - planned,
            calculation_snapshot_json={
                "engine_version": DEPRECIATION_ENGINE_VERSION,
                "profile_version": profile.version,
                "schedule_id": str(schedule.pk) if schedule else None,
                "skip_reason": skip_reason,
                "period_input_mode": (
                    "work_usage" if profile.method == "units_of_production"
                    else "manual_amount" if profile.method == "manual"
                    else "precalculated"
                ),
                "source_snapshot_hash": source_hash,
                "source_snapshot": _serializable(source_snapshot),
            },
            status=status,
            error_message=error,
        )
    _audit(actor=actor, action="depreciation_batch_generate", instance=batch, new={"period_start": period_start, "generation_no": generation, "item_count": batch.items.count()}, request=request)
    return batch


def _add_months(value, months):
    domain = _domain()
    return domain.add_months_safe(value, months)


def _calculate_profile_salvage(profile, original_cost):
    domain = _domain()
    return domain.calculate_salvage(
        original_cost=original_cost,
        salvage_mode=profile.salvage_mode,
        salvage_rate=profile.salvage_rate,
        salvage_amount=profile.salvage_amount,
    )


@transaction.atomic
def confirm_depreciation_batch(*, actor, batch, reason=None, request=None):
    require_manage_finance(actor)
    reason = _required_reason(reason)
    models = _models()
    Batch = models["DepreciationBatch"]
    Item = models["DepreciationBatchItem"]
    Entry = models["DepreciationEntry"]
    Asset = models["Asset"]
    Profile = models["AssetDepreciationProfile"]
    Finance = models["AssetFinance"]
    Company = models["Company"]
    batch_id = batch.pk
    company_id = Batch.objects.values_list("company_id", flat=True).get(pk=batch_id)
    company = Company.objects.select_for_update().get(pk=company_id)
    _require_current_company(company)

    # All finance mutations use the same coarse-to-fine order.  Read only the
    # immutable identities first, then lock Company -> Asset -> Profile ->
    # Finance -> Batch -> BatchItem.  The Company lock serializes supported
    # service operations; the identity comparison below also rejects a direct
    # concurrent draft-item change instead of confirming a mixed snapshot.
    expected_scope = list(
        Item.objects.filter(batch_id=batch_id, company=company)
        .order_by("asset_id", "depreciation_profile_id", "pk")
        .values_list("asset_id", "depreciation_profile_id", "pk")
    )
    asset_ids = sorted({row[0] for row in expected_scope}, key=str)
    profile_ids = sorted({row[1] for row in expected_scope}, key=str)
    list(
        _for_update_self(
            Asset.objects.filter(company=company, pk__in=asset_ids).order_by("pk")
        )
    )
    list(
        _for_update_self(
            Profile.objects.filter(
                company=company, pk__in=profile_ids
            ).order_by("pk")
        )
    )
    locked_finances = {
        finance.asset_id: finance
        for finance in Finance.objects.select_for_update()
        .filter(company=company, asset_id__in=asset_ids)
        .order_by("asset_id")
    }
    batch = Batch.objects.select_for_update().select_related("company").get(
        pk=batch_id, company=company
    )
    if batch.status == "confirmed":
        return batch
    if batch.status != "draft" or batch.batch_type != "regular":
        raise ValidationError("只有 regular 草稿批次可以确认。")
    item_queryset = batch.items.select_for_update()
    if connection.vendor == "postgresql":
        # ``depreciation_schedule`` is nullable.  Django/PostgreSQL may include
        # a LEFT JOIN while following the related graph; an unqualified FOR
        # UPDATE would then try to lock the nullable side and fail before any
        # business validation.  Only BatchItem rows need this lock here.
        item_queryset = item_queryset.select_for_update(of=("self",))
    items = list(
        item_queryset
        .select_related("asset", "asset__finance", "depreciation_profile")
        .order_by("asset_id")
    )
    actual_scope = sorted(
        (
            item.asset_id,
            item.depreciation_profile_id,
            item.pk,
        )
        for item in items
    )
    if actual_scope != sorted(expected_scope):
        raise ValidationError("批次明细在确认锁定期间已变化；请重新生成批次。")
    if any(item.status == "error" for item in items):
        raise ValidationError("批次存在错误明细，不能确认。")
    if Entry.objects.select_for_update().filter(
        asset_id__in=[item.asset_id for item in items],
        batch_item__batch__batch_type="regular",
        batch_item__batch__status="confirmed",
        batch_item__batch__period_start__gt=batch.period_start,
    ).exists():
        raise ValidationError("存在后续已确认月份，不得倒序补记并破坏余额链；请先倒序冲销。")
    Usage = models["AssetWorkUsage"]
    for item in items:
        ensure_asset_is_depreciable(
            item.asset,
            finance=locked_finances.get(item.asset_id),
        )
        _require_profile_continuation_reviewed(item.depreciation_profile)
        usages = []
        if item.calculation_method == "units_of_production":
            usages = _batch_work_usages(
                Usage=Usage,
                profile=item.depreciation_profile,
                period_start=batch.period_start,
                period_end=batch.period_end,
                lock=True,
            )
        _snapshot, current_hash = _batch_item_source_snapshot(
            profile=item.depreciation_profile,
            period_start=batch.period_start,
            period_end=batch.period_end,
            usages=usages,
            finance=locked_finances.get(item.asset_id),
            lock=True,
        )
        expected_hash = item.calculation_snapshot_json.get("source_snapshot_hash")
        if not expected_hash or current_hash != expected_hash:
            raise ValidationError(
                "批次生成后 Profile事件、价值调整、工作量或实际分录已变化；请取消草稿并重新生成。"
            )
    active = Batch.objects.select_for_update().filter(company=batch.company, period_start=batch.period_start, batch_type="regular", status="confirmed").exclude(pk=batch.pk)
    if active.exists():
        raise ValidationError("同公司同期间已有未冲销的 confirmed regular 批次。")
    for item in items:
        if item.status != "ready":
            continue
        balance_as_of = _batch_balance_as_of(
            item.depreciation_profile, batch.period_start
        )
        original, impairment, accumulated_before, _opening = _balances_before(
            item.asset,
            balance_as_of,
            finance=locked_finances[item.asset_id],
            lock=True,
        )
        db = max(original - impairment - accumulated_before - item.depreciable_floor, ZERO_MONEY)
        amount = min(_money(item.planned_amount), db)
        accumulated_after = accumulated_before + amount
        book_after = original - impairment - accumulated_after
        Entry.objects.create(
            company=batch.company,
            asset=item.asset,
            depreciation_profile=item.depreciation_profile,
            entry_date=batch.period_end,
            period_start=batch.period_start,
            period_end=batch.period_end,
            source_type="batch",
            batch_item=item,
            amount=amount,
            accumulated_depreciation_after=accumulated_after,
            book_value_after=book_after,
            posted_by=actor,
            posted_at=timezone.now(),
        )
    batch.status = "confirmed"
    batch.confirmed_by = actor
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    _audit(actor=actor, action="depreciation_batch_confirm", instance=batch, new={"status": "confirmed", "entry_count": Entry.objects.filter(batch_item__batch=batch).count(), "reason": reason}, request=request)
    return batch


@transaction.atomic
def reverse_depreciation_batch(*, actor, batch, reason, idempotency_key, request=None):
    require_manage_finance(actor)
    if not str(reason or "").strip():
        raise ValidationError({"reason": "批次冲销必须填写原因。"})
    models = _models()
    Batch = models["DepreciationBatch"]
    Item = models["DepreciationBatchItem"]
    Entry = models["DepreciationEntry"]
    Profile = models["AssetDepreciationProfile"]
    Company = models["Company"]
    source_id = batch.pk
    company_id = Batch.objects.values_list("company_id", flat=True).get(pk=source_id)
    company = Company.objects.select_for_update().get(pk=company_id)
    _require_current_company(company)
    batch = Batch.objects.select_for_update().select_related("company").get(
        pk=source_id, company=company
    )
    expected_hash = _request_hash({"batch_id": batch.pk, "reason": reason})
    existing_reversal = Batch.objects.select_for_update().filter(
        reverses_batch=batch
    ).first()
    if existing_reversal is not None:
        if (
            existing_reversal.idempotency_key == idempotency_key
            and existing_reversal.request_hash == expected_hash
            and existing_reversal.status == "confirmed"
        ):
            return existing_reversal
        raise ValidationError("该批次已有不同请求产生的永久冲销结果。")
    existing = Batch.objects.select_for_update().filter(
        company=company, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        raise ValidationError("相同幂等键已用于其他冲销。")
    if batch.status != "confirmed" or batch.batch_type != "regular":
        raise ValidationError("只能冲销 confirmed regular 批次。")
    source_asset_ids = list(
        batch.items.values_list("asset_id", flat=True)
    )
    if Entry.objects.select_for_update().filter(
        asset_id__in=source_asset_ids,
        batch_item__batch__batch_type="regular",
        batch_item__batch__status="confirmed",
        batch_item__batch__period_start__gt=batch.period_start,
    ).exists():
        raise ValidationError("存在依赖该期期初余额的后续已确认月份；必须从最后期向前按顺序冲销。")
    source_versions = batch.items.values_list(
        "asset_id", "depreciation_profile__version"
    )
    later_profile_scope = Q(pk__isnull=True)
    for asset_id, version in source_versions:
        later_profile_scope |= Q(asset_id=asset_id, version__gt=version)
    if _for_update_self(
        Profile.objects.filter(later_profile_scope, effective_from__gte=batch.period_end)
    ).exists():
        raise ValidationError(
            "存在依赖该期账面余额建立的后续折旧 Profile；必须先撤销后续版本或按完整更正方案处理。"
        )
    reversal = Batch.objects.create(
        company=batch.company,
        period_start=batch.period_start,
        period_end=batch.period_end,
        generation_no=batch.generation_no,
        batch_type="reversal",
        status="draft",
        idempotency_key=idempotency_key,
        request_hash=expected_hash,
        generated_by=actor,
        generated_at=timezone.now(),
        confirmed_by=None,
        confirmed_at=None,
        reverses_batch=batch,
        reversal_reason=str(reason).strip(),
    )
    originals = list(Entry.objects.select_for_update().filter(batch_item__batch=batch).select_related("asset", "depreciation_profile", "batch_item").order_by("asset_id"))
    for original in originals:
        manual_kwargs = {}
        if original.batch_item.calculation_method == "manual":
            manual_kwargs = {
                "manual_amount": original.amount,
                "manual_reason": f"冲销原手工折旧：{reason}",
                "manual_entered_by": actor,
                "manual_entered_at": timezone.now(),
            }
        item = Item.objects.create(
            company=batch.company,
            batch=reversal,
            asset=original.asset,
            depreciation_profile=original.depreciation_profile,
            depreciation_schedule=original.batch_item.depreciation_schedule,
            calculation_method=original.batch_item.calculation_method,
            opening_book_value=original.book_value_after,
            depreciable_floor=original.batch_item.depreciable_floor,
            eligible_fraction=original.batch_item.eligible_fraction,
            calculated_unrounded=original.amount,
            planned_amount=original.amount,
            closing_book_value=original.book_value_after + original.amount,
            calculation_snapshot_json={"reversal_of_entry_id": str(original.pk)},
            status="ready",
            **manual_kwargs,
        )
        current_ad = _entry_totals(original.asset)
        new_ad = current_ad - original.amount
        finance = original.asset.finance
        book = finance.original_cost - finance.impairment_balance_cache - new_ad
        Entry.objects.create(
            company=batch.company,
            asset=original.asset,
            depreciation_profile=original.depreciation_profile,
            entry_date=timezone.localdate(),
            period_start=batch.period_start,
            period_end=batch.period_end,
            source_type="batch",
            batch_item=item,
            amount=-original.amount,
            accumulated_depreciation_after=new_ad,
            book_value_after=book,
            reversal_of=original,
            posted_by=actor,
            posted_at=timezone.now(),
        )
    # Fill the reversal while its parent is draft, then close both sides in
    # one transaction.  PostgreSQL deferred guards validate the final pair.
    reversal.status = "confirmed"
    reversal.confirmed_by = actor
    reversal.confirmed_at = timezone.now()
    reversal.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    _set_controlled_batch_reversal()
    batch.status = "reversed"
    batch.save(update_fields=["status"])
    _audit(actor=actor, action="depreciation_batch_reverse", instance=reversal, new={"reverses_batch_id": str(batch.pk), "reason": str(reason).strip()}, request=request)
    return reversal


@transaction.atomic
def create_profile_event(*, actor, profile, event_type, effective_date, reason, request=None):
    require_manage_finance(actor)
    if event_type not in {"suspend", "resume", "stop"}:
        raise ValidationError({"event_type": "Sprint 4 仅支持 suspend/resume/stop。"})
    if not str(reason or "").strip():
        raise ValidationError({"reason": "折旧状态事件必须填写原因。"})
    models = _models()
    Profile = models["AssetDepreciationProfile"]
    Event = models["DepreciationProfileEvent"]
    Company = models["Company"]
    Asset = models["Asset"]
    identity = Profile.objects.values("company_id", "asset_id").get(pk=profile.pk)
    company = Company.objects.select_for_update().get(pk=identity["company_id"])
    _require_current_company(company)
    asset = _for_update_self(Asset.objects.all()).get(
        pk=identity["asset_id"], company=company
    )
    profile = _for_update_self(Profile.objects.all()).get(
        pk=profile.pk, company=company, asset=asset
    )
    ensure_asset_is_depreciable(asset)
    profile.company = company
    profile.asset = asset
    _require_profile_continuation_reviewed(profile)
    latest = _for_update_self(Event.objects.filter(
        depreciation_profile=profile
    )).order_by("-effective_date", "-created_at").first()
    if latest and effective_date <= latest.effective_date:
        raise ValidationError({"effective_date": "事件日期必须严格晚于上一事件。"})
    allowed = {"suspend": {"active"}, "resume": {"suspended"}, "stop": {"active", "suspended"}}
    if profile.status not in allowed[event_type]:
        raise ValidationError("当前 Profile 状态不允许该事件。")
    if _for_update_self(models["DepreciationEntry"].objects.filter(
        asset=profile.asset,
        batch_item__batch__batch_type="regular",
        batch_item__batch__status="confirmed",
        batch_item__batch__period_end__gt=effective_date,
    )).exists():
        raise ValidationError("事件生效日所在或之后已有确认折旧批次；请先按顺序冲销。")
    event = Event.objects.create(
        company=profile.company,
        asset=profile.asset,
        depreciation_profile=profile,
        event_type=event_type,
        effective_date=effective_date,
        reason=str(reason).strip(),
        created_by=actor,
    )
    target_status = {
        "suspend": "suspended",
        "resume": "active",
        "stop": "stopped",
    }[event_type]
    _set_controlled_profile_status_mutation()
    # Avoid the ordinary model save path: PostgreSQL accepts exactly one
    # approved state transition after the transaction-local GUC is set and
    # immediately consumes that permission.
    updated = Profile.objects.filter(pk=profile.pk, status=profile.status).update(
        status=target_status
    )
    if updated != 1:
        raise ValidationError("折旧 Profile 状态已变更，请刷新后重试。")
    _audit(actor=actor, action=f"depreciation_profile_{event_type}", instance=event, new={"effective_date": effective_date, "reason": reason}, request=request)
    return event


@transaction.atomic
def create_value_adjustment(*, actor, asset, adjustment_type, amount, effective_date, reason, request=None):
    require_manage_finance(actor)
    if adjustment_type not in {"impairment", "impairment_reversal", "cost_correction", "depreciation_adjustment"}:
        raise ValidationError({"adjustment_type": "不支持的价值调整类型。"})
    if not str(reason or "").strip():
        raise ValidationError({"reason": "价值调整必须填写原因。"})
    models = _models()
    Asset = models["Asset"]
    Finance = models["AssetFinance"]
    Adjustment = models["AssetValueAdjustment"]
    Entry = models["DepreciationEntry"]
    Company = models["Company"]
    asset_company_id = Asset.objects.values_list("company_id", flat=True).get(pk=asset.pk)
    company = Company.objects.select_for_update().get(pk=asset_company_id)
    _require_current_company(company)
    asset = _for_update_self(Asset.objects.all()).get(pk=asset.pk, company=company)
    asset.company = company
    finance = Finance.objects.select_for_update().get(asset=asset, company=company)
    ensure_asset_is_depreciable(asset, finance=finance)
    effective_date = _business_date(effective_date)
    if (
        adjustment_type == "depreciation_adjustment"
        and effective_date > _business_date()
    ):
        raise ValidationError({"effective_date": "价值调整不得以未来业务日期提前入账。"})
    if (
        adjustment_type in {"impairment", "impairment_reversal", "cost_correction"}
        and effective_date.day != 1
    ):
        raise ValidationError(
            {"effective_date": "减值、减值转回和原值更正只能从未确认自然月首生效。"}
        )
    current_profile = _for_update_self(
        asset.depreciation_profiles.filter(status__in=("active", "suspended"))
    ).order_by("-version").first()
    if current_profile is None:
        raise ValidationError("价值调整找不到当前折旧 Profile。")
    required_boundary = _next_unconfirmed_profile_month(
        asset=asset, profile=current_profile, lock=True
    )
    if (
        adjustment_type in {"impairment", "impairment_reversal", "cost_correction"}
        and effective_date != required_boundary
    ):
        raise ValidationError(
            {
                "effective_date": (
                    "减值、减值转回和原值更正必须精确从下一未确认自然月首生效；"
                    f"当前应为 {required_boundary.isoformat()}。"
                )
            }
        )
    if _for_update_self(models["DepreciationEntry"].objects.filter(
        asset=asset,
        batch_item__batch__batch_type="regular",
        batch_item__batch__status="confirmed",
        batch_item__batch__period_end__gt=effective_date,
    )).exists():
        raise ValidationError("调整生效日或之后已有确认折旧；必须先倒序冲销受影响月份。")
    value = _money(amount)
    if adjustment_type in {"impairment", "impairment_reversal"} and value <= 0:
        raise ValidationError({"amount": "减值及转回使用正数幅度。"})
    if adjustment_type in {"cost_correction", "depreciation_adjustment"} and value == 0:
        raise ValidationError({"amount": "原值更正和折旧调整不得为 0。"})
    current_ad = _money(_entry_totals(asset))
    old_cost = _money(finance.original_cost)
    old_impairment = _money(finance.impairment_balance_cache)
    new_cost = old_cost
    new_impairment = old_impairment
    new_ad = current_ad
    if adjustment_type == "cost_correction":
        new_cost = _money(old_cost + value)
        if new_cost < 0:
            raise ValidationError("原值更正后不得为负数。")
    elif adjustment_type == "impairment":
        new_impairment = _money(old_impairment + value)
    elif adjustment_type == "impairment_reversal":
        if value > old_impairment:
            raise ValidationError("减值转回不得导致累计减值为负。")
        new_impairment = _money(old_impairment - value)
    elif adjustment_type == "depreciation_adjustment":
        new_ad = _money(current_ad + value)
    if new_ad < 0:
        raise ValidationError("折旧调整不得导致累计折旧为负。")
    new_book = _money(new_cost - new_impairment - new_ad)
    if new_book < 0:
        raise ValidationError("价值调整不得导致账面价值为负。")
    if adjustment_type == "impairment_reversal":
        unimpaired_ceiling = _unimpaired_book_value_ceiling(
            asset=asset,
            effective_date=effective_date,
            finance=finance,
            lock=True,
        )
        if new_book > unimpaired_ceiling:
            raise ValidationError(
                {
                    "amount": (
                        "减值转回后账面价值不得超过假设从未发生该减值时的账面价值；"
                        f"当前上限为 {unimpaired_ceiling}。"
                    )
                }
            )
    profile = None
    if adjustment_type in {"cost_correction", "depreciation_adjustment"}:
        profile = (
            asset.depreciation_profiles.select_for_update()
            .filter(effective_from__lte=effective_date)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_date))
            .order_by("-version")
            .first()
            or asset.depreciation_profiles.select_for_update().order_by("-version").first()
        )
        if profile is None:
            raise ValidationError("价值调整找不到可用的折旧 Profile。")
        floor = _calculate_profile_salvage(profile, new_cost)
        if new_book < floor:
            raise ValidationError("原值/折旧调整会使账面价值低于当前残值地板。")
    old = {
        "original_cost": str(old_cost),
        "impairment": str(old_impairment),
        "accumulated_depreciation": str(current_ad),
        "book_value": str(_money(old_cost - old_impairment - current_ad)),
    }
    new = {
        "original_cost": str(new_cost),
        "impairment": str(new_impairment),
        "accumulated_depreciation": str(new_ad),
        "book_value": str(new_book),
    }
    # The confirmed adjustment is append-only from its first INSERT.  Build
    # complete before/after snapshots first; never insert then patch JSON.
    adjustment = Adjustment.objects.create(
        company=asset.company,
        asset=asset,
        adjustment_type=adjustment_type,
        effective_date=effective_date,
        amount=value,
        old_values_json=old,
        new_values_json=new,
        reason=str(reason).strip(),
        status="confirmed",
        confirmed_by=actor,
        confirmed_at=timezone.now(),
        created_by=actor,
    )
    if new_cost != old_cost or new_impairment != old_impairment:
        _set_controlled_finance_balance_mutation()
        updated = Finance.objects.filter(
            pk=finance.pk,
            original_cost=old_cost,
            impairment_balance_cache=old_impairment,
        ).update(
            original_cost=new_cost,
            impairment_balance_cache=new_impairment,
            updated_at=timezone.now(),
        )
        if updated != 1:
            raise ValidationError("资产财务余额已变更，请刷新后重试。")
    if adjustment_type == "depreciation_adjustment":
        Entry.objects.create(
            company=asset.company,
            asset=asset,
            depreciation_profile=profile,
            entry_date=effective_date,
            period_start=effective_date,
            period_end=effective_date + timedelta(days=1),
            source_type="adjustment",
            value_adjustment=adjustment,
            amount=value,
            accumulated_depreciation_after=new_ad,
            book_value_after=new_book,
            posted_by=actor,
            posted_at=timezone.now(),
        )
    if adjustment_type in {
        "impairment",
        "impairment_reversal",
        "cost_correction",
        "depreciation_adjustment",
    }:
        clone_asset_depreciation_profile(
            actor=actor,
            profile=current_profile,
            data={},
            effective_from=required_boundary,
            reason=f"{adjustment_type} 前瞻重算：{str(reason).strip()}",
            request=request,
        )
    _audit(actor=actor, action="asset_value_adjustment_confirm", instance=adjustment, old=old, new=new, request=request)
    return adjustment


@transaction.atomic
def reverse_value_adjustment(*, actor, adjustment, reason, request=None):
    """Append the exact opposite adjustment and mark its source reversed."""

    require_manage_finance(actor)
    if not str(reason or "").strip():
        raise ValidationError({"reason": "价值调整冲销必须填写原因。"})
    models = _models()
    Adjustment = models["AssetValueAdjustment"]
    Finance = models["AssetFinance"]
    Entry = models["DepreciationEntry"]
    Company = models["Company"]
    Asset = models["Asset"]
    identity = Adjustment.objects.values("company_id", "asset_id").get(
        pk=adjustment.pk
    )
    company = Company.objects.select_for_update().get(pk=identity["company_id"])
    _require_current_company(company)
    asset = _for_update_self(Asset.objects.all()).get(
        pk=identity["asset_id"], company=company
    )
    finance = Finance.objects.select_for_update().get(asset=asset, company=company)
    adjustment = _for_update_self(Adjustment.objects.all()).get(
        pk=adjustment.pk, company=company, asset=asset
    )
    adjustment.company = company
    adjustment.asset = asset
    existing = _for_update_self(Adjustment.objects.filter(
        reversal_of=adjustment
    )).first()
    if existing is not None:
        return existing
    if adjustment.status != "confirmed" or adjustment.reversal_of_id is not None:
        raise ValidationError("只能冲销尚未冲销的原始 confirmed 调整。")
    current_ad = _money(_entry_totals(asset))
    old_cost = _money(finance.original_cost)
    old_impairment = _money(finance.impairment_balance_cache)
    new_cost = old_cost
    new_impairment = old_impairment
    new_ad = current_ad
    if adjustment.adjustment_type in {"opening_impairment", "impairment"}:
        reversal_type = "impairment_reversal"
        reversal_amount = adjustment.amount
        new_impairment = _money(old_impairment - reversal_amount)
    elif adjustment.adjustment_type == "impairment_reversal":
        reversal_type = "impairment"
        reversal_amount = adjustment.amount
        new_impairment = _money(old_impairment + reversal_amount)
    elif adjustment.adjustment_type == "cost_correction":
        reversal_type = "cost_correction"
        reversal_amount = -adjustment.amount
        new_cost = _money(old_cost + reversal_amount)
    else:
        reversal_type = "depreciation_adjustment"
        reversal_amount = -adjustment.amount
        new_ad = _money(current_ad + reversal_amount)
    if min(new_cost, new_impairment, new_ad) < 0:
        raise ValidationError("当前余额已变化，冲销会产生负余额。")
    new_book = _money(new_cost - new_impairment - new_ad)
    if new_book < 0:
        raise ValidationError("冲销后账面价值不得为负。")
    current_profile = _for_update_self(
        asset.depreciation_profiles.filter(status__in=("active", "suspended"))
    ).order_by("-version").first()
    if current_profile is None:
        raise ValidationError("价值调整冲销后无法建立前瞻折旧版本：缺少当前 Profile。")
    effective_date = _next_unconfirmed_profile_month(
        asset=asset, profile=current_profile, lock=True
    )
    if reversal_type == "impairment_reversal":
        unimpaired_ceiling = _unimpaired_book_value_ceiling(
            asset=asset,
            effective_date=effective_date,
            finance=finance,
            lock=True,
        )
        if new_book > unimpaired_ceiling:
            raise ValidationError(
                {
                    "amount": (
                        "冲销原减值后账面价值不得超过假设从未发生该减值时的账面价值；"
                        f"当前上限为 {unimpaired_ceiling}。"
                    )
                }
            )
    old = {
        "original_cost": str(old_cost),
        "impairment": str(old_impairment),
        "accumulated_depreciation": str(current_ad),
        "book_value": str(_money(old_cost - old_impairment - current_ad)),
    }
    new = {
        "original_cost": str(new_cost),
        "impairment": str(new_impairment),
        "accumulated_depreciation": str(new_ad),
        "book_value": str(new_book),
    }
    reversal = Adjustment.objects.create(
        company=adjustment.company,
        asset=asset,
        adjustment_type=reversal_type,
        effective_date=effective_date,
        amount=reversal_amount,
        old_values_json=old,
        new_values_json=new,
        reason=str(reason).strip(),
        status="confirmed",
        confirmed_by=actor,
        confirmed_at=timezone.now(),
        reversal_of=adjustment,
        created_by=actor,
    )
    if new_cost != old_cost or new_impairment != old_impairment:
        _set_controlled_finance_balance_mutation()
        updated = Finance.objects.filter(
            pk=finance.pk,
            original_cost=old_cost,
            impairment_balance_cache=old_impairment,
        ).update(
            original_cost=new_cost,
            impairment_balance_cache=new_impairment,
            updated_at=timezone.now(),
        )
        if updated != 1:
            raise ValidationError("资产财务余额已变更，请刷新后重试。")
    if reversal_type == "depreciation_adjustment":
        original_entry = Entry.objects.select_for_update().get(
            value_adjustment=adjustment
        )
        Entry.objects.create(
            company=adjustment.company,
            asset=asset,
            depreciation_profile=original_entry.depreciation_profile,
            entry_date=effective_date,
            period_start=effective_date,
            period_end=effective_date + timedelta(days=1),
            source_type="adjustment",
            value_adjustment=reversal,
            amount=reversal_amount,
            accumulated_depreciation_after=new_ad,
            book_value_after=new_book,
            reversal_of=original_entry,
            posted_by=actor,
            posted_at=timezone.now(),
        )
    _set_controlled_adjustment_reversal()
    updated = Adjustment.objects.filter(
        pk=adjustment.pk, status="confirmed"
    ).update(status="reversed")
    if updated != 1:
        raise ValidationError("原调整状态已变更，请刷新后重试。")
    clone_asset_depreciation_profile(
        actor=actor,
        profile=current_profile,
        data={},
        effective_from=effective_date,
        reason=f"{reversal_type} 冲销后前瞻重算：{str(reason).strip()}",
        request=request,
    )
    _audit(
        actor=actor,
        action="asset_value_adjustment_reverse",
        instance=reversal,
        old=old,
        new={**new, "reversal_of_id": str(adjustment.pk)},
        request=request,
    )
    return reversal


@transaction.atomic
def run_theoretical_depreciation(*, actor, asset, as_of_date, parameters, idempotency_key, request=None):
    require_manage_finance(actor)
    models = _models()
    Asset = models["Asset"]
    Company = models["Company"]
    Run = models["TheoreticalDepreciationRun"]
    Line = models["TheoreticalDepreciationLine"]
    company_id = Asset.objects.values_list("company_id", flat=True).get(pk=asset.pk)
    company = Company.objects.select_for_update().get(pk=company_id)
    _require_current_company(company)
    asset = _for_update_self(Asset.objects.all()).get(pk=asset.pk, company=company)
    asset.company = company
    ensure_asset_is_depreciable(asset)
    existing = Run.objects.select_for_update().filter(company=asset.company, idempotency_key=idempotency_key).first()
    digest = _request_hash({"asset_id": asset.pk, "as_of_date": as_of_date, "parameters": parameters})
    if existing:
        if _request_hash(existing.parameter_snapshot_json) != _request_hash({"digest": digest, **parameters}):
            raise ValidationError("相同理论试算幂等键参数不同。")
        return existing
    spec = _domain().ScheduleInput(**parameters)
    result = _domain().generate_schedule(spec)
    run = Run.objects.create(
        company=asset.company,
        asset=asset,
        as_of_date=as_of_date,
        parameter_snapshot_json={"digest": digest, **_serializable(parameters)},
        status="draft",
        requested_by=actor,
        requested_at=timezone.now(),
        completed_at=None,
        idempotency_key=idempotency_key,
    )
    accumulated = ZERO_MONEY
    cutoff_exclusive = as_of_date + timedelta(days=1)
    for schedule in result.lines:
        if schedule.period_end > cutoff_exclusive:
            break
        accumulated += schedule.planned_amount
        Line.objects.create(
            run=run,
            period_start=schedule.period_start,
            period_end=schedule.period_end,
            theoretical_amount=schedule.planned_amount,
            theoretical_accumulated=accumulated,
            theoretical_book_value=schedule.closing_book_value,
            formula_snapshot_json=schedule.formula_snapshot,
        )
    run.status = "completed"
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "completed_at"])
    _audit(actor=actor, action="theoretical_depreciation_run", instance=run, new={"as_of_date": as_of_date, "line_count": run.lines.count()}, request=request)
    return run


__all__ = [
    "activate_depreciation_policy",
    "clone_asset_depreciation_profile",
    "clone_depreciation_policy",
    "confirm_asset_finance",
    "confirm_depreciation_batch",
    "create_fixed_asset_category",
    "create_depreciation_policy",
    "create_profile_event",
    "create_value_adjustment",
    "deactivate_fixed_asset_category",
    "delete_fixed_asset_category",
    "generate_depreciation_batch",
    "ensure_asset_is_depreciable",
    "is_depreciable_fixed_asset",
    "depreciable_fixed_asset_filter",
    "preview_asset_depreciation",
    "record_work_usage",
    "retire_depreciation_policy",
    "resolve_depreciation_policy",
    "reverse_depreciation_batch",
    "reverse_value_adjustment",
    "run_theoretical_depreciation",
    "save_asset_finance_draft",
    "set_category_default_depreciation_policy",
    "set_default_depreciation_policy",
    "update_fixed_asset_category",
    "update_draft_depreciation_policy",
]
