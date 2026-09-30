"""Additive summaries of authorized report rows; never sum prices or balances in a ledger."""

from decimal import Decimal

from apps.reports.schemas import CellKind, ColumnSchema


# Only explicitly additive columns are listed. Per-record unit prices, running
# balances and repeated root-issue balances must not become report totals.
SUMMARY_SPECS = {
    "asset_ledger": (("asset_status",), ()),
    "department_assets": (("department",), ()),
    "employee_assets": (("department", "responsible_employee"), ()),
    "fixed_asset_detail": (("fixed_asset_category",), ("original_cost", "actual_accumulated_depreciation", "impairment", "actual_book_value")),
    "depreciation_schedule": (("schedule_status", "period_start", "period_end"), ("theoretical_amount",)),
    "depreciation_detail": (("source",), ("actual_amount",)),
    "monthly_depreciation": (("period_start", "period_end"), ("opening_amount", "depreciation_amount", "adjustment_amount", "reversal_amount", "actual_amount")),
    "inventory_results": (("task_code", "inventory_status"), ()),
    "inventory_differences": (("task_code", "inventory_status"), ()),
    "maintenance_plans": (("due_status", "status"), ()),
    "maintenance_due": (("due_status",), ()),
    "maintenance_records": (("status", "result"), ()),
    "offboarding_unresolved": (("employee_no", "employee_name", "resolution"), ()),
    "disposal_list": (("status",), ("original_cost_snapshot", "accumulated_depreciation_snapshot", "impairment_snapshot", "book_value_snapshot", "disposal_income")),
    "supply_stock_balance": (("warehouse_code", "warehouse_name", "unit"), ("current_quantity", "current_amount", "shortage_quantity")),
    "supply_low_stock": (("configuration_status", "unit"), ("current_quantity", "shortage_quantity")),
    "supply_stock_movement": (("warehouse", "unit"), tuple(f"{prefix}_{suffix}" for prefix in ("opening", "receipt", "return", "issue", "ending") for suffix in ("quantity", "amount"))),
    "supply_stock_ledger": (("movement_type", "unit"), ("quantity_delta", "amount_delta")),
    "supply_issue_detail": (("business_type", "unit"), ("quantity", "amount")),
    "supply_department_issue": (("department", "unit"), ("issue_quantity", "return_quantity", "net_quantity", "issue_amount", "return_amount", "net_amount")),
    "supply_employee_issue": (("department", "employee", "unit"), ("issue_quantity", "return_quantity", "net_quantity", "issue_amount", "return_amount", "net_amount")),
    "supply_custody_balance": (("status", "unit"), ("current_quantity", "current_amount")),
    "supply_custody_movement": (("action", "unit"), ("quantity", "amount")),
    "supply_count_difference": (("task_no", "status", "resolution_type"), ()),
    "controlled_non_fixed_assets": (("department",), ("original_cost",)),
    "supply_management_amount": ((), ()),
}

SEPARATE_TOTALS = {"disposal_list", "supply_issue_detail", "supply_custody_movement", "depreciation_schedule"}
SUMMARY_NOTES = {
    "disposal_list": "处置金额按状态分别合计，未完成及已撤销记录不并入一个总额。",
    "supply_issue_detail": "领用、退回及冲销分别汇总；当前净领用在多笔明细中可能重复，未重复加总。",
    "supply_custody_movement": "按保管动作分别汇总，转交等动作不代表新增库存或费用。",
    "supply_stock_ledger": "合计为流水变动净额，变动前后余额和单位成本不累加。",
    "supply_management_amount": "包含管理金额小计，逐件资产原值与数量型管理金额分别列示，不重复加总。",
    "depreciation_schedule": "理论计划按状态分别汇总，已替代计划不与现行计划合并，不代表账面实际折旧。",
    "monthly_depreciation": "累计折旧变动净额 = 期初接续累计折旧 + 本期计提 + 折旧调整 + 冲销净额。期初接续单独列示。",
}


class ReportSummary:
    """Accumulate the exact row stream being displayed or written to Excel."""

    def __init__(self, definition):
        self.key = definition.key
        group_keys, metric_keys = SUMMARY_SPECS.get(self.key, ((), ()))
        visible = {column.key: column for column in definition.columns}
        self.group_columns = tuple(visible[name] for name in group_keys if name in visible)
        self.metrics = tuple(visible[name] for name in metric_keys if name in visible)
        self.groups = {}
        self.totals = {column.key: None for column in self.metrics}
        self.missing = {column.key: 0 for column in self.metrics}
        self.count = 0
        self.departments = set()
        self.employees = set()
        self.equipment_numbers = 0

    def add(self, row):
        self.count += 1
        if self.key in {"asset_ledger", "department_assets", "employee_assets"}:
            identities = row.get("_summary_identity", {})
            if row.get("department"):
                self.departments.add(identities.get("department", row["department"]))
            if row.get("responsible_employee"):
                self.employees.add(identities.get("responsible_employee", row["responsible_employee"]))
            self.equipment_numbers += bool(row.get("equipment_number"))
        if not self.group_columns:
            return
        identity = tuple(row.get("_summary_identity", {}).get(column.key, row.get(column.key)) for column in self.group_columns)
        bucket = self.groups.setdefault(identity, {
            **{column.key: row.get("_summary_labels", {}).get(column.key, row.get(column.key)) for column in self.group_columns},
            "_filter_ids": {column.key: row.get("_summary_identity", {}).get(column.key) for column in self.group_columns},
            "record_count": 0, **{column.key: None for column in self.metrics},
        })
        bucket["record_count"] += 1
        for column in self.metrics:
            value = row.get(column.key)
            if value is None or value == "":
                self.missing[column.key] += 1
                continue
            value = Decimal(str(value))
            bucket[column.key] = (bucket[column.key] or Decimal(0)) + value
            self.totals[column.key] = (self.totals[column.key] or Decimal(0)) + value

    def result(self):
        cards = [{"label": "记录数", "value": self.count, "kind": CellKind.INTEGER}]
        if self.key in {"asset_ledger", "department_assets", "employee_assets"}:
            cards.extend({"label": label, "value": value, "kind": CellKind.INTEGER} for label, value in (
                ("涉及部门", len(self.departments)), ("涉及责任人", len(self.employees)), ("设备编号已填", self.equipment_numbers)))
        if self.key not in SEPARATE_TOTALS:
            cards.extend(
                {"label": ("已知" if self.missing[column.key] else "") + column.label + "合计",
                 "value": self.totals[column.key], "kind": column.kind}
                for column in self.metrics if column.kind == CellKind.MONEY
            )
        count_column = ColumnSchema("record_count", "记录数", CellKind.INTEGER, 12)
        return {
            "cards": cards,
            "columns": (*self.group_columns, count_column, *self.metrics) if self.group_columns else (),
            "rows": tuple(self.groups.values()),
            "note": SUMMARY_NOTES.get(self.key, ""),
            "missing": tuple(f"{column.label}有 {self.missing[column.key]} 条缺失，仅合计已知值。" for column in self.metrics if self.missing[column.key]),
        }


def summarize_report(dataset):
    summary = ReportSummary(dataset.definition)
    for row in dataset.rows:
        summary.add(row)
    return summary.result()
