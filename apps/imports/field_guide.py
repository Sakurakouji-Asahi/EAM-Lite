"""Shared human-readable field help for upload pages and standard templates."""


_FIELD_HINTS = {
    "parent_code": "填写当前公司的上级部门编码；也可引用同一文件中的新部门，顶级部门留空。",
    "manager_employee_no": "填写当前公司已有员工编号，不能用姓名代替；没有经理时留空。",
    "department_code": "填写当前公司的部门档案编码，不能用部门名称代替。",
    "responsible_employee_no": "填写当前公司的责任员工编号，不能用姓名代替。",
    "employee_no": "填写员工编号，保留前导 0；不能用姓名代替。",
    "company_code": "填写当前公司的公司编码，不能用公司名称代替。",
    "warehouse_code": "填写当前公司已启用的仓库编码，不能用仓库名称代替。",
    "default_warehouse_code": "填写当前公司的默认仓库编码；没有默认仓库时留空。",
    "category_code": "填写分类档案编码，不能用分类名称代替。",
    "fixed_asset_category_code": "填写固定资产会计类别的档案编码。",
    "location_code": "填写当前公司的叶级位置编码，不能用位置名称代替。",
    "employment_status": "使用 active（在职）、leaving（离职办理中）或 resigned（已离职）。",
    "is_active": "填写“是”或“否”；留空默认为“是”。",
    "is_maintenance_required": "填写“是”或“否”；留空时使用系统默认值。",
    "management_attribute": "使用 FA、LV、IA、LS 或 OT；统一编码主资产需填写，组件可沿用主资产。",
    "coding_year": "填写四位取得年份；留空按购置日期年份，另行指定年份时须填写取得年份依据。",
    "coding_year_note": "取得年份与购置日期年份不同时，填写依据。",
    "parent_asset_code": "仅组件填写，使用已有正式主资产编号；本批尚未建档的草稿不能作为主资产。",
    "item_type": "使用 consumable（消耗品）或 durable_quantity（数量型耐用品）。",
    "minimum_stock_quantity": "填写不小于 0 的数量，最多 4 位小数；留空按 0 处理。",
    "unit_cost": "填写不小于 0 的单位成本，最多 6 位小数。",
    "zero_cost_reason": "单位成本为 0 时必填，说明采用 0 成本的原因。",
    "accounting_treatment": "使用 fixed_asset（固定资产）或 controlled_non_fixed（受控非固定资产）。",
    "attachment_note": "仅填写后续上传说明；照片或附件在草稿创建后上传，不填写本机路径或网址。",
}

_DATE_KEYS = {
    "hire_date", "termination_date", "acquisition_date", "commissioning_date",
    "capitalization_date", "specified_start", "actual_continuation_date",
    "theoretical_as_of_date", "started_on",
}
_MACHINE_VALUE_KEYS = {"method", "posting_period", "start_rule", "stop_rule", "salvage_mode"}


def template_field_guide(definition, *, finance_keys=()):
    """Keep template order and required markers from the actual definition."""
    guide = []
    for column in definition.columns:
        key = column.key
        hint = _FIELD_HINTS.get(key, "填写实际内容；无需填写时留空。")
        if key in _DATE_KEYS:
            hint = "填写 YYYY-MM-DD 日期，例如 2026-10-04；也可使用 Excel 日期单元格。"
        elif key == "quantity":
            hint = ("每行代表一件实物，数量必须精确为 1。" if definition.import_type == "asset_initialization"
                    else "填写大于 0 的数量，最多 4 位小数。")
        elif key == "code" or key == "item_code" and definition.import_type == "item_master":
            hint = "填写新记录的唯一编码，保留前导 0；不能与已有记录重复。"
        elif key == "item_code":
            hint = "填写当前公司已启用的物品编码，不能用物品名称代替。"
        elif key in _MACHINE_VALUE_KEYS:
            hint = "使用所选折旧政策对应的系统机器值，不填写中文别名。"
        elif key.startswith("custom:"):
            hint = "按当前自定义字段设置填写；只适用于该字段所属实物分类，其他分类留空。"
        if key in finance_keys:
            hint += " 仅有财务权限时填写；没有财务权限时留空。"
        guide.append({"name": column.name, "key": key, "required": column.required, "hint": hint})
    return guide
