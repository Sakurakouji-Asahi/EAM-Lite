"""Report navigation and supported filters, shared by the server-rendered UI."""

from django.urls import reverse

from apps.reports.schemas import REPORT_REGISTRY, SUPPLY_REPORT_REGISTRY


REPORT_GROUPS = (
    ("assets", "逐件资产", ("asset_ledger", "department_assets", "employee_assets", "controlled_non_fixed_assets")),
    ("finance", "财务与折旧", ("fixed_asset_detail", "monthly_depreciation", "depreciation_detail", "depreciation_schedule", "disposal_list")),
    ("operations", "盘点、保养与清退", ("inventory_results", "inventory_differences", "maintenance_plans", "maintenance_due", "maintenance_records", "offboarding_unresolved")),
    ("supplies", "办公用品与低值品", tuple(key for key in SUPPLY_REPORT_REGISTRY if key != "controlled_non_fixed_assets")),
    ("tplus", "T+ 对账", ("tplus_reconciliation",)),
)

REPORT_DESCRIPTIONS = {
    "asset_ledger": "查看逐件资产的编号、设备编号、型号、使用部门、责任人及状态。",
    "department_assets": "按部门汇总资产记录，并核对各部门的逐件明细。",
    "employee_assets": "按部门和责任人汇总，核对人员名下的逐件资产。",
    "fixed_asset_detail": "核对已确认固定资产的原值、累计折旧、减值准备和账面净值。",
    "monthly_depreciation": "按资产、月份分别核对期初接续、本期计提、折旧调整与冲销。",
    "depreciation_detail": "核对已过账的折旧、期初、调整及冲销记录。",
    "depreciation_schedule": "查看理论折旧计划及期间汇总，计划金额不代表已入账。",
    "disposal_list": "按处置状态核对报废、出售及其他处置的财务快照。",
    "inventory_results": "查看盘点任务的应在部门、责任人、扫描结果和处理结论。",
    "inventory_differences": "查看非正常盘点记录，包括未盘、异常和处理结果。",
    "maintenance_plans": "查看保养计划、周期、责任人及下次到期日。",
    "maintenance_due": "查看基准日的即将到期和逾期保养计划。",
    "maintenance_records": "按实际完成日期查询保养记录及处理结果。",
    "offboarding_unresolved": "核对离职清退中仍未处理完成的资产及关联人员。",
    "supply_stock_balance": "查看各仓库的物品余额、低库存状态及库存成本。",
    "supply_low_stock": "核对默认仓库的库存缺口，以及尚未配置预警的物品。",
    "supply_stock_movement": "按期间核对期初、入库、领退、调拨、盘盈盘亏和期末。",
    "supply_stock_ledger": "追查每笔出入库的数量、金额变化及冲销关系。",
    "supply_issue_detail": "查看领用及退回明细、原领用单和当前净领用。",
    "supply_department_issue": "按部门、物品分别核对领用、退回和净领用。",
    "supply_employee_issue": "按员工、物品分别核对领用、退回和净领用。",
    "supply_custody_balance": "查看数量型耐用品的责任部门、保管人和当前余额。",
    "supply_custody_movement": "追查耐用品保管的转交、退回、清退及冲销记录。",
    "supply_count_difference": "核对库存和保管盘点的应盘、实盘、差异及处理单据。",
    "controlled_non_fixed_assets": "查看已认定为受控非固定资产的逐件清单及盘点清退状态。",
    "supply_management_amount": "分别查看仓库库存、开放保管及逐件资产的管理金额。",
    "tplus_reconciliation": "按会计月份生成固定资产及折旧对账文件。",
}

ASSET_FILTERS = {
    "q", "as_of_date", "department", "category", "responsible_employee",
    "asset_status", "accounting_treatment", "fixed_asset_category",
    "include_drafts", "include_disposed",
}
PERIOD_FILTERS = {
    "q", "period_start", "period_end", "department", "category",
    "responsible_employee", "asset_status",
}
FILTERS_BY_REPORT = {
    "asset_ledger": ASSET_FILTERS | {"asset_scope", "label_scope"},
    "department_assets": ASSET_FILTERS | {"asset_scope"},
    "employee_assets": ASSET_FILTERS,
    "fixed_asset_detail": ASSET_FILTERS - {"include_drafts", "accounting_treatment"},
    "depreciation_schedule": PERIOD_FILTERS | {"fixed_asset_category", "include_disposed"},
    "depreciation_detail": PERIOD_FILTERS | {"fixed_asset_category", "include_disposed"},
    "monthly_depreciation": PERIOD_FILTERS | {"fixed_asset_category", "include_disposed"},
    "inventory_results": PERIOD_FILTERS,
    "inventory_differences": PERIOD_FILTERS,
    "maintenance_plans": (PERIOD_FILTERS - {"period_start", "period_end"}) | {"as_of_date"},
    "maintenance_due": (PERIOD_FILTERS - {"period_start", "period_end"}) | {"as_of_date", "maintenance_due_scope"},
    "maintenance_records": PERIOD_FILTERS,
    "offboarding_unresolved": {"q"},
    "disposal_list": PERIOD_FILTERS | {"fixed_asset_category"},
}


def report_url(key):
    if key in SUPPLY_REPORT_REGISTRY:
        return reverse("reports:supply-report-detail", args=[key])
    if key == "tplus_reconciliation":
        return reverse("reports:tplus-export")
    return reverse("reports:report-center") + "?report_type=" + key


def report_navigation(actor, selected=""):
    from apps.reports.permissions import can_view_report

    definitions = {**REPORT_REGISTRY, **SUPPLY_REPORT_REGISTRY}
    groups = []
    for key, title, keys in REPORT_GROUPS:
        reports = [
            {"key": report_key, "title": definitions[report_key].title,
             "description": REPORT_DESCRIPTIONS[report_key], "url": report_url(report_key),
             "active": report_key == selected}
            for report_key in keys if can_view_report(actor, report_key)
        ]
        if reports:
            groups.append({"key": key, "title": title, "reports": reports,
                           "active": selected in keys, "url": reports[0]["url"]})
    return groups


def report_scope_note(key):
    if key in {"asset_ledger", "department_assets", "employee_assets", "fixed_asset_detail"}:
        return "基准日包含当天；部门、责任人、位置和状态按该日业务历史还原，其余档案信息取当前资料。"
    if key in {"monthly_depreciation", "depreciation_detail", "depreciation_schedule"}:
        return "查询起止日期均包含当天；明细中的期间结束为下一期间起点，不含当天。"
    if key.startswith("inventory_"):
        return "起止日期查询与所选期间有交集的盘点任务，部门及责任人采用盘点快照。"
    if key == "disposal_list":
        return "按实际处置日期查询，包含起止当天；不同处置状态分别汇总。"
    if key in {"maintenance_plans", "maintenance_due"}:
        return "计划取当前资料，到期状态按基准日判断。"
    if key == "maintenance_records":
        return "按实际完成日期查询，包含起止当天。"
    return ""
