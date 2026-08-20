"""Shared real-XLSX and database factories for Sprint 5 acceptance tests.

This module intentionally contains no tests.  It builds the versioned workbook
through the production template generator, so the import tests exercise the
same headers, metadata and ZIP container that users download.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from openpyxl import load_workbook

from apps.finance.models import DepreciationPolicy
from apps.imports.services import build_template_workbook, get_template_definition
from apps.masterdata.models import FixedAssetCategory
from tests.test_sprint3_support import (
    complete_initialization,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
)


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def sprint5_context(*, role="equipment", prefix="S5"):
    company = make_company(prefix)
    actor = make_user(f"{prefix.lower()}-{role}", role)
    complete_initialization(company, actor)
    category = make_category(company, f"{prefix}-EQ")
    department = make_department(company, f"{prefix}-D")
    employee = make_employee(company, department, f"{prefix}-E")
    _site, _area, location = make_location_tree(company, f"{prefix}-L")
    return company, actor, category, department, employee, location


def physical_row(company, category, department, employee, location, **overrides):
    row = {
        "资产名称": "初始化导入设备",
        "实物分类编码": category.code,
        "品牌": "EAM",
        "型号": "S5",
        "厂家": "测试制造商",
        "序列号": "S5-SERIAL-001",
        "出厂编号": "S5-FACTORY-001",
        "历史参考编号": "S5-OLD-001",
        "数量": 1,
        "单位": "台",
        "公司编码": company.code,
        "部门编码": department.code,
        "责任员工编号": employee.employee_no,
        "位置编码": location.code,
        "购置日期": "2024-01-01",
        "达到可使用状态日期": "2024-01-01",
        "是否需要保养": "是",
        "附件后续上传说明": "确认草稿后通过受保护入口上传照片",
        "备注": "Sprint 5 初始化",
    }
    row.update(overrides)
    return row


def add_finance_row(
    row,
    *,
    fixed_category,
    policy,
    cost="12000.00",
    opening_ad="2400.00",
    opening_impairment="0.00",
    opening_book="9600.00",
    method=None,
    **overrides,
):
    row.update(
        {
            "会计认定": "fixed_asset",
            "会计认定说明": "历史固定资产承接",
            "固定资产类别编码": fixed_category.code,
            "原值": cost,
            "资本化日期": "2024-01-01",
            "折旧政策编码": policy.policy_key,
            "折旧方法": method or policy.method,
            "计提周期": policy.posting_period,
            "起算规则": policy.start_rule,
            "指定起算日期": "2024-01-01",
            "历史起算原因": "旧资产按原始达到可使用状态日期承接",
            "停止规则": policy.stop_rule,
            "使用寿命月数": policy.default_useful_life_months,
            "残值方式": policy.default_salvage_mode,
            "残值率": str(policy.default_salvage_rate or ""),
            "残值金额": (
                str(policy.default_salvage_amount)
                if policy.default_salvage_amount is not None
                else ""
            ),
            "年度计提月份": policy.annual_posting_month or "",
            "预计总工作量": "",
            "工作量单位": "",
            "实际期初累计折旧": opening_ad,
            "期初减值": opening_impairment,
            "实际期初账面净值": opening_book,
            "实际接续日": (
                "2026-01-01"
                if Decimal(str(opening_ad or "0")) != 0
                or Decimal(str(opening_impairment or "0")) != 0
                else ""
            ),
            "理论测算截止日": "2025-12-31",
            "财务备注": "实际账面值不得被理论值覆盖",
        }
    )
    row.update(overrides)
    return row


def asset_workbook_upload(
    company,
    rows,
    *,
    filename="asset-initialization.xlsx",
    content_type=XLSX_MIME,
    mutate=None,
):
    content = build_template_workbook("asset_initialization", company=company)
    workbook = load_workbook(io.BytesIO(content))
    definition = get_template_definition("asset_initialization", company=company)
    sheet = workbook[definition.sheet_name]
    for values in rows:
        sheet.append([values.get(header, "") for header in definition.headers])
    if mutate is not None:
        mutate(workbook, sheet, definition)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return SimpleUploadedFile(
        filename,
        output.getvalue(),
        content_type=content_type,
    )


def finance_configuration(company, actor, *, method="straight_line", key="S5-POLICY"):
    fixed = FixedAssetCategory.objects.create(
        company=company,
        code="S5-FA",
        normalized_code="s5-fa",
        name="Sprint 5 固定资产类别",
        useful_life_months_default=60,
    )
    policy = DepreciationPolicy.objects.create(
        company=company,
        policy_key=key,
        version=1,
        name="Sprint 5 默认政策",
        method=method,
        posting_period="monthly",
        start_rule="specified_date",
        stop_rule="event_date",
        default_useful_life_months=60,
        default_salvage_mode="rate",
        default_salvage_rate=Decimal("0.05"),
        default_salvage_amount=None,
        annual_posting_month=None,
        work_unit="台时" if method == "units_of_production" else "",
        status="active",
        is_default=True,
        effective_from=date(2024, 1, 1),
        created_by=actor,
    )
    return fixed, policy
