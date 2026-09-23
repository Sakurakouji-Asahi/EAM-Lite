"""Optional master-data numbers, allocated only inside controlled saves.

No numbers are reserved while rendering or validating a form. PostgreSQL
serializes automatic allocation in the same model/company scope; normalized
unique constraints and collision retries also protect against manual writes.
"""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, router, transaction

from apps.masterdata.normalization import clean_display_identifier, normalize_identifier


# Model label -> (editable field, normalized field, default prefix).
NUMBER_FIELDS = {
    "masterdata.company": ("code", "normalized_code", "CO"),
    "masterdata.department": ("code", "normalized_code", "DP"),
    "masterdata.employee": ("employee_no", "normalized_employee_no", "EMP"),
    "masterdata.location": ("code", "normalized_code", "LOC"),
    "masterdata.assetcategory": ("code", "normalized_code", "CAT"),
    "masterdata.fixedassetcategory": ("code", "normalized_code", "FAC"),
    "supplies.supplycategory": ("code", "normalized_code", "SC"),
    "supplies.supplywarehouse": ("code", "normalized_code", "WH"),
    "supplies.supplyitem": ("item_code", "normalized_item_code", None),
}

HIERARCHICAL_NUMBER_MODELS = frozenset({
    "masterdata.department",
    "masterdata.location",
    "masterdata.assetcategory",
    "supplies.supplycategory",
})


def configure_auto_number_field(form):
    """Allow a blank new number without weakening model or edit validation."""
    instance = getattr(form, "instance", None)
    if instance is None:
        return
    rule = NUMBER_FIELDS.get(instance._meta.label_lower)
    if rule is None or rule[0] not in form.fields:
        return
    field = form.fields[rule[0]]
    if instance._state.adding:
        field.required = False
        field.widget.attrs["placeholder"] = "留空自动生成，也可手工填写"
        field.help_text = "留空时在保存后自动生成；手工填写时使用所填编号，并检查是否重复。"
        if instance._meta.label_lower in HIERARCHICAL_NUMBER_MODELS:
            field.help_text += "选择上级后，自动编号采用“上级编码-两位序号”。"
        if instance._meta.label_lower == "masterdata.assetcategory":
            field.help_text += "一级分类自动使用 01—99 的空闲两位数字。"
    else:
        field.required = True


def prepare_auto_number(instance):
    """Fill a new blank number; never replace an existing or supplied number.

Call after permission checks and before full_clean/save, inside the service
transaction. Calling for unrelated models is intentionally a no-op so that
the existing shared save helpers can retain a single validation path.
"""
    label = instance._meta.label_lower
    rule = NUMBER_FIELDS.get(label)
    if rule is None:
        return
    field, normalized_field, prefix = rule
    using = instance._state.db or router.db_for_write(type(instance), instance=instance)
    connection = connections[using]
    if not connection.in_atomic_block:
        raise RuntimeError("自动编号必须在保存事务中生成。")
    company_id = getattr(instance, "company_id", None)
    if label != "masterdata.company" and company_id is None:
        raise ValidationError({"company": "生成编号前必须选择公司。"})
    if not instance._state.adding or normalize_identifier(getattr(instance, field)):
        return
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"master-number:{label}:{company_id}"],
            )
    queryset = type(instance)._default_manager.using(using).all()
    if company_id is not None:
        queryset = queryset.filter(company_id=company_id)

    if label == "masterdata.assetcategory" and instance.parent_id is None:
        # Major categories feed the existing two-digit asset identity rule.
        # Check ALL category rows, including inactive and child categories.
        occupied = set(queryset.values_list(normalized_field, flat=True))
        for number in range(1, 100):
            candidate = f"{number:02d}"
            if candidate not in occupied:
                setattr(instance, field, candidate)
                return
        raise ValidationError({field: "01—99 的一级分类编码已全部占用，请核对分类。"})

    if label in HIERARCHICAL_NUMBER_MODELS and instance.parent_id is not None:
        parent_code = clean_display_identifier(instance.parent.code)
        if not parent_code:
            raise ValidationError({"parent": "上级编码为空，无法生成下级编号。"})
        max_length = instance._meta.get_field(field).max_length
        if len(parent_code) + 3 > max_length:
            raise ValidationError({field: "上级编码过长，无法追加两位下级序号；请核对编码。"})
        parent_prefix = normalize_identifier(parent_code)
        sibling_pattern = re.compile(rf"{re.escape(parent_prefix)}-([0-9]{{2}})\Z")
        sibling_codes = queryset.filter(parent_id=instance.parent_id).values_list(
            normalized_field, flat=True
        )
        last_number = max(
            (int(match.group(1)) for code in sibling_codes
             if (match := sibling_pattern.fullmatch(code))),
            default=0,
        )
        for number in range(last_number + 1, 100):
            candidate = f"{parent_code}-{number:02d}"
            if not queryset.filter(**{normalized_field: normalize_identifier(candidate)}).exists():
                setattr(instance, field, candidate)
                return
        raise ValidationError({field: "该上级下的两位序号已用完，请手工填写未使用的编号。"})

    if label == "supplies.supplyitem":
        prefix = {"durable_quantity": "LVD", "consumable": "LVC"}.get(instance.item_type)
        if prefix is None:
            raise ValidationError({"item_type": "请选择有效的物品管理方式。"})
    last = queryset.filter(
        **{f"{normalized_field}__regex": f"^{prefix.lower()}[0-9]{{6}}$"}
    ).order_by(f"-{normalized_field}").values_list(normalized_field, flat=True).first()
    number = int(last[-6:]) + 1 if last else 1
    if number > 999999:
        raise ValidationError({field: "该自动编号流水已用完，请手工填写未使用的编号。"})
    setattr(instance, field, f"{prefix}{number:06d}")


def save_with_auto_number(instance, *, update_fields=None, validate=True):
    """Retry only an automatic candidate occupied by a concurrent manual write.

Manual writes do not join the numbering lock: existing services and imports
can already own company/related-row locks. Acquiring a new advisory lock there
would reverse lock order against an automatic insert's foreign-key checks.
"""
    rule = NUMBER_FIELDS.get(instance._meta.label_lower)
    automatic = bool(
        rule and instance._state.adding
        and not normalize_identifier(getattr(instance, rule[0]))
    )
    using = instance._state.db or router.db_for_write(type(instance), instance=instance)
    for _ in range(10 if automatic else 1):
        prepare_auto_number(instance)
        try:
            with transaction.atomic(using=using):
                if validate:
                    instance.full_clean()
                instance.save(using=using, update_fields=update_fields)
            return instance
        except (ValidationError, IntegrityError):
            if not automatic:
                raise
            field, normalized_field, _prefix = rule
            collisions = type(instance)._default_manager.using(using).filter(
                **{normalized_field: normalize_identifier(getattr(instance, field))}
            )
            company_id = getattr(instance, "company_id", None)
            if company_id is not None:
                collisions = collisions.filter(company_id=company_id)
            if not collisions.exists():
                raise
            setattr(instance, field, "")
    raise ValidationError({rule[0]: "当前编号正在被其他人使用，请重新保存以生成新编号。"})
