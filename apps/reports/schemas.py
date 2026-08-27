"""Versioned, server-side report and Excel column contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class CellKind(StrEnum):
    TEXT = "text"
    IDENTIFIER = "identifier"
    INTEGER = "integer"
    DECIMAL = "decimal"
    QUANTITY = "quantity"
    UNIT_COST = "unit_cost"
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
    access: str = ""


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
    supply: bool = False


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


def _c(key, label, kind=CellKind.TEXT, width=16, access=""):
    return ColumnSchema(key, label, kind, width, access)


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


def _supply_report(key, title, sheet, columns):
    return ReportDefinition(
        key,
        title,
        sheet,
        REPORT_SCHEMA_VERSION,
        tuple(columns),
        supply=True,
    )


SQ = CellKind.QUANTITY
SC = CellKind.UNIT_COST
SM = CellKind.MONEY
SUPPLY_COST = "supply_cost"
ASSET_FINANCE = "asset_finance"

SUPPLY_STOCK_BALANCE_COLUMNS = (
    _c("warehouse_code", "仓库编码", CellKind.IDENTIFIER, 16),
    _c("warehouse_name", "仓库名称", width=20),
    _c("item_code", "物品编码", CellKind.IDENTIFIER, 18),
    _c("item_name", "物品名称", width=24),
    _c("category", "分类", width=18),
    _c("management_mode", "管理模式", width=20),
    _c("unit", "单位", width=10),
    _c("current_quantity", "当前数量", SQ, 14),
    _c("average_unit_cost", "平均单位成本", SC, 16, SUPPLY_COST),
    _c("current_amount", "当前金额", SM, 16, SUPPLY_COST),
    _c("minimum_stock", "最低库存", SQ, 14),
    _c("default_warehouse", "默认仓库", width=20),
    _c("is_low_stock", "是否低库存", CellKind.BOOLEAN, 14),
    _c("shortage_quantity", "缺口数量", SQ, 14),
    _c("last_ledger_date", "最后流水日期", CellKind.DATE, 16),
    _c("item_active", "物品启用", CellKind.BOOLEAN, 12),
)

SUPPLY_LOW_STOCK_COLUMNS = (
    _c("item_code", "物品编码", CellKind.IDENTIFIER, 18),
    _c("item_name", "物品名称", width=24),
    _c("category", "分类", width=18),
    _c("unit", "单位", width=10),
    _c("default_warehouse", "默认仓库", width=20),
    _c("current_quantity", "当前数量", SQ, 14),
    _c("minimum_stock", "最低库存", SQ, 14),
    _c("shortage_quantity", "缺口数量", SQ, 14),
    _c("last_receipt_date", "最近入库日期", CellKind.DATE, 16),
    _c("last_issue_date", "最近领用日期", CellKind.DATE, 16),
    _c("item_active", "物品启用", CellKind.BOOLEAN, 12),
    _c("configuration_status", "预警配置状态", width=18),
)


def _movement_columns():
    columns = [
        _c("warehouse", "仓库", width=20),
        _c("item_code", "物品编码", CellKind.IDENTIFIER, 18),
        _c("item_name", "物品名称", width=24),
        _c("unit", "单位", width=10),
    ]
    for key, label in (
        ("opening", "期初"), ("receipt", "入库"), ("return", "退回"),
        ("transfer_in", "调入"), ("count_gain", "盘盈"),
        ("issue", "领用"), ("transfer_out", "调出"),
        ("count_loss", "盘亏"), ("ending", "期末"),
    ):
        columns.append(_c(f"{key}_quantity", f"{label}数量", SQ, 14))
        columns.append(
            _c(f"{key}_amount", f"{label}金额", SM, 16, SUPPLY_COST)
        )
    return tuple(columns)


SUPPLY_STOCK_LEDGER_COLUMNS = (
    _c("business_date", "业务日期", CellKind.DATE, 14),
    _c("created_at", "创建时间", CellKind.DATETIME, 20),
    _c("document_no", "单据号", CellKind.IDENTIFIER, 20),
    _c("original_document_type", "原单据类型", width=16),
    _c("movement_type", "流水类型", width=16),
    _c("warehouse", "仓库", width=20),
    _c("item", "物品", width=26),
    _c("unit", "单位", width=10),
    _c("quantity_delta", "数量变动", SQ, 14),
    _c("amount_delta", "金额变动", SM, 16, SUPPLY_COST),
    _c("quantity_before", "变动前数量", SQ, 14),
    _c("quantity_after", "变动后数量", SQ, 14),
    _c("amount_before", "变动前金额", SM, 16, SUPPLY_COST),
    _c("amount_after", "变动后金额", SM, 16, SUPPLY_COST),
    _c("posting_unit_cost", "过账单位成本", SC, 16, SUPPLY_COST),
    _c("original_ledger", "原流水", CellKind.IDENTIFIER, 38),
    _c("reversed_by_ledger", "冲销流水", CellKind.IDENTIFIER, 38),
    _c("operator", "操作人", width=16),
    _c("count_task", "盘点任务", CellKind.IDENTIFIER, 20),
)

SUPPLY_ISSUE_DETAIL_COLUMNS = (
    _c("business_date", "业务日期", CellKind.DATE, 14),
    _c("document_no", "单据号", CellKind.IDENTIFIER, 20),
    _c("business_type", "业务类型", width=14),
    _c("department", "部门", width=18),
    _c("employee", "员工", width=16),
    _c("item", "物品", width=26),
    _c("management_mode", "管理模式", width=20),
    _c("unit", "单位", width=10),
    _c("quantity", "数量", SQ, 14),
    _c("unit_cost", "单位成本", SC, 16, SUPPLY_COST),
    _c("amount", "金额", SM, 16, SUPPLY_COST),
    _c("original_issue_document", "原领用单", CellKind.IDENTIFIER, 20),
    _c("current_net_quantity", "当前净领用数量", SQ, 16),
    _c("current_net_amount", "当前净领用金额", SM, 18, SUPPLY_COST),
)


def _issue_summary_columns(*, employee=False):
    columns = [_c("department", "部门", width=18)]
    if employee:
        columns.append(_c("employee", "员工", width=16))
    columns.extend(
        (
            _c("item_code", "物品编码", CellKind.IDENTIFIER, 18),
            _c("item_name", "物品名称", width=24),
            _c("unit", "单位", width=10),
            _c("management_mode", "管理模式", width=20),
            _c("issue_quantity", "领用数量", SQ, 14),
            _c("issue_amount", "领用金额", SM, 16, SUPPLY_COST),
            _c("return_quantity", "退回数量", SQ, 14),
            _c("return_amount", "退回金额", SM, 16, SUPPLY_COST),
            _c("net_quantity", "净领用数量", SQ, 14),
            _c("net_amount", "净领用金额", SM, 16, SUPPLY_COST),
        )
    )
    return tuple(columns)


SUPPLY_CUSTODY_BALANCE_COLUMNS = (
    _c("custody_id", "保管 ID", CellKind.IDENTIFIER, 38),
    _c("item", "物品", width=26),
    _c("unit", "单位", width=10),
    _c("department", "当前责任部门", width=18),
    _c("employee", "当前责任员工", width=16),
    _c("current_quantity", "当前数量", SQ, 14),
    _c("unit_cost", "单位成本快照", SC, 16, SUPPLY_COST),
    _c("current_amount", "当前金额", SM, 16, SUPPLY_COST),
    _c("status", "状态", width=12),
    _c("started_on", "开始日期", CellKind.DATE, 14),
    _c("root_source_type", "根来源类型", width=14),
    _c("source_reference", "原领用单/期初批次", width=22),
    _c("parent_custody", "上级保管", CellKind.IDENTIFIER, 38),
    _c("last_action_date", "最近动作日期", CellKind.DATE, 16),
    _c("in_active_count", "处于盘点", CellKind.BOOLEAN, 12),
    _c("pending_clearance", "待处理清退", CellKind.BOOLEAN, 14),
)

SUPPLY_CUSTODY_MOVEMENT_COLUMNS = (
    _c("business_date", "业务日期", CellKind.DATE, 14),
    _c("action", "动作", width=14),
    _c("item", "物品", width=26),
    _c("unit", "单位", width=10),
    _c("from_department", "来源部门", width=18),
    _c("from_employee", "来源员工", width=16),
    _c("to_department", "目标部门", width=18),
    _c("to_employee", "目标员工", width=16),
    _c("from_custody", "来源保管", CellKind.IDENTIFIER, 38),
    _c("to_custody", "目标保管", CellKind.IDENTIFIER, 38),
    _c("quantity", "数量", SQ, 14),
    _c("unit_cost", "单位成本", SC, 16, SUPPLY_COST),
    _c("amount", "金额", SM, 16, SUPPLY_COST),
    _c("source_document", "原业务单据", CellKind.IDENTIFIER, 20),
    _c("count_task", "盘点任务", CellKind.IDENTIFIER, 20),
    _c("count_line", "盘点行", CellKind.IDENTIFIER, 38),
    _c("clearance", "清退单", CellKind.IDENTIFIER, 38),
    _c("clearance_item", "清退项", CellKind.IDENTIFIER, 38),
    _c("original_movement", "原流水", CellKind.IDENTIFIER, 38),
    _c("reversal_movement", "反向流水", CellKind.IDENTIFIER, 38),
    _c("reason", "原因", width=28),
    _c("operator", "操作人", width=16),
)

SUPPLY_COUNT_DIFFERENCE_COLUMNS = (
    _c("task_no", "盘点任务号", CellKind.IDENTIFIER, 20),
    _c("count_domain", "盘点域", width=16),
    _c("status", "状态", width=14),
    _c("scope", "仓库/部门", width=20),
    _c("employee_scope", "员工范围", width=16),
    _c("item", "物品", width=26),
    _c("custody", "保管记录", CellKind.IDENTIFIER, 38),
    _c("expected_quantity", "应盘数量", SQ, 14),
    _c("counted_quantity", "实盘数量", SQ, 14),
    _c("difference_quantity", "差异数量", SQ, 14),
    _c("expected_amount", "应盘金额", SM, 16, SUPPLY_COST),
    _c("adjustment_unit_cost", "调整成本", SC, 16, SUPPLY_COST),
    _c("reason", "差异原因", width=28),
    _c("resolution_type", "解决类型", width=14),
    _c("adjustment_document", "调整单号", CellKind.IDENTIFIER, 20),
    _c("stock_ledger", "库存调整流水", CellKind.IDENTIFIER, 38),
    _c("custody_movement", "保管解决流水", CellKind.IDENTIFIER, 38),
    _c("counted_by", "录入人", width=16),
    _c("resolved_by", "解决人", width=16),
    _c("closed_at", "关闭时间", CellKind.DATETIME, 20),
)

CONTROLLED_NON_FIXED_COLUMNS = (
    _c("asset_code", "资产编码", CellKind.IDENTIFIER, 20),
    _c("asset_name", "资产名称", width=24),
    _c("category", "分类", width=18),
    _c("department", "部门", width=18),
    _c("responsible_employee", "责任员工", width=16),
    _c("location", "位置", width=28),
    _c("original_cost", "原值", SM, 16, ASSET_FINANCE),
    _c("acquisition_date", "取得日期", CellKind.DATE, 14),
    _c("status", "状态", width=14),
    _c("qr_status", "二维码状态", width=14),
    _c("inventory_status", "盘点状态", width=14),
    _c("offboarding_status", "离职清退状态", width=16),
)

SUPPLY_MANAGEMENT_AMOUNT_COLUMNS = (
    _c("component", "管理口径", width=30),
    _c("quantity", "数量", SQ, 14),
    _c("unit", "单位", width=10),
    _c("supply_amount", "库存/保管管理金额", SM, 20, SUPPLY_COST),
    _c("asset_original_cost", "逐件资产原值", SM, 18, ASSET_FINANCE),
    _c("note", "说明", width=56),
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

SUPPLY_REPORT_REGISTRY = {
    "supply_stock_balance": _supply_report("supply_stock_balance", "当前库存余额表", "当前库存余额", SUPPLY_STOCK_BALANCE_COLUMNS),
    "supply_low_stock": _supply_report("supply_low_stock", "低库存预警表", "低库存预警", SUPPLY_LOW_STOCK_COLUMNS),
    "supply_stock_movement": _supply_report("supply_stock_movement", "库存收发存表", "库存收发存", _movement_columns()),
    "supply_stock_ledger": _supply_report("supply_stock_ledger", "库存流水明细表", "库存流水明细", SUPPLY_STOCK_LEDGER_COLUMNS),
    "supply_issue_detail": _supply_report("supply_issue_detail", "领用明细表", "领用明细", SUPPLY_ISSUE_DETAIL_COLUMNS),
    "supply_department_issue": _supply_report("supply_department_issue", "部门领用汇总表", "部门领用汇总", _issue_summary_columns()),
    "supply_employee_issue": _supply_report("supply_employee_issue", "员工领用汇总表", "员工领用汇总", _issue_summary_columns(employee=True)),
    "supply_custody_balance": _supply_report("supply_custody_balance", "数量型耐用品保管余额表", "耐用品保管余额", SUPPLY_CUSTODY_BALANCE_COLUMNS),
    "supply_custody_movement": _supply_report("supply_custody_movement", "保管动作明细表", "保管动作明细", SUPPLY_CUSTODY_MOVEMENT_COLUMNS),
    "supply_count_difference": _supply_report("supply_count_difference", "盘点差异及处理结果表", "盘点差异处理", SUPPLY_COUNT_DIFFERENCE_COLUMNS),
    "controlled_non_fixed_assets": _supply_report("controlled_non_fixed_assets", "逐件受控非固定资产清单", "逐件受控非固定资产", CONTROLLED_NON_FIXED_COLUMNS),
    "supply_management_amount": _supply_report("supply_management_amount", "低值物品综合管理金额表", "综合管理金额", SUPPLY_MANAGEMENT_AMOUNT_COLUMNS),
}

ALL_REPORT_REGISTRY = {**REPORT_REGISTRY, **SUPPLY_REPORT_REGISTRY}

TOTAL_METRIC_REGISTRY = {
    REPORT_SCHEMA_VERSION: frozenset(),
    TPLUS_SCHEMA_VERSION: frozenset(TPLUS_TOTAL_METRICS),
}


def get_report_definition(report_key: str) -> ReportDefinition:
    try:
        return ALL_REPORT_REGISTRY[report_key]
    except KeyError as exc:
        raise ValueError("未知报表类型。") from exc


SUPPLY_REPORT_KEYS = tuple(SUPPLY_REPORT_REGISTRY)


def report_choices(*, include_tplus=True, include_supply=False):
    return tuple(
        (item.key, item.title)
        for item in ALL_REPORT_REGISTRY.values()
        if (include_tplus or not item.tplus) and (include_supply or not item.supply)
    )


def visible_report_definition(
    report_key: str,
    *,
    include_supply_cost: bool,
    include_asset_finance: bool,
) -> ReportDefinition:
    definition = get_report_definition(report_key)
    allowed = {
        "": True,
        SUPPLY_COST: include_supply_cost,
        ASSET_FINANCE: include_asset_finance,
    }
    columns = tuple(
        column for column in definition.columns if allowed.get(column.access, False)
    )
    if columns == definition.columns:
        return definition
    return replace(definition, columns=columns)


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
    "SUPPLY_REPORT_REGISTRY", "ALL_REPORT_REGISTRY",
    "REPORT_SCHEMA_VERSION", "TOTAL_METRIC_REGISTRY", "TPLUS_ASSET_COLUMNS",
    "TPLUS_ENTRY_COLUMNS", "TPLUS_SCHEMA_VERSION", "TPLUS_TOTAL_METRICS",
    "SUPPLY_REPORT_KEYS", "SUPPLY_COST", "ASSET_FINANCE",
    "get_report_definition", "report_choices", "required_total_metrics",
    "validate_totals", "visible_report_definition",
]
