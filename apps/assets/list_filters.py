"""Shared current-ledger filtering for the list and its audited Excel export."""
from datetime import date
import re

from django.core.exceptions import ValidationError
from django.db.models import CharField, Exists, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce

from apps.assets.models import Asset, AssetQrIdentity, AttachmentLink
from apps.assets.classification import individual_durable_filter
from apps.assets.permissions import ASSET_GLOBAL_P1_VIEW_ROLES, can_view_financial_fields, scoped_assets, scoped_assets_p1
from apps.masterdata.models import AssetCategory, Department, Employee, FixedAssetCategory, Location
from apps.masterdata.location_tree import LocationTree
from apps.masterdata.permissions import role_names_for, scoped_departments, scoped_employees

FILTER_LABELS = {
    "q": "搜索", "category": "实物分类", "department": "部门",
    "employee": "责任人", "location": "位置", "asset_status": "资产状态",
    "record_status": "显示范围", "accounting_treatment": "会计认定",
    "fixed_asset_category": "固定资产类别", "view": "资产视图",
    "maintenance_required": "需要保养", "label_status": "标签状态",
    "has_serial_number": "有序列号", "has_attachments": "有可见附件",
    "initialized_from": "正式建档开始日期", "initialized_to": "正式建档结束日期",
    "created_from": "创建开始日期", "created_to": "创建结束日期",
}
MODEL_FILTERS = {
    "category": (AssetCategory, "category_id"), "department": (Department, "department_id"),
    "employee": (Employee, "responsible_employee_id"), "location": (Location, "location_id"),
    "fixed_asset_category": (FixedAssetCategory, "finance__fixed_asset_category_id"),
}
CHOICES = {
    "asset_status": {**dict(Asset.AssetStatus.choices), "pending_label": "待标识确认", "other_disposed": "已其他处置／归还"},
    "record_status": {"active": "当前业务资产", "archived": "仅归档资产"},
    "accounting_treatment": {"fixed_asset": "固定资产", "controlled_non_fixed": "受控非固定资产", "unconfirmed": "未确认"},
    "view": {"individual_durable": "逐件低值耐用品"},
    "label_status": dict(AssetQrIdentity.LabelStatus.choices),
    **{key: {"yes": "是", "no": "否"} for key in ("maintenance_required", "has_serial_number", "has_attachments")},
}


def normalize_list_filters(data, *, actor, company):
    clean = {key: str(data.get(key) if data.get(key) is not None else "").strip() for key in FILTER_LABELS}
    if not can_view_financial_fields(actor):
        clean["accounting_treatment"] = clean["fixed_asset_category"] = ""
    for key, choices in CHOICES.items():
        if clean[key] and clean[key] not in choices:
            raise ValidationError({key: f"{FILTER_LABELS[key]}无效。"})
    if len(clean["q"]) > 200:
        raise ValidationError({"q": "搜索内容不能超过 200 个字符。"})
    for key, (model, _) in MODEL_FILTERS.items():
        if clean[key]:
            candidates = model.objects.filter(company=company)
            if key == "department":
                candidates = scoped_departments(actor, company)
            elif key == "employee":
                candidates = scoped_employees(actor, company)
            elif key == "location" and not role_names_for(actor).intersection(ASSET_GLOBAL_P1_VIEW_ROLES):
                visible = scoped_assets(actor, company).values_list("location_id", flat=True)
                candidates = candidates.filter(pk__in=LocationTree(company).ancestors(visible))
            elif key == "category" and not role_names_for(actor).intersection(ASSET_GLOBAL_P1_VIEW_ROLES):
                candidates = candidates.filter(pk__in=scoped_assets(actor, company).values(MODEL_FILTERS[key][1]))
            try:
                exists = candidates.filter(pk=int(clean[key])).exists()
            except (TypeError, ValueError, OverflowError):
                exists = False
            if not exists:
                raise ValidationError({key: f"{FILTER_LABELS[key]}无效或不在当前账号的可选范围内。"})
    for key in ("initialized_from", "initialized_to", "created_from", "created_to"):
        if clean[key]:
            try:
                clean[key] = date.fromisoformat(clean[key]).isoformat()
            except ValueError as exc:
                raise ValidationError({key: "请填写有效日期。"}) from exc
    for start, end, label in (("initialized_from", "initialized_to", "正式建档"), ("created_from", "created_to", "创建")):
        if clean[start] and clean[end] and clean[start] > clean[end]:
            raise ValidationError({end: f"{label}结束日期不得早于开始日期。"})
    return clean


def with_ledger_status(queryset, *, company, actor):
    from apps.inventory.models import InventoryScan
    from apps.inventory.permissions import scoped_inventory_tasks

    labels = AssetQrIdentity.objects.filter(company=company, asset_id=OuterRef("pk"), status="active")
    scans = InventoryScan.objects.filter(
        company=company, asset_id=OuterRef("pk"), is_effective=True,
        inventory_task__in=scoped_inventory_tasks(actor, company).exclude(status="cancelled"),
    ).order_by("-scanned_at", "-pk")
    from apps.assets.status_display import with_identity_display
    return with_identity_display(queryset).annotate(
        current_label_status=Coalesce(Subquery(labels.values("label_status")[:1]), Value("not_generated")),
        latest_inventory_at=Subquery(scans.values("scanned_at")[:1]),
    )


def filter_asset_list(queryset, filters, *, actor, company):
    """Caller supplies a permission-scoped queryset; filtering never widens it."""
    qs = queryset.filter(company=company, record_status=filters.get("record_status") or "active")
    p1_ids = scoped_assets_p1(actor, company).values("pk")
    all_p1 = not qs.exclude(pk__in=p1_ids).exists()
    query = filters.get("q", "")
    if query:
        search = Q(asset_code__icontains=query) | Q(asset_name__icontains=query) | Q(responsible_employee__name__icontains=query)
        if all_p1:
            search |= Q(model__icontains=query) | Q(serial_number__icontains=query) | Q(factory_number__icontains=query) | Q(equipment_number__icontains=query)
        draft = re.fullmatch(r"D-([0-9A-Fa-f]{1,8})", query)
        if draft:
            qs = qs.annotate(_draft_uuid=Cast("id", output_field=CharField()))
            search |= Q(_draft_uuid__istartswith=draft.group(1))
        qs = qs.filter(search)
    for key, (_, field) in MODEL_FILTERS.items():
        if filters.get(key):
            if key == "location":
                qs = qs.filter(location_id__in=LocationTree(company).descendants(filters[key]))
            else:
                qs = qs.filter(**{field: filters[key]})
    if filters.get("asset_status"):
        qs = qs.filter(asset_status=filters["asset_status"])
    if filters.get("view") == "individual_durable":
        qs = qs.filter(individual_durable_filter())
    treatment = filters.get("accounting_treatment")
    if treatment == "unconfirmed":
        qs = qs.filter(Q(finance__isnull=True) | Q(finance__finance_confirmed_at__isnull=True) | Q(finance__accounting_treatment__isnull=True))
    elif treatment:
        qs = qs.filter(finance__accounting_treatment=treatment, finance__finance_confirmed_at__isnull=False)
    if filters.get("label_status"):
        qs = qs.filter(current_label_status=filters["label_status"])
    for key, lookup in (
        ("initialized_from", "registration__registered_at__date__gte"),
        ("initialized_to", "registration__registered_at__date__lte"),
        ("created_from", "created_at__date__gte"),
        ("created_to", "created_at__date__lte"),
    ):
        if filters.get(key):
            qs = qs.filter(**{lookup: date.fromisoformat(filters[key])})
    # P1 filters must never reveal attributes of HR-only summary rows.
    if all_p1:
        if filters.get("maintenance_required"):
            qs = qs.filter(is_maintenance_required=filters["maintenance_required"] == "yes")
        if filters.get("has_serial_number"):
            empty = Q(serial_number="") | Q(serial_number__isnull=True)
            qs = qs.exclude(empty) if filters["has_serial_number"] == "yes" else qs.filter(empty)
        if filters.get("has_attachments"):
            security = ["A0", "A1"] if can_view_financial_fields(actor) else ["A0"]
            links = AttachmentLink.objects.filter(
                company=company, asset_id=OuterRef("pk"), status="active",
                attachment__is_available=True, security_class__in=security,
            )
            qs = qs.annotate(_visible_attachment=Exists(links)).filter(_visible_attachment=filters["has_attachments"] == "yes")
    return qs


def describe_list_filters(filters, *, company):
    result = []
    for key, value in filters.items():
        if not value or key not in FILTER_LABELS:
            continue
        label = CHOICES.get(key, {}).get(value, value)
        if key in MODEL_FILTERS:
            item = MODEL_FILTERS[key][0].objects.filter(company=company, pk=value).first()
            label = LocationTree(company).path(item.pk) if item and key == "location" else str(item) if item else value
        result.append((FILTER_LABELS[key], label))
    return result
