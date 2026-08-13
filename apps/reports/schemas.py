"""Versioned, server-side report and Excel column contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CellKind(StrEnum):
    TEXT = "text"
    IDENTIFIER = "identifier"
    INTEGER = "integer"
    DECIMAL = "decimal"
    MONEY = "money"
    RATE = "rate"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class ColumnSchema:
    key: str
    label: str
    kind: CellKind = CellKind.TEXT
    width: int = 16


@dataclass(frozen=True, slots=True)
class ReportDefinition:
    key: str
    title: str
    sheet_name: str
    schema_version: str
    columns: tuple[ColumnSchema, ...]
    financial: bool = False
    hr_clearance: bool = False
    tplus: bool = False
    total_metrics: tuple[str, ...] = ()


REPORT_SCHEMA_VERSION = "report_v1"
TPLUS_SCHEMA_VERSION = "tplus_v1"

TPLUS_TOTAL_METRICS = (
    "original_cost",
    "opening_accumulated_depreciation",
    "automatic_depreciation",
    "manual_depreciation",
    "adjustment_net",
    "reversal_net",
    "ending_accumulated_depreciation",
    "impairment",
    "ending_book_value",
    "disposal_income",
)


def _c(key, label, kind=CellKind.TEXT, width=16):
    return ColumnSchema(key, label, kind, width)


ASSET_COLUMNS = (
    _c("asset_code", "资产编号", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称", width=24),
    _c("category", "实物分类", width=18),
    _c("model", "型号", width=18),
    _c("department", "部门", width=18),
    _c("responsible_employee", "责任人", width=14),
    _c("location", "位置", width=28),
    _c("asset_status", "资产状态", width=14),
    _c("quantity", "数量", CellKind.INTEGER, 10),
    _c("acquisition_date", "购置日期", CellKind.DATE, 14),
)

FINANCE_COLUMNS = ASSET_COLUMNS + (
    _c("accounting_treatment", "会计认定", width=18),
    _c("fixed_asset_category", "固定资产会计类别", width=22),
    _c("original_cost", "原值", CellKind.MONEY, 16),
    _c("actual_accumulated_depreciation", "实际累计折旧", CellKind.MONEY, 18),
    _c("impairment", "减值准备", CellKind.MONEY, 16),
    _c("actual_book_value", "实际账面净值", CellKind.MONEY, 18),
)

DEPRECIATION_COLUMNS = (
    _c("asset_code", "资产编号", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称", width=24),
    _c("period_start", "期间开始", CellKind.DATE, 14),
    _c("period_end", "期间结束", CellKind.DATE, 14),
    _c("method", "折旧方法", width=18),
    _c("theoretical_amount", "理论折旧（测算）", CellKind.MONEY, 18),
    _c("actual_amount", "账面实际折旧", CellKind.MONEY, 18),
    _c("source", "实际来源", width=18),
)

INVENTORY_COLUMNS = (
    _c("task_code", "盘点任务编号", CellKind.IDENTIFIER, 20),
    _c("asset_code", "资产编号快照", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称快照", width=24),
    _c("expected_department", "应在部门", width=18),
    _c("expected_employee", "应由责任人", width=16),
    _c("expected_location", "应在位置", width=28),
    _c("inventory_status", "盘点状态", width=14),
    _c("scan_result", "有效扫描结果", width=16),
    _c("resolution", "处理结论", width=28),
)

MAINTENANCE_PLAN_COLUMNS = (
    _c("asset_code", "资产编号", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称", width=24),
    _c("plan_name", "计划名称", width=24),
    _c("responsible_employee", "责任人", width=16),
    _c("cycle", "周期", width=14),
    _c("next_due_date", "下次到期日", CellKind.DATE, 14),
    _c("due_status", "到期状态", width=14),
    _c("status", "计划状态", width=14),
)

MAINTENANCE_RECORD_COLUMNS = (
    _c("asset_code", "资产编号", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称", width=24),
    _c("plan_name", "计划名称", width=24),
    _c("scheduled_date", "计划日期", CellKind.DATE, 14),
    _c("completed_date", "实际完成日期", CellKind.DATE, 14),
    _c("completed_by", "完成人", width=16),
    _c("result", "结果", width=14),
    _c("status", "记录状态", width=14),
    _c("remark", "备注", width=28),
)

OFFBOARDING_COLUMNS = (
    _c("employee_no", "员工编号", CellKind.IDENTIFIER, 18),
    _c("employee_name", "员工姓名", width=16),
    _c("asset_code", "资产编号快照", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称快照", width=24),
    _c("source_type", "关联来源", width=16),
    _c("original_department", "原部门", width=18),
    _c("original_location", "原位置", width=28),
    _c("resolution", "清退状态", width=18),
)

DISPOSAL_COLUMNS = (
    _c("asset_code", "资产编号", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称", width=24),
    _c("disposal_type", "处置类型", width=14),
    _c("actual_disposal_date", "实际处置日期", CellKind.DATE, 16),
    _c("status", "处置状态", width=14),
    _c("original_cost_snapshot", "原值快照", CellKind.MONEY, 16),
    _c("accumulated_depreciation_snapshot", "累计折旧快照", CellKind.MONEY, 18),
    _c("impairment_snapshot", "减值快照", CellKind.MONEY, 16),
    _c("book_value_snapshot", "净值快照", CellKind.MONEY, 16),
    _c("disposal_income", "处置收入", CellKind.MONEY, 16),
)

TPLUS_ASSET_COLUMNS = tuple(
    _c(key, label, kind, width)
    for key, label, kind, width in (
        ("asset_code", "EAM资产编号", CellKind.IDENTIFIER, 20),
        ("tplus_card_code", "T+资产卡片编码（可空）", CellKind.IDENTIFIER, 24),
        ("asset_name", "资产名称", CellKind.TEXT, 24),
        ("physical_category", "实物分类", CellKind.TEXT, 18),
        ("fixed_asset_category", "固定资产会计类别", CellKind.TEXT, 22),
        ("department", "使用部门", CellKind.TEXT, 18),
        ("responsible_employee", "责任人", CellKind.TEXT, 16),
        ("location", "当前位置", CellKind.TEXT, 28),
        ("asset_status", "资产状态", CellKind.TEXT, 14),
        ("commissioning_date", "达到可使用状态日期", CellKind.DATE, 18),
        ("capitalization_date", "资本化日期", CellKind.DATE, 14),
        ("depreciation_start_date", "折旧起始日期", CellKind.DATE, 16),
        ("depreciation_method", "折旧方法", CellKind.TEXT, 18),
        ("useful_life_months", "使用年限（月）", CellKind.INTEGER, 14),
        ("salvage_mode", "残值方式", CellKind.TEXT, 14),
        ("salvage_rate", "残值率", CellKind.RATE, 12),
        ("salvage_amount", "残值金额", CellKind.MONEY, 14),
        ("original_cost", "原值", CellKind.MONEY, 16),
        ("opening_accumulated_depreciation", "期初累计折旧", CellKind.MONEY, 18),
        ("automatic_depreciation", "本期自动折旧", CellKind.MONEY, 18),
        ("manual_depreciation", "本期手工折旧", CellKind.MONEY, 18),
        ("adjustment_net", "本期调整净额", CellKind.MONEY, 18),
        ("reversal_net", "本期冲销净额", CellKind.MONEY, 18),
        ("ending_accumulated_depreciation", "期末累计折旧", CellKind.MONEY, 18),
        ("impairment", "减值准备", CellKind.MONEY, 16),
        ("ending_book_value", "期末账面净值", CellKind.MONEY, 18),
        ("disposal_date", "本期处置日期", CellKind.DATE, 16),
        ("disposal_type", "处置类型", CellKind.TEXT, 14),
        ("disposal_income", "处置收入", CellKind.MONEY, 16),
        ("remark", "备注", CellKind.TEXT, 28),
    )
)

TPLUS_ENTRY_COLUMNS = (
    _c("batch_code", "批次编号", CellKind.IDENTIFIER, 24),
    _c("asset_code", "EAM资产编号", CellKind.IDENTIFIER, 20),
    _c("tplus_card_code", "T+资产卡片编码", CellKind.IDENTIFIER, 22),
    _c("period", "期间", CellKind.TEXT, 12),
    _c("entry_type", "分录类型", width=16),
    _c("source", "来源", width=20),
    _c("reversal_of", "原分录引用", CellKind.IDENTIFIER, 36),
    _c("amount", "金额", CellKind.MONEY, 16),
    _c("posted_user", "过账人", width=16),
    _c("posted_at", "过账时间", CellKind.DATETIME, 20),
    _c("batch_actor", "批次确认/冲销人", width=18),
    _c("remark", "备注", width=28),
)


def _report(key, title, sheet, columns, *, financial=False, hr=False):
    return ReportDefinition(
        key, title, sheet, REPORT_SCHEMA_VERSION, tuple(columns), financial, hr
    )


REPORT_REGISTRY = {
    "asset_ledger": _report("asset_ledger", "公司资产总账", "公司资产总账", ASSET_COLUMNS),
    "fixed_asset_detail": _report("fixed_asset_detail", "固定资产明细", "固定资产明细", FINANCE_COLUMNS, financial=True),
    "depreciation_schedule": _report("depreciation_schedule", "折旧计划", "折旧计划", DEPRECIATION_COLUMNS, financial=True),
    "depreciation_detail": _report("depreciation_detail", "折旧明细", "折旧明细", DEPRECIATION_COLUMNS, financial=True),
    "monthly_depreciation": _report("monthly_depreciation", "月度计提报表", "月度计提", DEPRECIATION_COLUMNS, financial=True),
    "department_assets": _report("department_assets", "部门资产", "部门资产", ASSET_COLUMNS),
    "employee_assets": _report("employee_assets", "人员资产", "人员资产", ASSET_COLUMNS),
    "equipment_list": _report("equipment_list", "设备清单", "设备清单", ASSET_COLUMNS),
    "mold_tool_inspection_list": _report("mold_tool_inspection_list", "模具工具检具清单", "模具工具检具", ASSET_COLUMNS),
    "inventory_results": _report("inventory_results", "盘点结果", "盘点结果", INVENTORY_COLUMNS),
    "inventory_differences": _report("inventory_differences", "盘点差异", "盘点差异", INVENTORY_COLUMNS),
    "maintenance_plans": _report("maintenance_plans", "保养计划", "保养计划", MAINTENANCE_PLAN_COLUMNS),
    "maintenance_due": _report("maintenance_due", "保养到期", "保养到期", MAINTENANCE_PLAN_COLUMNS),
    "maintenance_records": _report("maintenance_records", "保养完成记录", "保养完成记录", MAINTENANCE_RECORD_COLUMNS),
    "offboarding_unresolved": _report("offboarding_unresolved", "离职资产未清", "离职资产未清", OFFBOARDING_COLUMNS, hr=True),
    "disposal_list": _report("disposal_list", "报废出售处置清单", "处置清单", DISPOSAL_COLUMNS, financial=True),
    "tplus_reconciliation": ReportDefinition(
        "tplus_reconciliation", "T+月末人工对账", "EAM固定资产明细",
        TPLUS_SCHEMA_VERSION, TPLUS_ASSET_COLUMNS, financial=True, tplus=True,
        total_metrics=TPLUS_TOTAL_METRICS,
    ),
}

TOTAL_METRIC_REGISTRY = {
    REPORT_SCHEMA_VERSION: frozenset(),
    TPLUS_SCHEMA_VERSION: frozenset(TPLUS_TOTAL_METRICS),
}


def get_report_definition(report_key: str) -> ReportDefinition:
    try:
        return REPORT_REGISTRY[report_key]
    except KeyError as exc:
        raise ValueError("未知报表类型。") from exc


def report_choices(*, include_tplus=True):
    return tuple(
        (item.key, item.title)
        for item in REPORT_REGISTRY.values()
        if include_tplus or not item.tplus
    )


def required_total_metrics(export_type: str, schema_version: str):
    definition = get_report_definition(export_type)
    if definition.schema_version != schema_version:
        raise ValueError("报表类型与合计 schema 版本不匹配。")
    return frozenset(definition.total_metrics)


def validate_totals(export_type: str, schema_version: str, totals):
    from decimal import Decimal

    required = required_total_metrics(export_type, schema_version)
    supplied = frozenset(totals)
    if supplied != required:
        missing = sorted(required - supplied)
        unknown = sorted(supplied - required)
        raise ValueError(f"导出合计键不完整：缺少 {missing}；未知 {unknown}。")
    if any(not isinstance(value, Decimal) for value in totals.values()):
        raise TypeError("所有导出合计必须是 Decimal。")


__all__ = [
    "CellKind", "ColumnSchema", "ReportDefinition", "REPORT_REGISTRY",
    "REPORT_SCHEMA_VERSION", "TOTAL_METRIC_REGISTRY", "TPLUS_ASSET_COLUMNS",
    "TPLUS_ENTRY_COLUMNS", "TPLUS_SCHEMA_VERSION", "TPLUS_TOTAL_METRICS",
    "get_report_definition", "report_choices", "required_total_metrics",
    "validate_totals",
]
