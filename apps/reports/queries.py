"""Permission-scoped, read-only report materialization."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import (
    Count,
    Exists,
    OuterRef,
    Q,
    Sum,
)
from django.utils import timezone

from apps.finance.reporting import (
    ZERO,
    approved_depreciation_entries,
    balances_by_asset,
    confirmed_entries_for_period,
    depreciation_entry_business_date,
    money,
    tplus_period_components,
)
from apps.reports.permissions import (
    require_tplus_export,
    require_view_report,
    scoped_report_assets,
)
from apps.reports.schemas import TPLUS_TOTAL_METRICS, get_report_definition


MANAGED_STATUSES = (
    "pending_label", "in_use", "idle", "loaned", "under_repair",
    "pending_disposal",
)
TERMINAL_STATUSES = ("disposed", "sold", "other_disposed")


class ReportValidationError(ValidationError):
    def __init__(self, errors, *, warnings=()):
        self.errors = tuple(str(item) for item in errors)
        self.warnings = tuple(str(item) for item in warnings)
        super().__init__(list(self.errors))


@dataclass(frozen=True, slots=True)
class ReportDataset:
    definition: object
    rows: tuple[MappingProxyType, ...]
    filters: MappingProxyType
    data_snapshot_at: datetime
    totals: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    warnings: tuple[str, ...] = ()

    @property
    def row_count(self):
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class TplusDataset:
    definition: object
    asset_rows: tuple[MappingProxyType, ...]
    entry_rows: tuple[MappingProxyType, ...]
    filters: MappingProxyType
    data_snapshot_at: datetime
    totals: MappingProxyType
    warnings: tuple[str, ...] = ()

    @property
    def rows(self):
        return self.asset_rows

    @property
    def row_count(self):
        return len(self.asset_rows)


def _frozen_rows(rows):
    return tuple(MappingProxyType(dict(row)) for row in rows)


def _begin_consistent_read():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )


def _business_boundary(company, business_date):
    tz = ZoneInfo(company.timezone or "Asia/Shanghai")
    return timezone.make_aware(
        datetime.combine(business_date, datetime.min.time()), timezone=tz
    )


def _location_path(location):
    if location is None:
        return ""
    names, seen = [], set()
    node = location
    while node is not None and node.pk not in seen:
        seen.add(node.pk)
        names.append(node.name)
        node = node.parent
    return " / ".join(reversed(names))


def _display(instance, field):
    if instance is None:
        return ""
    method = getattr(instance, f"get_{field}_display", None)
    return method() if method else str(getattr(instance, field, "") or "")


def _validated_filters(*, actor, company, filters):
    from apps.assets.permissions import can_view_financial_fields
    from apps.masterdata.models import (
        AssetCategory,
        Department,
        Employee,
        FixedAssetCategory,
    )
    from apps.masterdata.permissions import resolve_department_ids, role_names_for

    clean = dict(filters or {})
    if (
        clean.get("fixed_asset_category") not in (None, "")
        and not can_view_financial_fields(actor)
    ):
        # Reject before resolving the master-data ID so an unauthorized caller
        # cannot enumerate valid F1 category identifiers through errors or row
        # counts.
        raise ReportValidationError(("您无权使用固定资产会计类别筛选。",))
    choice_filters = {
        "asset_scope": {"managed"},
        "label_scope": {"not_attached"},
        "maintenance_due_scope": {"upcoming", "overdue"},
        "accounting_treatment": {
            "fixed_asset",
            "controlled_non_fixed",
            "unconfirmed",
        },
    }
    for key, choices in choice_filters.items():
        value = clean.get(key)
        if value in (None, ""):
            clean.pop(key, None)
        elif value not in choices:
            raise ReportValidationError((f"{key} 筛选值无效。",))
    model_fields = (
        ("department", Department),
        ("category", AssetCategory),
        ("fixed_asset_category", FixedAssetCategory),
        ("responsible_employee", Employee),
    )
    for key, model in model_fields:
        value = clean.get(key)
        if value in (None, ""):
            clean.pop(key, None)
            continue
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ReportValidationError((f"{key} 筛选值无效。",)) from exc
        if not model.objects.filter(company=company, pk=value).exists():
            raise ReportValidationError((f"{key} 不属于当前公司。",))
        clean[key] = value
    if "department_manager" in role_names_for(actor) and clean.get("department"):
        if clean["department"] not in resolve_department_ids(actor, company):
            raise ReportValidationError(("无权筛选该部门。",))
    for key in ("as_of_date", "period_start", "period_end"):
        value = clean.get(key)
        if isinstance(value, str) and value:
            try:
                clean[key] = date.fromisoformat(value)
            except ValueError as exc:
                raise ReportValidationError((f"{key} 必须是有效日期。",)) from exc
    start, end = clean.get("period_start"), clean.get("period_end")
    if bool(start) != bool(end) or start and end < start:
        raise ReportValidationError(("报表期间无效。",))
    return clean


def _base_assets(actor, company, filters):
    from apps.assets.models import Asset

    qs = scoped_report_assets(
        actor,
        company,
        queryset=Asset.objects.select_related(
            "category", "department", "responsible_employee", "location",
            "location__parent", "finance", "finance__fixed_asset_category",
        ),
    )
    if filters.get("department"):
        qs = qs.filter(department_id=filters["department"])
    if filters.get("category"):
        qs = qs.filter(category_id=filters["category"])
    if filters.get("responsible_employee"):
        qs = qs.filter(responsible_employee_id=filters["responsible_employee"])
    if filters.get("fixed_asset_category"):
        qs = qs.filter(finance__fixed_asset_category_id=filters["fixed_asset_category"])
    if filters.get("accounting_treatment") in {
        "fixed_asset",
        "controlled_non_fixed",
    }:
        qs = qs.filter(
            finance__accounting_treatment=filters["accounting_treatment"],
            finance__finance_confirmed_at__isnull=False,
        )
    elif filters.get("accounting_treatment") == "unconfirmed":
        qs = qs.filter(
            Q(finance__isnull=True)
            | Q(finance__finance_confirmed_at__isnull=True)
            | Q(finance__accounting_treatment__isnull=True)
        )
    if filters.get("asset_status"):
        qs = qs.filter(asset_status=filters["asset_status"])
    if not filters.get("include_drafts", False):
        qs = qs.exclude(asset_status__in=("draft", "pending_finance"))
    if not filters.get("include_disposed", True):
        qs = qs.exclude(asset_status__in=TERMINAL_STATUSES)
    return qs.order_by("asset_code", "created_at", "id")


def _assets_at(actor, company, filters, boundary):
    """Materialize authorized assets and their assignment/status at a boundary."""
    from apps.assets.models import Asset
    qs = scoped_report_assets(
        actor,
        company,
        queryset=Asset.objects.filter(created_at__lt=boundary).select_related(
            "category", "department", "responsible_employee", "location",
            "location__parent", "finance", "finance__fixed_asset_category",
        ),
    )
    if filters.get("category"):
        qs = qs.filter(category_id=filters["category"])
    if filters.get("fixed_asset_category"):
        qs = qs.filter(finance__fixed_asset_category_id=filters["fixed_asset_category"])
    if filters.get("accounting_treatment") in {
        "fixed_asset",
        "controlled_non_fixed",
    }:
        qs = qs.filter(
            finance__accounting_treatment=filters["accounting_treatment"],
            finance__finance_confirmed_at__isnull=False,
        )
    elif filters.get("accounting_treatment") == "unconfirmed":
        qs = qs.filter(
            Q(finance__isnull=True)
            | Q(finance__finance_confirmed_at__isnull=True)
            | Q(finance__accounting_treatment__isnull=True)
        )
    if filters.get("label_scope") == "not_attached":
        from apps.assets.models import AssetQrIdentity

        qs = qs.annotate(
            has_attached_label=Exists(
                AssetQrIdentity.objects.filter(
                    asset_id=OuterRef("pk"), status="active", label_status="attached"
                )
            )
        ).filter(has_attached_label=False)
    assets = list(qs.order_by("asset_code", "created_at", "id"))
    attribution = _historical_attribution(assets, boundary)
    selected = []
    for asset in assets:
        at = attribution[asset.pk]
        department_id = getattr(at["department"], "pk", None)
        employee_id = getattr(at["responsible_employee"], "pk", None)
        if filters.get("department") and department_id != filters["department"]:
            continue
        if filters.get("responsible_employee") and employee_id != filters["responsible_employee"]:
            continue
        status = at["asset_status"]
        if filters.get("asset_scope") == "managed" and status not in MANAGED_STATUSES:
            continue
        if filters.get("asset_status") and status != filters["asset_status"]:
            continue
        if not filters.get("include_drafts", False) and status in {"draft", "pending_finance"}:
            continue
        if not filters.get("include_disposed", True) and status in TERMINAL_STATUSES:
            continue
        selected.append(asset)
    return selected, attribution


def _historical_attribution(assets, boundary):
    """Use the first later movement's before-values at a historic boundary."""
    from apps.assets.models import AssetMovement

    ids = [item.pk for item in assets]
    later = AssetMovement.objects.filter(
        asset_id__in=ids, effective_at__gte=boundary
    ).select_related(
        "from_department", "from_employee", "from_location", "from_location__parent"
    ).order_by("asset_id", "effective_at", "created_at", "id")
    first = {}
    for movement in later.iterator(chunk_size=1000):
        first.setdefault(movement.asset_id, movement)
    result = {}
    for asset in assets:
        movement = first.get(asset.pk)
        finance = getattr(asset, "finance", None)
        if finance is None or finance.finance_confirmed_at is None or (
            finance.finance_confirmed_at >= boundary
        ):
            historic_status = (
                "pending_finance"
                if asset.submitted_at is not None and asset.submitted_at < boundary
                else "draft"
            )
        else:
            historic_status = movement.from_status if movement else asset.asset_status
        result[asset.pk] = {
            "department": movement.from_department if movement else asset.department,
            "responsible_employee": movement.from_employee if movement else asset.responsible_employee,
            "location": movement.from_location if movement else asset.location,
            "asset_status": historic_status,
        }
    return result


def _asset_rows(*, actor, company, report_key, filters):
    as_of = filters.get("as_of_date") or timezone.localdate()
    boundary = _business_boundary(company, as_of + timedelta(days=1))
    assets, attribution = _assets_at(actor, company, filters, boundary)
    if report_key == "equipment_list":
        assets = [a for a in assets if a.category.category_type == "equipment"]
    elif report_key == "mold_tool_inspection_list":
        assets = [a for a in assets if a.category.category_type in {"mold", "tool", "inspection_tool"}]
    elif report_key == "fixed_asset_detail":
        assets = [
            a for a in assets
            if hasattr(a, "finance")
            and a.finance.finance_confirmed_at
            and a.finance.finance_confirmed_at < boundary
            and a.finance.accounting_treatment == "fixed_asset"
        ]
    balances = balances_by_asset(
        company=company, asset_ids=[a.pk for a in assets], boundary=as_of + timedelta(days=1)
    ) if report_key == "fixed_asset_detail" else {}
    rows = []
    for asset in assets:
        at = attribution[asset.pk]
        row = {
            "asset_code": asset.asset_code or f"草稿-{asset.pk}",
            "asset_name": asset.asset_name,
            "category": asset.category.name,
            "model": asset.model,
            "department": getattr(at["department"], "name", ""),
            "responsible_employee": getattr(at["responsible_employee"], "name", ""),
            "location": _location_path(at["location"]),
            "asset_status": dict(asset.AssetStatus.choices).get(at["asset_status"], at["asset_status"]),
            "quantity": asset.quantity,
            "acquisition_date": asset.acquisition_date,
        }
        if report_key == "fixed_asset_detail":
            finance = asset.finance
            balance = balances.get(asset.pk)
            row.update({
                "accounting_treatment": finance.get_accounting_treatment_display(),
                "fixed_asset_category": getattr(finance.fixed_asset_category, "name", ""),
                "original_cost": balance.original_cost if balance else ZERO,
                "actual_accumulated_depreciation": balance.accumulated_depreciation if balance else ZERO,
                "impairment": balance.impairment if balance else ZERO,
                "actual_book_value": balance.book_value if balance else ZERO,
            })
        rows.append(row)
    return rows


def _depreciation_rows(*, actor, company, report_key, filters):
    from apps.finance.models import DepreciationEntry, DepreciationSchedule
    from apps.finance.services import depreciable_fixed_asset_filter

    asset_ids = list(_base_assets(actor, company, filters).values_list("pk", flat=True))
    start = filters.get("period_start")
    end = filters.get("period_end")
    if report_key == "depreciation_schedule":
        qs = DepreciationSchedule.objects.filter(
            depreciable_fixed_asset_filter("asset"),
            company=company,
            asset_id__in=asset_ids,
        ).select_related("asset", "depreciation_profile")
        if start:
            qs = qs.filter(period_start__lt=end + timedelta(days=1), period_end__gt=start)
        rows = []
        for item in qs.order_by("asset__asset_code", "period_start", "sequence_no"):
            rows.append({
                "asset_code": item.asset.asset_code, "asset_name": item.asset.asset_name,
                "period_start": item.period_start, "period_end": item.period_end,
                "method": item.depreciation_profile.get_method_display(),
                "theoretical_amount": item.planned_amount, "actual_amount": ZERO,
                "source": "计划/理论（不入账）",
            })
        return rows
    qs = approved_depreciation_entries(
        DepreciationEntry.objects.filter(company=company, asset_id__in=asset_ids)
    ).select_related("asset", "depreciation_profile", "value_adjustment")
    if start:
        period_end = end + timedelta(days=1)
        qs = qs.filter(
            Q(reversal_of__isnull=True, source_type="batch", period_start__gte=start, period_start__lt=period_end)
            | Q(reversal_of__isnull=True, source_type="opening", entry_date__gte=start, entry_date__lt=period_end)
            | Q(reversal_of__isnull=True, source_type="adjustment", value_adjustment__effective_date__gte=start, value_adjustment__effective_date__lt=period_end)
            | Q(reversal_of__isnull=False, entry_date__gte=start, entry_date__lt=period_end)
        )
    if report_key == "monthly_depreciation":
        grouped = {}
        for item in qs.order_by("asset__asset_code", "period_start", "created_at"):
            month_start = depreciation_entry_business_date(item).replace(day=1)
            if month_start.month == 12:
                month_end = date(month_start.year + 1, 1, 1)
            else:
                month_end = date(month_start.year, month_start.month + 1, 1)
            key = (item.asset_id, month_start)
            row = grouped.setdefault(
                key,
                {
                    "asset_code": item.asset.asset_code,
                    "asset_name": item.asset.asset_name,
                    "period_start": month_start,
                    "period_end": month_end,
                    "method": item.depreciation_profile.get_method_display(),
                    "theoretical_amount": None,
                    "actual_amount": ZERO,
                    "source": "已过账分录代数净额",
                },
            )
            row["actual_amount"] = money(row["actual_amount"] + item.amount)
        return sorted(
            grouped.values(), key=lambda row: (row["asset_code"], row["period_start"])
        )
    rows = []
    for item in qs.order_by("asset__asset_code", "period_start", "created_at"):
        business_date = depreciation_entry_business_date(item)
        rows.append({
            "asset_code": item.asset.asset_code, "asset_name": item.asset.asset_name,
            "period_start": item.period_start if item.source_type == "batch" and not item.reversal_of_id else business_date,
            "period_end": item.period_end if item.source_type == "batch" and not item.reversal_of_id else business_date + timedelta(days=1),
            "method": item.depreciation_profile.get_method_display(),
            "theoretical_amount": None, "actual_amount": item.amount,
            "source": "冲销" if item.reversal_of_id else item.get_source_type_display(),
        })
    return rows


def _inventory_rows(*, actor, company, differences_only, filters):
    from apps.inventory.models import InventoryTaskAsset
    from apps.inventory.permissions import scoped_inventory_tasks

    task_ids = scoped_inventory_tasks(actor, company).values_list("pk", flat=True)
    qs = InventoryTaskAsset.objects.filter(company=company, inventory_task_id__in=task_ids).select_related("inventory_task").prefetch_related("scans", "resolutions")
    if filters.get("department"):
        qs = qs.filter(expected_department_id=filters["department"])
    if filters.get("category"):
        from apps.masterdata.models import AssetCategory

        category_name = AssetCategory.objects.values_list("name", flat=True).get(
            company=company, pk=filters["category"]
        )
        qs = qs.filter(expected_category_snapshot=category_name)
    if filters.get("responsible_employee"):
        qs = qs.filter(expected_employee_id=filters["responsible_employee"])
    if filters.get("asset_status"):
        qs = qs.filter(expected_asset_status=filters["asset_status"])
    if filters.get("period_start"):
        qs = qs.filter(
            inventory_task__planned_start__lte=filters["period_end"],
            inventory_task__planned_end__gte=filters["period_start"],
        )
    if differences_only:
        qs = qs.exclude(inventory_status="normal")
    rows = []
    for item in qs.order_by("inventory_task__task_code", "expected_code_snapshot"):
        scan = next((s for s in item.scans.all() if s.is_effective), None)
        resolution = next((r for r in item.resolutions.all() if r.status == "active"), None)
        rows.append({
            "task_code": item.inventory_task.task_code,
            "asset_code": item.expected_code_snapshot,
            "asset_name": item.expected_name_snapshot,
            "expected_department": item.expected_department_snapshot,
            "expected_employee": item.expected_employee_snapshot,
            "expected_location": item.expected_location_path_snapshot,
            "inventory_status": item.get_inventory_status_display(),
            "scan_result": scan.get_result_display() if scan else "未扫描",
            "resolution": resolution.conclusion if resolution else "",
        })
    return rows


def _maintenance_rows(*, actor, company, report_key, filters):
    from apps.maintenance.models import MaintenanceRecord
    from apps.maintenance.permissions import scoped_maintenance_plans

    today = filters.get("as_of_date") or timezone.localdate()
    plans = scoped_maintenance_plans(actor, company).select_related("asset", "responsible_employee")
    if filters.get("department"):
        plans = plans.filter(asset__department_id=filters["department"])
    if filters.get("category"):
        plans = plans.filter(asset__category_id=filters["category"])
    if filters.get("responsible_employee"):
        plans = plans.filter(responsible_employee_id=filters["responsible_employee"])
    if filters.get("asset_status"):
        plans = plans.filter(asset__asset_status=filters["asset_status"])
    if report_key == "maintenance_records":
        qs = MaintenanceRecord.objects.filter(company=company, maintenance_plan_id__in=plans.values("pk")).select_related("asset", "maintenance_plan", "completed_by")
        if filters.get("period_start"):
            qs = qs.filter(
                completed_date__gte=filters["period_start"],
                completed_date__lte=filters["period_end"],
            )
        return [{
            "asset_code": r.asset.asset_code, "asset_name": r.asset.asset_name,
            "plan_name": r.maintenance_plan.name, "scheduled_date": r.scheduled_date,
            "completed_date": r.completed_date, "completed_by": getattr(r.completed_by, "name", ""),
            "result": r.get_result_display(), "status": r.get_status_display(), "remark": r.remark,
        } for r in qs.order_by("completed_date", "asset__asset_code")]
    rows = []
    for plan in plans.order_by("next_maintenance_date", "asset__asset_code"):
        if plan.status != "active":
            due = "不适用"
        elif today > plan.next_maintenance_date:
            due = "逾期"
        elif today >= plan.next_maintenance_date - timedelta(days=plan.advance_notice_days):
            due = "即将到期"
        else:
            due = "未到提醒期"
        if report_key == "maintenance_due" and due not in {"逾期", "即将到期"}:
            continue
        due_scope = filters.get("maintenance_due_scope")
        if due_scope == "upcoming" and due != "即将到期":
            continue
        if due_scope == "overdue" and due != "逾期":
            continue
        rows.append({
            "asset_code": plan.asset.asset_code, "asset_name": plan.asset.asset_name,
            "plan_name": plan.name, "responsible_employee": plan.responsible_employee.name,
            "cycle": f"{plan.cycle_value}{plan.get_cycle_unit_display()}",
            "next_due_date": plan.next_maintenance_date, "due_status": due,
            "status": plan.get_status_display(),
        })
    return rows


def _offboarding_rows(*, actor, company, filters):
    from apps.offboarding.domain import UNRESOLVED_ITEM_RESOLUTIONS
    from apps.offboarding.permissions import scoped_clearance_items

    qs = scoped_clearance_items(actor, company).filter(
        resolution__in=UNRESOLVED_ITEM_RESOLUTIONS
    )
    if "_authorized_clearance_item_ids" in filters:
        qs = qs.filter(pk__in=filters["_authorized_clearance_item_ids"])
    qs = qs.select_related("clearance__employee")
    return [{
        "employee_no": item.clearance.employee.employee_no,
        "employee_name": item.clearance.employee.name,
        "asset_code": item.asset_code_snapshot, "asset_name": item.asset_name_snapshot,
        "source_type": item.get_source_type_display(),
        "original_department": item.original_department_snapshot,
        "original_location": item.original_location_path_snapshot,
        "resolution": item.get_resolution_display(),
    } for item in qs.order_by("clearance__employee__employee_no", "asset_code_snapshot")]


def _disposal_rows(*, actor, company, filters):
    from apps.assets.models import AssetDisposal

    asset_ids = _base_assets(actor, company, {**filters, "include_disposed": True}).values("pk")
    qs = AssetDisposal.objects.filter(company=company, asset_id__in=asset_ids).select_related("asset")
    start, end = filters.get("period_start"), filters.get("period_end")
    if start:
        qs = qs.filter(actual_disposal_date__gte=start, actual_disposal_date__lte=end)
    return [{
        "asset_code": d.asset.asset_code, "asset_name": d.asset.asset_name,
        "disposal_type": d.get_disposal_type_display(),
        "actual_disposal_date": d.actual_disposal_date, "status": d.get_status_display(),
        "original_cost_snapshot": d.original_cost_snapshot,
        "accumulated_depreciation_snapshot": d.actual_accumulated_depreciation_snapshot,
        "impairment_snapshot": d.impairment_snapshot,
        "book_value_snapshot": d.book_value_snapshot,
        "disposal_income": d.disposal_income,
    } for d in qs.order_by("actual_disposal_date", "asset__asset_code")]


def build_report_dataset(*, actor, company, report_key, filters=None):
    require_view_report(actor, report_key)
    definition = get_report_definition(report_key)
    if definition.supply:
        from apps.reports.supply_queries import build_supply_report_dataset

        return build_supply_report_dataset(
            actor=actor,
            company=company,
            report_key=report_key,
            filters=filters,
        )
    if definition.tplus:
        raise ReportValidationError(("请使用 T+ 专用查询接口。",))
    with transaction.atomic():
        _begin_consistent_read()
        snapshot_at = timezone.now()
        clean = _validated_filters(actor=actor, company=company, filters=filters)
        if clean.get("asset_scope") and report_key not in {
            "asset_ledger", "department_assets"
        }:
            raise ReportValidationError(("资产范围筛选不适用于当前报表。",))
        if clean.get("label_scope") and (
            report_key != "asset_ledger" or clean.get("asset_scope") != "managed"
        ):
            raise ReportValidationError(("标签范围筛选只用于在管资产总账。",))
        if clean.get("maintenance_due_scope") and report_key != "maintenance_due":
            raise ReportValidationError(("保养到期范围筛选不适用于当前报表。",))
        if report_key in {"asset_ledger", "fixed_asset_detail", "department_assets", "employee_assets", "equipment_list", "mold_tool_inspection_list"}:
            rows = _asset_rows(actor=actor, company=company, report_key=report_key, filters=clean)
        elif report_key in {"depreciation_schedule", "depreciation_detail", "monthly_depreciation"}:
            rows = _depreciation_rows(actor=actor, company=company, report_key=report_key, filters=clean)
        elif report_key in {"inventory_results", "inventory_differences"}:
            rows = _inventory_rows(
                actor=actor, company=company,
                differences_only=report_key == "inventory_differences",
                filters=clean,
            )
        elif report_key in {"maintenance_plans", "maintenance_due", "maintenance_records"}:
            rows = _maintenance_rows(actor=actor, company=company, report_key=report_key, filters=clean)
        elif report_key == "offboarding_unresolved":
            rows = _offboarding_rows(actor=actor, company=company, filters=clean)
        elif report_key == "disposal_list":
            rows = _disposal_rows(actor=actor, company=company, filters=clean)
        else:
            raise ReportValidationError(("未知报表查询。",))
        return ReportDataset(
            definition=definition, rows=_frozen_rows(rows),
            filters=MappingProxyType(clean), data_snapshot_at=snapshot_at,
        )


def _validate_tplus_period(*, company, period_start, period_end):
    from apps.assets.models import Asset
    from apps.finance.models import DepreciationBatch, DepreciationEntry

    errors, warnings = [], []
    open_batches = DepreciationBatch.objects.filter(
        company=company, period_start__lt=period_end, period_end__gt=period_start,
        status="draft",
    ).values_list("pk", flat=True)
    if open_batches:
        errors.append("期间存在未确认折旧批次：" + "、".join(map(str, open_batches)))
    missing = Asset.objects.filter(company=company).exclude(asset_status__in=("draft", "pending_finance")).filter(
        Q(finance__isnull=True) | Q(finance__finance_confirmed_at__isnull=True)
    ).values_list("asset_code", flat=True)
    if missing:
        errors.append("正式资产缺少已确认财务数据：" + "、".join(code or "(无编号)" for code in missing))
    reversals = approved_depreciation_entries(
        DepreciationEntry.objects.filter(
            company=company,
            reversal_of__isnull=False,
            entry_date__gte=period_start,
            entry_date__lt=period_end,
        )
    ).select_related("reversal_of")
    broken = [str(e.pk) for e in reversals if e.asset_id != e.reversal_of.asset_id or e.amount != -e.reversal_of.amount]
    if broken:
        errors.append("折旧冲销链金额或资产不一致：" + "、".join(broken))
    reversed_entries = DepreciationEntry.objects.filter(
        company=company, batch_item__batch__status="reversed",
        reversal__isnull=True,
    ).values_list("pk", flat=True)
    if reversed_entries:
        errors.append("已冲销批次存在断链原分录：" + "、".join(map(str, reversed_entries)))
    return errors, warnings


def build_tplus_dataset(*, actor, company, period_start, period_end, filters=None):
    require_tplus_export(actor)
    if not isinstance(period_start, date) or not isinstance(period_end, date) or period_end <= period_start:
        raise ReportValidationError(("T+ 期间必须是有效半开日期区间。",))
    with transaction.atomic():
        _begin_consistent_read()
        snapshot_at = timezone.now()
        clean = _validated_filters(
            actor=actor, company=company,
            filters={**(filters or {}), "period_start": period_start, "period_end": period_end},
        )
        errors, warnings = _validate_tplus_period(company=company, period_start=period_start, period_end=period_end)
        if errors:
            raise ReportValidationError(errors, warnings=warnings)
        include_disposed = clean.get("include_disposed", True)
        assets = list(_base_assets(actor, company, {**clean, "include_drafts": False, "include_disposed": include_disposed}).filter(
            finance__finance_confirmed_at__isnull=False,
            finance__accounting_treatment="fixed_asset",
        ))
        if include_disposed:
            from apps.assets.models import AssetDisposal
            from apps.finance.models import AssetValueAdjustment, DepreciationEntry

            terminal_ids = [asset.pk for asset in assets if asset.asset_status in TERMINAL_STATUSES]
            active_terminal_ids = set(
                approved_depreciation_entries(
                    DepreciationEntry.objects.filter(
                        company=company, asset_id__in=terminal_ids
                    )
                )
                .filter(
                    Q(period_start__gte=period_start, period_start__lt=period_end)
                    | Q(entry_date__gte=period_start, entry_date__lt=period_end)
                )
                .values_list("asset_id", flat=True)
            )
            active_terminal_ids.update(
                AssetValueAdjustment.objects.filter(
                    company=company,
                    asset_id__in=terminal_ids,
                    status__in=("confirmed", "reversed"),
                    effective_date__gte=period_start,
                    effective_date__lt=period_end,
                ).values_list("asset_id", flat=True)
            )
            active_terminal_ids.update(
                AssetDisposal.objects.filter(
                    company=company,
                    asset_id__in=terminal_ids,
                    actual_disposal_date__gte=period_start,
                    actual_disposal_date__lt=period_end,
                    status__in=("confirmed", "reversed"),
                ).values_list("asset_id", flat=True)
            )
            assets = [
                asset for asset in assets
                if asset.asset_status not in TERMINAL_STATUSES or asset.pk in active_terminal_ids
            ]
        asset_ids = [asset.pk for asset in assets]
        ending = balances_by_asset(company=company, asset_ids=asset_ids, boundary=period_end)
        components = tplus_period_components(
            company=company, asset_ids=asset_ids,
            period_start=period_start, period_end=period_end,
        )
        from apps.assets.models import AssetDisposal, AssetExternalReference
        from apps.finance.models import AssetDepreciationProfile

        references = {
            row.asset_id: row.reference_value
            for row in AssetExternalReference.objects.filter(
                company=company, asset_id__in=asset_ids,
                external_system="TPLUS", reference_type="asset_card_code",
            )
        }
        profiles = {}
        for profile in AssetDepreciationProfile.objects.filter(
            company=company, asset_id__in=asset_ids,
            effective_from__lt=period_end,
        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=period_start)).order_by("asset_id", "-version"):
            profiles.setdefault(profile.asset_id, profile)
        disposals = {
            row.asset_id: row
            for row in AssetDisposal.objects.filter(
                company=company, asset_id__in=asset_ids, status="confirmed",
                actual_disposal_date__gte=period_start,
                actual_disposal_date__lt=period_end,
            )
        }
        attribution = _historical_attribution(assets, _business_boundary(company, period_end))
        rows = []
        for asset in assets:
            finance = asset.finance
            profile = profiles.get(asset.pk)
            if finance.original_cost is None or finance.fixed_asset_category_id is None or profile is None:
                errors.append(f"资产 {asset.asset_code} 缺少 T+ 对账所需财务/Profile 数据。")
                continue
            balance = ending[asset.pk]
            comp = components[asset.pk]
            ending_ad = money(
                comp["opening_accumulated_depreciation"]
                + comp["automatic_depreciation"] + comp["manual_depreciation"]
                + comp["adjustment_net"] + comp["reversal_net"]
            )
            if ending_ad != balance.accumulated_depreciation:
                errors.append(f"资产 {asset.asset_code} 期末累计折旧与分项加法不一致。")
            disposal = disposals.get(asset.pk)
            at = attribution[asset.pk]
            rows.append({
                "asset_code": asset.asset_code, "tplus_card_code": references.get(asset.pk, ""),
                "asset_name": asset.asset_name, "physical_category": asset.category.name,
                "fixed_asset_category": finance.fixed_asset_category.name,
                "department": getattr(at["department"], "name", ""),
                "responsible_employee": getattr(at["responsible_employee"], "name", ""),
                "location": _location_path(at["location"]),
                "asset_status": dict(asset.AssetStatus.choices).get(at["asset_status"], at["asset_status"]),
                "commissioning_date": asset.commissioning_date,
                "capitalization_date": finance.capitalization_date,
                "depreciation_start_date": profile.start_date,
                "depreciation_method": profile.get_method_display(),
                "useful_life_months": profile.useful_life_months,
                "salvage_mode": profile.get_salvage_mode_display(),
                "salvage_rate": profile.salvage_rate,
                "salvage_amount": profile.salvage_amount,
                "original_cost": balance.original_cost,
                **comp,
                "ending_accumulated_depreciation": ending_ad,
                "impairment": balance.impairment,
                "ending_book_value": balance.book_value,
                "disposal_date": disposal.actual_disposal_date if disposal else None,
                "disposal_type": disposal.get_disposal_type_display() if disposal else "",
                "disposal_income": money(disposal.disposal_income) if disposal else ZERO,
                "remark": finance.finance_remark,
            })
        if errors:
            raise ReportValidationError(errors, warnings=warnings)
        entry_rows = []
        for entry in confirmed_entries_for_period(
            company=company, asset_ids=asset_ids,
            period_start=period_start, period_end=period_end,
        ):
            batch = entry.batch_item.batch if entry.batch_item_id else None
            actor_obj = batch.confirmed_by if batch else None
            source = entry.get_source_type_display()
            remark = ""
            if entry.value_adjustment_id:
                remark = entry.value_adjustment.reason
            elif batch and batch.batch_type == "reversal":
                remark = batch.reversal_reason
            elif entry.batch_item_id and entry.batch_item.manual_reason:
                remark = entry.batch_item.manual_reason
            entry_rows.append({
                "batch_code": str(batch.pk) if batch else "",
                "asset_code": entry.asset.asset_code,
                "tplus_card_code": references.get(entry.asset_id, ""),
                "period": depreciation_entry_business_date(entry).strftime("%Y-%m"),
                "entry_type": "冲销" if entry.reversal_of_id else "原始",
                "source": source, "reversal_of": str(entry.reversal_of_id or ""),
                "amount": entry.amount,
                "posted_user": getattr(entry.posted_by, "username", ""),
                "posted_at": entry.posted_at,
                "batch_actor": getattr(actor_obj, "username", ""), "remark": remark,
            })
        totals = {key: ZERO for key in TPLUS_TOTAL_METRICS}
        for row in rows:
            for key in TPLUS_TOTAL_METRICS:
                totals[key] += money(row[key])
        totals = {key: money(value) for key, value in totals.items()}
        return TplusDataset(
            definition=get_report_definition("tplus_reconciliation"),
            asset_rows=_frozen_rows(rows), entry_rows=_frozen_rows(entry_rows),
            filters=MappingProxyType(clean), data_snapshot_at=snapshot_at,
            totals=MappingProxyType(totals), warnings=tuple(warnings),
        )


def build_dashboard(*, actor, company, filters=None):
    """Return role-safe Dashboard aggregates using the same asset scope."""
    from apps.inventory.models import InventorySurplus, InventoryTaskAsset
    from apps.inventory.permissions import scoped_inventory_tasks
    from apps.assets.models import AssetQrIdentity
    from apps.finance.models import DepreciationEntry
    from apps.maintenance.domain import due_status
    from apps.maintenance.permissions import scoped_maintenance_plans
    from apps.masterdata.permissions import role_names_for
    from apps.offboarding.domain import UNRESOLVED_ITEM_RESOLUTIONS
    from apps.offboarding.permissions import scoped_clearance_items

    roles = role_names_for(actor)
    if not roles:
        raise PermissionDenied("您没有查看 Dashboard 的权限。")
    with transaction.atomic():
        _begin_consistent_read()
        snapshot_at = timezone.now()
        clean = _validated_filters(actor=actor, company=company, filters=filters)
        if roles == {"hr"}:
            offboarding_unresolved = scoped_clearance_items(actor, company).filter(
                resolution__in=UNRESOLVED_ITEM_RESOLUTIONS
            ).count()
            return {
                "data_snapshot_at": snapshot_at,
                "pending": {"offboarding_unresolved": offboarding_unresolved},
            }
        assets = _base_assets(actor, company, {**clean, "include_drafts": True, "include_disposed": True})
        managed = assets.filter(asset_status__in=MANAGED_STATUSES)
        day = clean.get("as_of_date") or timezone.localdate()
        task_scope = scoped_inventory_tasks(actor, company)
        in_progress_task_ids = task_scope.filter(status="in_progress").values("pk")
        reconciliation_task_ids = task_scope.filter(status="reconciliation").values("pk")
        inventory_pending = InventoryTaskAsset.objects.filter(
            company=company,
            inventory_task_id__in=in_progress_task_ids,
            inventory_status="pending",
        ).count()
        inventory_exceptions = InventoryTaskAsset.objects.filter(
            company=company,
            inventory_task_id__in=reconciliation_task_ids,
            inventory_status__in=("exception", "missing"),
        ).count() + InventorySurplus.objects.filter(
            company=company,
            inventory_task_id__in=reconciliation_task_ids,
            resolution_status="pending",
        ).count()
        plan_scope = scoped_maintenance_plans(actor, company).filter(status="active")
        # Keep the three public maintenance buckets stable for existing home
        # and due-list consumers. The Dashboard card combines today's items
        # with upcoming, while callers can still reconcile exact counts.
        maintenance_counts = {"upcoming": 0, "due_today": 0, "overdue": 0}
        due_plan_ids = []
        for plan_id, next_due, advance_notice in plan_scope.values_list(
            "pk", "next_maintenance_date", "advance_notice_days"
        ).order_by(
            "next_maintenance_date", "pk"
        ):
            status = (
                "overdue" if next_due < day
                else "due_today" if next_due == day
                else "upcoming" if day >= next_due - timedelta(days=advance_notice)
                else "not_due"
            )
            bucket = status if status in maintenance_counts else None
            if bucket is None:
                continue
            maintenance_counts[bucket] += 1
            if len(due_plan_ids) < 8:
                due_plan_ids.append(plan_id)
        due_plans = {
            plan.pk: plan
            for plan in plan_scope.filter(pk__in=due_plan_ids).select_related(
                "asset", "responsible_employee"
            )
        }
        maintenance_items = []
        for plan_id in due_plan_ids:
            plan = due_plans[plan_id]
            status = due_status(plan, day)
            maintenance_items.append(
                {
                    "plan": plan,
                    "id": plan.pk,
                    "asset_code": plan.asset.asset_code,
                    "name": plan.name,
                    "next_due_date": plan.next_maintenance_date,
                    "responsible_employee": plan.responsible_employee.name,
                    "due_status": status,
                }
            )
        offboarding_unresolved = scoped_clearance_items(actor, company).filter(
            resolution__in=UNRESOLVED_ITEM_RESOLUTIONS
        ).count()
        result = {
            "data_snapshot_at": snapshot_at,
            "physical": {
                "asset_total": managed.count(),
                "in_use": assets.filter(asset_status="in_use").count(),
                "idle": assets.filter(asset_status="idle").count(),
                "disposed": assets.filter(asset_status="disposed").count(),
            },
            "pending": {
                "pending_finance": assets.filter(asset_status="pending_finance").count(),
                "pending_label": managed.annotate(
                    has_attached_label=Exists(
                        AssetQrIdentity.objects.filter(
                            asset_id=OuterRef("pk"), status="active", label_status="attached"
                        )
                    )
                ).filter(has_attached_label=False).count(),
                "inventory_pending": inventory_pending,
                "inventory_exceptions": inventory_exceptions,
                "maintenance_upcoming": (
                    maintenance_counts["upcoming"] + maintenance_counts["due_today"]
                ),
                "maintenance_overdue": maintenance_counts["overdue"],
                "offboarding_unresolved": offboarding_unresolved,
            },
            "maintenance_items": tuple(maintenance_items),
            "maintenance_counts": maintenance_counts,
            "by_department": tuple(
                {
                    "id": row["department_id"],
                    "label": row["department__name"] or "未分配",
                    "count": row["count"],
                }
                for row in managed.values("department_id", "department__name")
                .annotate(count=Count("pk"))
                .order_by("department__name")
            ),
            "by_category": tuple(
                {
                    "id": row["category_id"],
                    "label": row["category__name"] or "未分类",
                    "count": row["count"],
                }
                for row in managed.values("category_id", "category__name")
                .annotate(count=Count("pk"))
                .order_by("category__name")
            ),
        }
        if roles.intersection({"finance", "management"}):
            balance_ids = list(
                assets.filter(
                    finance__accounting_treatment="fixed_asset",
                    finance__finance_confirmed_at__isnull=False,
                ).exclude(asset_status__in=TERMINAL_STATUSES).values_list("pk", flat=True)
            )
            balances = balances_by_asset(company=company, asset_ids=balance_ids, boundary=day + timedelta(days=1))
            month_start = day.replace(day=1)
            if month_start.month == 12:
                month_end = date(month_start.year + 1, 1, 1)
            else:
                month_end = date(month_start.year, month_start.month + 1, 1)
            current_month = approved_depreciation_entries(
                DepreciationEntry.objects.filter(
                    company=company,
                    # All four financial cards use the same current,
                    # non-terminal fixed-asset scope.  Otherwise an asset
                    # disposed after this month's posting would disappear
                    # from original cost/book value but remain in monthly
                    # depreciation and its drilldown.
                    asset_id__in=balance_ids,
                )
            ).filter(
                Q(
                    reversal_of__isnull=True,
                    source_type="batch",
                    period_start__gte=month_start,
                    period_start__lt=month_end,
                )
                | Q(
                    reversal_of__isnull=False,
                    entry_date__gte=month_start,
                    entry_date__lt=month_end,
                )
                | Q(
                    reversal_of__isnull=True,
                    source_type="adjustment",
                    value_adjustment__effective_date__gte=month_start,
                    value_adjustment__effective_date__lt=month_end,
                    value_adjustment__adjustment_type="depreciation_adjustment",
                )
            ).aggregate(total=Sum("amount"))["total"] or ZERO
            result["financial"] = {
                "original_cost": money(sum((b.original_cost for b in balances.values()), ZERO)),
                "accumulated_depreciation": money(sum((b.accumulated_depreciation for b in balances.values()), ZERO)),
                "book_value": money(sum((b.book_value for b in balances.values()), ZERO)),
                "current_month_depreciation": money(current_month),
            }
        return result


__all__ = [
    "ReportDataset", "ReportValidationError", "TplusDataset",
    "build_dashboard", "build_report_dataset", "build_tplus_dataset",
]
