"""Shared permanent-code issuance for physical registration and finance compatibility.

Call only inside an authorized business transaction that locks Company then Asset.
The caller binds the issued code, QR and durable registration before committing.
"""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.models import Q
from django.utils import timezone

from apps.coding.domain import (
    build_scope_key, is_effective, normalize_code, render_code,
    validate_scheme_structure,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _models():
    from apps.assets.models import AssetCodeHistory
    from apps.masterdata.models import AssetCodingScheme, IssuedCode, SequenceCounter
    return {
        "AssetCodeHistory": AssetCodeHistory,
        "AssetCodingScheme": AssetCodingScheme,
        "IssuedCode": IssuedCode,
        "SequenceCounter": SequenceCounter,
    }


def _effective_timestamp(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=SHANGHAI)

def _resolve_coding_scheme(*, asset, effective_date, lock=True):
    Scheme = _models()["AssetCodingScheme"]
    queryset = Scheme.objects.prefetch_related("segments")
    if lock:
        queryset = queryset.select_for_update()
    if asset.requested_coding_scheme_id:
        scheme = queryset.get(pk=asset.requested_coding_scheme_id)
        if scheme.company_id != asset.company_id or not is_effective(scheme, effective_date):
            raise ValidationError({"coding_scheme": "指定编码方案在正式编号生效日不可用；不会静默回退。"})
        return scheme
    if asset.component_of_id:
        parent = asset.component_of
        if parent.identity is None:
            raise ValidationError({"component_of": "主资产尚无统一编码身份。"})
        key = parent.current_issued_code.coding_scheme.scheme_key
        schemes = list(queryset.filter(company=asset.company, scheme_key=key, status="active", effective_from__lte=effective_date)
                       .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=effective_date)))
        if len(schemes) != 1:
            raise ValidationError({"coding_scheme": "主资产的统一编码规则暂无生效版本，请先维护规则。"})
        return schemes[0]
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

def _insert_counter_if_missing(*, company, scheme, scope_key, standard_scope=False):
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
    filters = {"company": company, "scope_key": scope_key}
    if not standard_scope:
        filters["coding_scheme"] = scheme
    return Counter.objects.select_for_update().get(**filters)

def _issue_asset_code(*, actor, asset, effective_date, reason, idempotency_key):
    if not connection.in_atomic_block:
        raise ValidationError("正式编号必须随受控建档事务生成。")
    models = _models()
    IssuedCode = models["IssuedCode"]
    History = models["AssetCodeHistory"]
    scheme = _resolve_coding_scheme(asset=asset, effective_date=effective_date)
    segments = validate_scheme_structure(scheme)
    from apps.coding.standard import is_standard_segments
    identity_values = None
    if is_standard_segments(segments):
        from apps.coding.standard_issuance import _allocate_standard_code
        scope_key, next_value, display, identity_values = _allocate_standard_code(
            actor=actor, asset=asset, scheme=scheme, effective_date=effective_date,
        )
    else:
        if asset.component_of_id:
            raise ValidationError({"component_of": "组件必须使用统一资产编码方案。"})
        category_scoped = scheme.reset_mode in {"category_yearly", "category_monthly"}
        scope_key = build_scope_key(
            asset.company_id, scheme.pk, scheme.reset_mode, effective_date,
            category=asset.category if category_scoped else None,
            category_scope_level=(scheme.category_scope_level if category_scoped else None),
        )
        counter = _insert_counter_if_missing(company=asset.company, scheme=scheme, scope_key=scope_key)
        next_value = counter.current_value + 1
        display = render_code(segments, {"company": asset.company, "category": asset.category,
            "department": asset.department, "effective_date": effective_date}, next_value)
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('eam_lite.controlled_sequence_counter_increment', 'on', true)")
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
    issued._identity_values = identity_values
    return issued
