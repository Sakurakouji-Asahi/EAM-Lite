"""Versioned, protected XLSX imports for Sprint 1 master data.

The policy is deliberately conservative: imports only create records.  They
never update by code or name, and any existing unique key is a row error.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from defusedxml import ElementTree as DefusedElementTree

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from django.utils.text import get_valid_filename
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.normalization import clean_display_identifier, normalize_identifier
from apps.masterdata.permissions import current_company, require_manage_masterdata
from apps.masterdata.services import (
    create_department,
    create_employee,
    get_system_setting,
)
from apps.imports.tempfiles import hold_temp_file_active


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ARCHIVE_MEMBERS = 512
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 10 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_WORKSHEET_ROWS = 10_001
MAX_WORKSHEET_COLUMNS = 32
MAX_WORKSHEET_CELLS = MAX_WORKSHEET_ROWS * MAX_WORKSHEET_COLUMNS
MAX_IMPORT_ROWS = 10_000
CELL_REFERENCE_PATTERN = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)$")
FORBIDDEN_ARCHIVE_PARTS = (
    "vbaproject.bin",
    "xl/externallinks/",
    "xl/embeddings/",
    "xl/activex/",
)
FORBIDDEN_RELATIONSHIP_TYPES = (
    "/externallink",
    "/oleobject",
    "/controlproperties",
    "/activex",
    "/relationships/package",
    "/attachedtemplate",
)


def _lock_import_namespace(company_id):
    """Serialize duplicate/idempotency decisions per company on PostgreSQL."""
    from django.db import connection

    if connection.vendor == "postgresql":
        key = int(company_id)
        if not -(2**31) <= key < 2**31:
            raise ValidationError("公司标识超出导入锁支持范围。")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [0x45414D49, key],
            )


@dataclass(frozen=True)
class Column:
    name: str
    key: str
    required: bool = False


@dataclass(frozen=True)
class TemplateDefinition:
    import_type: str
    label: str
    version: str
    sheet_name: str
    columns: tuple[Column, ...]

    @property
    def headers(self):
        return tuple(column.name for column in self.columns)


TEMPLATE_REGISTRY = {
    "department": TemplateDefinition(
        import_type="department",
        label="部门",
        version="department-v1",
        sheet_name="部门导入",
        columns=(
            Column("部门编码", "code", True),
            Column("部门名称", "name", True),
            Column("上级部门编码", "parent_code"),
            Column("经理工号", "manager_employee_no"),
            Column("是否启用", "is_active"),
        ),
    ),
    "employee": TemplateDefinition(
        import_type="employee",
        label="人员",
        version="employee-v1",
        sheet_name="人员导入",
        columns=(
            Column("员工编号", "employee_no", True),
            Column("姓名", "name", True),
            Column("部门编码", "department_code", True),
            Column("任职状态", "employment_status", True),
            Column("入职日期", "hire_date"),
            Column("离职日期", "termination_date"),
            Column("手机号码", "mobile"),
            Column("备注", "remark"),
            Column("是否启用", "is_active"),
        ),
    ),
}


def get_template_definition(import_type: str) -> TemplateDefinition:
    try:
        return TEMPLATE_REGISTRY[import_type]
    except KeyError as exc:
        raise ValidationError("不支持的导入类型。") from exc


def _require_import_permission(actor, import_type):
    resource = {"department": "department", "employee": "employee"}.get(import_type)
    if resource is None:
        raise ValidationError("不支持的导入类型。")
    require_manage_masterdata(actor, resource)


def _require_current_import_company(company):
    current = current_company(include_inactive=True)
    if (
        company is None
        or current is None
        or getattr(company, "pk", None) != current.pk
    ):
        raise PermissionDenied("导入目标不属于当前公司。")


def build_template_workbook(import_type: str) -> bytes:
    definition = get_template_definition(import_type)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = definition.sheet_name
    sheet.append(definition.headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{sheet.cell(1, len(definition.columns)).coordinate}"
    for index, column in enumerate(definition.columns, 1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = max(
            14, len(column.name) * 2 + 4
        )
        sheet.cell(1, index).comment = None
    boolean_validation = DataValidation(type="list", formula1='"是,否"')
    sheet.add_data_validation(boolean_validation)
    boolean_column = definition.headers.index("是否启用") + 1
    boolean_validation.add(
        f"{sheet.cell(2, boolean_column).coordinate}:"
        f"{sheet.cell(1001, boolean_column).coordinate}"
    )
    if import_type == "employee":
        status_validation = DataValidation(
            type="list", formula1='"active,leaving,resigned"'
        )
        sheet.add_data_validation(status_validation)
        status_column = definition.headers.index("任职状态") + 1
        status_validation.add(
            f"{sheet.cell(2, status_column).coordinate}:"
            f"{sheet.cell(1001, status_column).coordinate}"
        )
    instructions = workbook.create_sheet("填写说明")
    instructions.append(["项目", "说明"])
    policies = [
        ("模板版本", definition.version),
        ("工作表", definition.sheet_name),
        ("数据策略", "只新增，不按名称或编码覆盖现有记录"),
        ("日期格式", "YYYY-MM-DD"),
        ("布尔值", "是 / 否；留空默认为“是”"),
        ("空行", "忽略完全空白的数据行"),
        ("安全", "未知列、公式、外部链接、宏或嵌入对象会导致拒绝"),
    ]
    if import_type == "department":
        policies.append(
            ("匹配键", "上级部门按当前公司的部门编码匹配；可引用同文件新部门")
        )
        policies.append(("经理", "按当前公司已有员工编号匹配"))
    else:
        policies.append(("匹配键", "部门按当前公司已有部门编码匹配"))
    for row in policies:
        instructions.append(row)
    instructions.column_dimensions["A"].width = 18
    instructions.column_dimensions["B"].width = 90
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _error(field, value, reason):
    return {"field": field, "value": _json_value(value), "reason": reason}


def _text(value):
    if value is None:
        return ""
    return clean_display_identifier(str(value))


def _boolean(value, field, errors, default=True):
    text = _text(value)
    if not text:
        return default
    if text == "是":
        return True
    if text == "否":
        return False
    errors.append(_error(field, value, "只能填写“是”或“否”。"))
    return default


def _date(value, field, errors):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        errors.append(_error(field, value, "日期必须使用 YYYY-MM-DD 格式。"))
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        errors.append(_error(field, value, "日期不存在或格式无效。"))
        return None


def _read_uploaded(uploaded_file, limit):
    if getattr(uploaded_file, "size", 0) > limit:
        raise ValidationError(f"文件超过当前上限 {limit} 字节。")
    chunks = []
    total = 0
    temporary_path = None
    temporary_path_getter = getattr(uploaded_file, "temporary_file_path", None)
    if callable(temporary_path_getter):
        temporary_path = temporary_path_getter()

    from contextlib import nullcontext
    from django.conf import settings

    activity = (
        hold_temp_file_active(temporary_path, settings.IMPORT_TEMP_ROOT)
        if temporary_path
        else nullcontext()
    )
    with activity:
        for chunk in uploaded_file.chunks():
            total += len(chunk)
            if total > limit:
                raise ValidationError(f"文件超过当前上限 {limit} 字节。")
            chunks.append(chunk)
    if total <= 0:
        raise ValidationError("上传文件不能为空。")
    return b"".join(chunks)


def _validate_xlsx_container(data):
    if not data.startswith(b"PK\x03\x04"):
        raise ValidationError("文件内容不是有效的 XLSX ZIP 容器。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValidationError("XLSX 内部文件数量异常。")
            total_uncompressed = 0
            for item in members:
                if item.file_size < 0 or item.compress_size < 0:
                    raise ValidationError("XLSX ZIP 元数据无效。")
                if item.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValidationError("XLSX 单个内部文件解压后过大。")
                if item.file_size and item.compress_size == 0:
                    raise ValidationError("XLSX 内部文件压缩比异常。")
                if (
                    item.compress_size
                    and item.file_size / item.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ValidationError("XLSX 内部文件压缩比异常。")
                total_uncompressed += item.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ValidationError("XLSX 解压后大小超过安全上限。")
            # ZIP metadata is attacker-controlled.  Read every member through
            # a hard bounded loop as a second line of defence, instead of
            # trusting only the declared file_size/compress_size values.
            actual_total = 0
            for item in members:
                actual_member = 0
                with archive.open(item) as member_stream:
                    while chunk := member_stream.read(64 * 1024):
                        actual_member += len(chunk)
                        actual_total += len(chunk)
                        if actual_member > MAX_ARCHIVE_MEMBER_BYTES:
                            raise ValidationError(
                                "XLSX 单个内部文件解压后过大。"
                            )
                        if actual_total > MAX_UNCOMPRESSED_BYTES:
                            raise ValidationError(
                                "XLSX 解压后大小超过安全上限。"
                            )
            names = {item.filename.lower() for item in members}
            if "[content_types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValidationError("文件缺少 XLSX 必需结构。")
            content_types = archive.read("[Content_Types].xml").lower()
            if b"macroenabled" in content_types or b"vbaproject" in content_types:
                raise ValidationError("文件声明了宏启用内容，已拒绝。")
            forbidden = sorted(
                name
                for name in names
                if any(part in name for part in FORBIDDEN_ARCHIVE_PARTS)
            )
            if forbidden:
                raise ValidationError("文件包含宏、外部链接或嵌入对象，已拒绝。")
            for item in members:
                if not item.filename.lower().endswith(".rels"):
                    continue
                try:
                    with archive.open(item) as relationship_stream:
                        relationships = DefusedElementTree.parse(
                            relationship_stream
                        ).getroot()
                except Exception as exc:
                    raise ValidationError("XLSX 关系 XML 无法安全解析。") from exc
                for relationship in relationships.iter():
                    if relationship.tag.rsplit("}", 1)[-1].casefold() != "relationship":
                        continue
                    attributes = [
                        (name.rsplit("}", 1)[-1].casefold(), str(value).strip())
                        for name, value in relationship.attrib.items()
                    ]
                    has_external_target = any(
                        name == "targetmode" and value.casefold() == "external"
                        for name, value in attributes
                    )
                    relationship_types = [
                        value.casefold() for name, value in attributes if name == "type"
                    ]
                    has_forbidden_type = any(
                        token in relationship_type
                        for relationship_type in relationship_types
                        for token in FORBIDDEN_RELATIONSHIP_TYPES
                    )
                    if has_external_target or has_forbidden_type:
                        raise ValidationError("文件包含外部关系或嵌入内容，已拒绝。")
            for item in members:
                name = item.filename.lower()
                if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                    continue
                with archive.open(item) as worksheet_stream:
                    worksheet_errors = _validate_worksheet_xml(
                        worksheet_stream, item.filename
                    )
                if worksheet_errors:
                    return worksheet_errors
            return []
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ValidationError("文件不是完整的 XLSX。") from exc


def _column_number(letters):
    number = 0
    for character in letters.upper():
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _validate_worksheet_xml(xml_source, worksheet_name):
    """Check actual cells rather than trusting a worksheet's dimension hint."""
    cell_count = 0
    if isinstance(xml_source, (bytes, bytearray)):
        xml_source = io.BytesIO(xml_source)
    try:
        for _event, element in DefusedElementTree.iterparse(
            xml_source, events=("end",)
        ):
            local_name = element.tag.rsplit("}", 1)[-1].lower()
            if local_name == "f":
                return [
                    _error(
                        "公式",
                        worksheet_name,
                        "所有工作表均禁止公式单元格。",
                    )
                ]
            if local_name == "c":
                cell_count += 1
                if cell_count > MAX_WORKSHEET_CELLS:
                    return [
                        _error(
                            "工作表",
                            worksheet_name,
                            "实际单元格数量超过安全上限："
                            f"最多 {MAX_WORKSHEET_CELLS} 个。",
                        )
                    ]
                reference = str(element.attrib.get("r", ""))
                match = CELL_REFERENCE_PATTERN.fullmatch(reference)
                if not match:
                    raise ValidationError(
                        f"工作表 {worksheet_name} 包含无效单元格坐标。"
                    )
                column_number = _column_number(match.group(1))
                row_number = int(match.group(2))
                if (
                    row_number > MAX_WORKSHEET_ROWS
                    or column_number > MAX_WORKSHEET_COLUMNS
                ):
                    return [
                        _error(
                            "工作表",
                            f"{worksheet_name}!{reference}",
                            "实际单元格超过安全上限："
                            f"最多 {MAX_WORKSHEET_ROWS} 行、{MAX_WORKSHEET_COLUMNS} 列。",
                        )
                    ]
            element.clear()
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("XLSX 工作表 XML 无法安全解析。") from exc
    return []


def _cell_is_formula(cell):
    return cell.data_type == "f" or (
        isinstance(cell.value, str) and cell.value.startswith("=")
    )


def _worksheet_limits_error(workbook):
    for worksheet in workbook.worksheets:
        if (
            worksheet.max_row > MAX_WORKSHEET_ROWS
            or worksheet.max_column > MAX_WORKSHEET_COLUMNS
        ):
            return _error(
                "工作表",
                worksheet.title,
                (
                    f"工作表维度超过安全上限：最多 {MAX_WORKSHEET_ROWS} 行、"
                    f"{MAX_WORKSHEET_COLUMNS} 列。"
                ),
            )
        for row in worksheet.iter_rows(
            min_row=1,
            max_row=worksheet.max_row,
            min_col=1,
            max_col=worksheet.max_column,
        ):
            for cell in row:
                if _cell_is_formula(cell):
                    return _error(
                        "公式",
                        f"{worksheet.title}!{cell.coordinate}",
                        "所有工作表均禁止公式单元格。",
                    )
    return None


def _load_rows(data, definition):
    try:
        workbook = load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
    except Exception as exc:
        raise ValidationError("无法解析 XLSX，请重新下载标准模板。") from exc
    try:
        if definition.sheet_name not in workbook.sheetnames:
            return [], [
                _error(
                    "工作表",
                    ", ".join(workbook.sheetnames),
                    f"必须存在名为“{definition.sheet_name}”的工作表。",
                )
            ]
        expected_sheets = {definition.sheet_name, "填写说明"}
        missing_sheets = [name for name in expected_sheets if name not in workbook.sheetnames]
        if missing_sheets:
            return [], [
                _error(
                    "工作表",
                    ", ".join(workbook.sheetnames),
                    f"缺少标准工作表：{', '.join(sorted(missing_sheets))}。",
                )
            ]
        unknown_sheets = [name for name in workbook.sheetnames if name not in expected_sheets]
        if unknown_sheets:
            return [], [
                _error(
                    "工作表",
                    ", ".join(unknown_sheets),
                    "发现标准模板之外的工作表，已拒绝。",
                )
            ]
        safety_error = _worksheet_limits_error(workbook)
        if safety_error:
            return [], [safety_error]
        instructions = workbook["填写说明"]
        if _text(instructions["A2"].value) != "模板版本" or _text(
            instructions["B2"].value
        ) != definition.version:
            return [], [
                _error(
                    "模板版本",
                    instructions["B2"].value,
                    f"必须使用模板版本 {definition.version}。",
                )
            ]
        sheet = workbook[definition.sheet_name]
        actual_headers = tuple(_text(cell.value) for cell in sheet[1])
        if actual_headers != definition.headers:
            unknown = [value for value in actual_headers if value not in definition.headers]
            missing = [value for value in definition.headers if value not in actual_headers]
            reason = "列名及顺序必须与标准模板完全一致。"
            if unknown:
                reason += f" 未知列：{', '.join(unknown)}。"
            if missing:
                reason += f" 缺少列：{', '.join(missing)}。"
            return [], [_error("表头", list(actual_headers), reason)]
        rows = []
        for row_number, cells in enumerate(
            sheet.iter_rows(min_row=2, max_col=len(definition.columns)), start=2
        ):
            if all(cell.value in (None, "") for cell in cells):
                continue
            formulas = [
                definition.columns[index].name
                for index, cell in enumerate(cells)
                if cell.data_type == "f"
                or (isinstance(cell.value, str) and cell.value.startswith("="))
            ]
            raw = {
                column.name: _json_value(cells[index].value)
                for index, column in enumerate(definition.columns)
            }
            rows.append((row_number, cells, raw, formulas))
            if len(rows) > MAX_IMPORT_ROWS:
                return [], [
                    _error(
                        "数据",
                        len(rows),
                        f"单批最多允许 {MAX_IMPORT_ROWS} 行有效数据。",
                    )
                ]
        return rows, []
    finally:
        workbook.close()


def _normalize_department_rows(company, loaded_rows, definition):
    from apps.masterdata.models import Department, Employee

    existing_codes = set(
        Department.objects.filter(company=company).values_list("normalized_code", flat=True)
    )
    employees = {
        employee.normalized_employee_no: employee
        for employee in Employee.objects.filter(company=company).select_related("department")
    }
    prepared = []
    seen = {}
    for row_number, cells, raw, formulas in loaded_rows:
        values = {column.key: cells[index].value for index, column in enumerate(definition.columns)}
        errors = []
        if formulas:
            errors.extend(_error(field, raw.get(field), "禁止公式单元格。") for field in formulas)
        code = _text(values["code"])
        name = _text(values["name"])
        parent_code = _text(values["parent_code"])
        manager_no = _text(values["manager_employee_no"])
        normalized_code = normalize_identifier(code)
        normalized_parent = normalize_identifier(parent_code)
        normalized_manager = normalize_identifier(manager_no)
        if not code:
            errors.append(_error("部门编码", values["code"], "必填。"))
        if not name:
            errors.append(_error("部门名称", values["name"], "必填。"))
        if normalized_code in existing_codes:
            errors.append(_error("部门编码", code, "当前公司已存在该编码；本导入只新增。"))
        if normalized_code and normalized_code in seen:
            errors.append(_error("部门编码", code, f"与文件第 {seen[normalized_code]} 行重复。"))
        elif normalized_code:
            seen[normalized_code] = row_number
        if normalized_parent and normalized_parent == normalized_code:
            errors.append(_error("上级部门编码", parent_code, "不能将部门自身设为上级。"))
        manager = employees.get(normalized_manager) if normalized_manager else None
        if normalized_manager and manager is None:
            errors.append(_error("经理工号", manager_no, "当前公司不存在该员工编号。"))
        elif manager and (
            manager.employment_status != "active"
            or not manager.is_active
            or not manager.department.is_active
        ):
            errors.append(_error("经理工号", manager_no, "经理必须在职、启用且属于启用部门。"))
        normalized = {
            "code": code,
            "normalized_code": normalized_code,
            "name": name,
            "parent_code": parent_code,
            "normalized_parent_code": normalized_parent,
            "manager_employee_no": manager_no,
            "normalized_manager_employee_no": normalized_manager,
            "is_active": _boolean(values["is_active"], "是否启用", errors),
        }
        prepared.append({"row_number": row_number, "raw": raw, "normalized": normalized, "errors": errors})

    available = existing_codes | set(seen)
    parent_by_code = {item["normalized"]["normalized_code"]: item for item in prepared}
    for item in prepared:
        parent = item["normalized"]["normalized_parent_code"]
        if parent and parent not in available:
            item["errors"].append(_error("上级部门编码", item["normalized"]["parent_code"], "当前公司及本文件均不存在该部门编码。"))

    for item in prepared:
        start = item["normalized"]["normalized_code"]
        current = item["normalized"]["normalized_parent_code"]
        visited = {start}
        while current in parent_by_code:
            if current in visited:
                item["errors"].append(_error("上级部门编码", item["normalized"]["parent_code"], "文件内部门父级关系形成循环。"))
                break
            visited.add(current)
            current = parent_by_code[current]["normalized"]["normalized_parent_code"]
    return prepared


def _normalize_employee_rows(company, loaded_rows, definition):
    from apps.masterdata.models import Department, Employee

    existing_numbers = set(
        Employee.objects.filter(company=company).values_list("normalized_employee_no", flat=True)
    )
    departments = {
        department.normalized_code: department
        for department in Department.objects.filter(company=company)
    }
    prepared = []
    seen = {}
    for row_number, cells, raw, formulas in loaded_rows:
        values = {column.key: cells[index].value for index, column in enumerate(definition.columns)}
        errors = []
        if formulas:
            errors.extend(_error(field, raw.get(field), "禁止公式单元格。") for field in formulas)
        employee_no = _text(values["employee_no"])
        name = _text(values["name"])
        department_code = _text(values["department_code"])
        status = _text(values["employment_status"])
        normalized_no = normalize_identifier(employee_no)
        normalized_department = normalize_identifier(department_code)
        if not employee_no:
            errors.append(_error("员工编号", values["employee_no"], "必填。"))
        if not name:
            errors.append(_error("姓名", values["name"], "必填。"))
        if not department_code:
            errors.append(_error("部门编码", values["department_code"], "必填。"))
        department = departments.get(normalized_department)
        if department is None:
            errors.append(_error("部门编码", department_code, "当前公司不存在该部门编码。"))
        elif not department.is_active:
            errors.append(_error("部门编码", department_code, "人员不能新绑定到已停用部门。"))
        if status not in {"active", "leaving", "resigned"}:
            errors.append(_error("任职状态", status, "只能为 active、leaving 或 resigned。"))
        if normalized_no in existing_numbers:
            errors.append(_error("员工编号", employee_no, "当前公司已存在该编号；本导入只新增。"))
        if normalized_no and normalized_no in seen:
            errors.append(_error("员工编号", employee_no, f"与文件第 {seen[normalized_no]} 行重复。"))
        elif normalized_no:
            seen[normalized_no] = row_number
        hire_date = _date(values["hire_date"], "入职日期", errors)
        termination_date = _date(values["termination_date"], "离职日期", errors)
        is_active = _boolean(values["is_active"], "是否启用", errors)
        if status == "resigned" and termination_date is None:
            errors.append(_error("离职日期", values["termination_date"], "resigned 人员必须填写离职日期。"))
        if status in {"active", "leaving"} and termination_date is not None:
            errors.append(_error("离职日期", values["termination_date"], "active/leaving 人员的离职日期必须为空。"))
        if status in {"leaving", "resigned"} and is_active:
            errors.append(_error("是否启用", values["is_active"], "leaving/resigned 人员必须停用。"))
        if hire_date and termination_date and termination_date < hire_date:
            errors.append(_error("离职日期", values["termination_date"], "离职日期不能早于入职日期。"))
        normalized = {
            "employee_no": employee_no,
            "normalized_employee_no": normalized_no,
            "name": name,
            "department_code": department_code,
            "normalized_department_code": normalized_department,
            "employment_status": status,
            "hire_date": hire_date.isoformat() if hire_date else None,
            "termination_date": termination_date.isoformat() if termination_date else None,
            "mobile": _text(values["mobile"]),
            "remark": _text(values["remark"]),
            "is_active": is_active,
        }
        prepared.append({"row_number": row_number, "raw": raw, "normalized": normalized, "errors": errors})
    return prepared


def _audit_batch(*, batch, actor, action, new_data, old_data=None, request=None):
    write_business_audit_log(
        company=batch.company,
        user=actor,
        action=action,
        object_type="ImportBatch",
        object_id=batch.pk,
        old_data=old_data or {},
        new_data=new_data,
        **request_audit_context(request),
    )


def _validation_messages(exc):
    if hasattr(exc, "message_dict"):
        messages = []
        for field, field_messages in exc.message_dict.items():
            messages.extend(f"{field}: {message}" for message in field_messages)
        return messages
    return list(getattr(exc, "messages", (str(exc),)))


def _preflight_business_rows(*, actor, company, import_type, prepared):
    """Run the same model/service validation without leaving business rows behind."""

    from apps.masterdata.models import Department, Employee

    if any(item["errors"] for item in prepared):
        return prepared
    with transaction.atomic():
        if import_type == "department":
            existing = {
                value.normalized_code: value
                for value in Department.objects.filter(company=company)
            }
            employees = {
                value.normalized_employee_no: value
                for value in Employee.objects.filter(company=company).select_related(
                    "department"
                )
            }
            created = {}
            pending = list(prepared)
            while pending:
                progressed = False
                for item in pending[:]:
                    normalized = item["normalized"]
                    parent_code = normalized["normalized_parent_code"]
                    parent = existing.get(parent_code) or created.get(parent_code)
                    if parent_code and parent is None:
                        continue
                    try:
                        department = create_department(
                            actor=actor,
                            company=company,
                            data={
                                "code": normalized["code"],
                                "name": normalized["name"],
                                "parent": parent,
                                "manager_employee": employees.get(
                                    normalized["normalized_manager_employee_no"]
                                ),
                                "is_active": normalized["is_active"],
                            },
                        )
                    except ValidationError as exc:
                        item["errors"].extend(
                            _error("数据", None, message)
                            for message in _validation_messages(exc)
                        )
                        department = None
                    if department is not None:
                        created[normalized["normalized_code"]] = department
                    pending.remove(item)
                    progressed = True
                if not progressed:
                    break
        else:
            departments = {
                value.normalized_code: value
                for value in Department.objects.filter(company=company, is_active=True)
            }
            for item in prepared:
                normalized = item["normalized"]
                try:
                    create_employee(
                        actor=actor,
                        company=company,
                        data={
                            "employee_no": normalized["employee_no"],
                            "name": normalized["name"],
                            "department": departments.get(
                                normalized["normalized_department_code"]
                            ),
                            "employment_status": normalized["employment_status"],
                            "hire_date": date.fromisoformat(normalized["hire_date"])
                            if normalized["hire_date"]
                            else None,
                            "termination_date": date.fromisoformat(
                                normalized["termination_date"]
                            )
                            if normalized["termination_date"]
                            else None,
                            "mobile": normalized["mobile"],
                            "remark": normalized["remark"],
                            "is_active": normalized["is_active"],
                        },
                    )
                except ValidationError as exc:
                    item["errors"].extend(
                        _error("数据", None, message)
                        for message in _validation_messages(exc)
                    )
        transaction.set_rollback(True)
    return prepared


def upload_and_validate_import(
    *, actor, company, import_type, uploaded_file, idempotency_key, request=None
):
    from apps.masterdata.models import Attachment, ImportBatch, ImportRow

    _require_import_permission(actor, import_type)
    _require_current_import_company(company)
    definition = get_template_definition(import_type)
    allowed = set(get_system_setting(company=company, key="attachment_allowed_extensions"))
    if "xlsx" not in allowed:
        raise ValidationError("当前公司附件白名单未允许 xlsx。")
    if Path(uploaded_file.name).suffix.lower() != ".xlsx":
        raise ValidationError("只允许上传无宏的 .xlsx 文件。")
    limit = get_system_setting(company=company, key="attachment_max_size_bytes")
    data = _read_uploaded(uploaded_file, limit)
    container_errors = _validate_xlsx_container(data)
    digest = hashlib.sha256(data).hexdigest()
    request_hash = hashlib.sha256(
        f"{import_type}:{definition.version}:{digest}".encode()
    ).hexdigest()

    with transaction.atomic():
        _lock_import_namespace(company.pk)
        existing_key = (
            ImportBatch.objects.select_for_update()
            .filter(company=company, idempotency_key=idempotency_key)
            .first()
        )
        if existing_key:
            if existing_key.request_hash != request_hash:
                raise ValidationError("同一幂等标识已用于不同文件。")
            return existing_key
        duplicate = (
            ImportBatch.objects.select_for_update()
            .filter(
                company=company,
                import_type=import_type,
                template_version=definition.version,
                file_sha256=digest,
            )
            .order_by("-uploaded_at")
            .first()
        )
        if duplicate:
            raise ValidationError(
                f"该文件已上传为批次 {duplicate.pk}，请打开原批次，不要重复上传。"
            )

    if container_errors:
        loaded_rows, workbook_errors = [], container_errors
    else:
        loaded_rows, workbook_errors = _load_rows(data, definition)
    if workbook_errors:
        prepared = [{"row_number": 1, "raw": {}, "normalized": {}, "errors": workbook_errors}]
    elif import_type == "department":
        prepared = _normalize_department_rows(company, loaded_rows, definition)
    else:
        prepared = _normalize_employee_rows(company, loaded_rows, definition)
    prepared = _preflight_business_rows(
        actor=actor,
        company=company,
        import_type=import_type,
        prepared=prepared,
    )
    if not prepared:
        prepared = [{"row_number": 2, "raw": {}, "normalized": {}, "errors": [_error("数据", None, "模板中没有可导入的数据行。")]}]

    # The object is written first under a private, random key.  It has no
    # public URL and is not downloadable until the database transaction below
    # atomically publishes Attachment.is_available=True.  A process failure
    # before the database commit therefore leaves, at worst, an unreferenced
    # private object for the idempotent cleanup command.
    storage_key = f"private/imports/{company.pk}/{uuid.uuid4().hex}.xlsx"
    saved_key = default_storage.save(storage_key, ContentFile(data))
    batch = None
    created_batch = False
    try:
        with transaction.atomic():
            _lock_import_namespace(company.pk)
            existing_key = (
                ImportBatch.objects.select_for_update()
                .filter(company=company, idempotency_key=idempotency_key)
                .first()
            )
            if existing_key:
                if existing_key.request_hash != request_hash:
                    raise ValidationError("同一幂等标识已用于不同文件。")
                batch = existing_key
            if batch is not None:
                # Do not create a second evidence attachment for an idempotent
                # retry.  The just-written private object is removed after the
                # transaction has released its locks.
                pass
            else:
                duplicate = (
                    ImportBatch.objects.select_for_update()
                    .filter(
                        company=company,
                        import_type=import_type,
                        template_version=definition.version,
                        file_sha256=digest,
                    )
                    .order_by("-uploaded_at")
                    .first()
                )
                if duplicate:
                    raise ValidationError(
                        f"该文件已上传为批次 {duplicate.pk}，请打开原批次，不要重复上传。"
                    )
                attachment = Attachment(
                    company=company,
                    storage_key=saved_key,
                    original_filename=str(uploaded_file.name)[:255],
                    safe_filename=(get_valid_filename(Path(uploaded_file.name).name) or "import.xlsx")[:255],
                    file_size=len(data),
                    mime_type=XLSX_MIME,
                    sha256=digest,
                    uploaded_by=actor,
                    malware_scan_status="pending",
                    is_available=False,
                )
                attachment.full_clean()
                attachment.save()
                errors_count = sum(bool(item["errors"]) for item in prepared)
                batch = ImportBatch(
                    company=company,
                    import_type=import_type,
                    template_version=definition.version,
                    file_attachment=attachment,
                    file_sha256=digest,
                    status="invalid" if errors_count else "validated",
                    total_rows=len(prepared),
                    valid_rows=len(prepared) - errors_count,
                    error_rows=errors_count,
                    warning_rows=0,
                    request_hash=request_hash,
                    idempotency_key=idempotency_key,
                    uploaded_by=actor,
                    validated_at=timezone.now(),
                )
                batch.full_clean()
                batch.save()
                created_batch = True
                rows = [
                    ImportRow(
                        batch=batch,
                        row_number=item["row_number"],
                        raw_data_json=item["raw"],
                        normalized_data_json=item["normalized"],
                        validation_status="invalid" if item["errors"] else "valid",
                        errors_json=item["errors"],
                        warnings_json=[],
                    )
                    for item in prepared
                ]
                for row in rows:
                    row.full_clean()
                ImportRow.objects.bulk_create(rows)
                _audit_batch(
                    batch=batch,
                    actor=actor,
                    action="import_validate",
                    new_data={
                        "import_type": import_type,
                        "template_version": definition.version,
                        "file_sha256": digest,
                        "status": batch.status,
                        "total_rows": batch.total_rows,
                        "valid_rows": batch.valid_rows,
                        "error_rows": batch.error_rows,
                    },
                    request=request,
                )
                # This update is the publication point.  Other transactions
                # can only observe it after the validation rows and audit log
                # have committed successfully.
                attachment.malware_scan_status = "policy_limited"
                attachment.is_available = True
                attachment.full_clean()
                attachment.save(
                    update_fields=["malware_scan_status", "is_available"]
                )
        if not created_batch:
            default_storage.delete(saved_key)
        return batch
    except Exception:
        if saved_key:
            try:
                default_storage.delete(saved_key)
            except Exception:
                # Never mask the database/validation failure.  The private
                # unreferenced object is handled by cleanup_import_staging.
                pass
        raise


@transaction.atomic
def confirm_import_batch(*, actor, batch, request=None):
    from apps.masterdata.models import Department, Employee, ImportBatch

    # Share the upload/cleanup namespace lock.  Cleanup can therefore prove
    # that no confirmation/idempotency request is processing the company while
    # it re-checks and removes an expired staging batch.
    _lock_import_namespace(batch.company_id)
    batch = (
        ImportBatch.objects.select_for_update()
        .select_related("company", "file_attachment")
        .get(pk=batch.pk)
    )
    _require_current_import_company(batch.company)
    _require_import_permission(actor, batch.import_type)
    if batch.status == "confirmed":
        return batch
    if batch.status != "validated" or batch.error_rows != 0:
        raise ValidationError("只能确认无错误的已验证批次。")
    if batch.file_sha256 != batch.file_attachment.sha256:
        raise ValidationError("原文件摘要与批次不一致，已阻止确认。")
    if not batch.file_attachment.is_available:
        raise ValidationError("原文件已不可用，已阻止确认。")
    if batch.file_attachment.malware_scan_status not in {
        "policy_limited",
        "clean",
    }:
        raise ValidationError("原文件尚未通过安全策略校验，已阻止确认。")
    rows = list(batch.rows.select_for_update().order_by("row_number"))
    if not rows or any(row.validation_status != "valid" for row in rows):
        raise ValidationError("批次行状态与已验证状态不一致。")

    created = {}
    if batch.import_type == "department":
        existing = {
            value.normalized_code: value
            for value in Department.objects.select_for_update().filter(company=batch.company)
        }
        if any(row.normalized_data_json["normalized_code"] in existing for row in rows):
            raise ValidationError("确认前检测到部门编码已存在，请重新上传验证。")
        employees = {
            value.normalized_employee_no: value
            for value in Employee.objects.filter(company=batch.company).select_related("department")
        }
        pending = list(rows)
        while pending:
            progressed = False
            for row in pending[:]:
                item = row.normalized_data_json
                parent_code = item["normalized_parent_code"]
                parent = existing.get(parent_code) or created.get(parent_code)
                if parent_code and parent is None:
                    continue
                manager = employees.get(item["normalized_manager_employee_no"])
                department = create_department(
                    actor=actor,
                    company=batch.company,
                    data={
                        "code": item["code"],
                        "name": item["name"],
                        "parent": parent,
                        "manager_employee": manager,
                        "is_active": item["is_active"],
                    },
                    request=request,
                )
                created[item["normalized_code"]] = department
                row.created_object_type = "Department"
                row.created_object_id = str(department.pk)
                pending.remove(row)
                progressed = True
            if not progressed:
                raise ValidationError("无法解析部门树的父级顺序，已整批回滚。")
    else:
        existing_numbers = set(
            Employee.objects.select_for_update().filter(company=batch.company).values_list(
                "normalized_employee_no", flat=True
            )
        )
        if any(row.normalized_data_json["normalized_employee_no"] in existing_numbers for row in rows):
            raise ValidationError("确认前检测到员工编号已存在，请重新上传验证。")
        departments = {
            value.normalized_code: value
            for value in Department.objects.filter(company=batch.company, is_active=True)
        }
        for row in rows:
            item = row.normalized_data_json
            department = departments.get(item["normalized_department_code"])
            if department is None:
                raise ValidationError(f"第 {row.row_number} 行部门已不可用，已整批回滚。")
            employee = create_employee(
                actor=actor,
                company=batch.company,
                data={
                    "employee_no": item["employee_no"],
                    "name": item["name"],
                    "department": department,
                    "employment_status": item["employment_status"],
                    "hire_date": date.fromisoformat(item["hire_date"]) if item["hire_date"] else None,
                    "termination_date": date.fromisoformat(item["termination_date"]) if item["termination_date"] else None,
                    "mobile": item["mobile"],
                    "remark": item["remark"],
                    "is_active": item["is_active"],
                },
                request=request,
            )
            row.created_object_type = "Employee"
            row.created_object_id = str(employee.pk)

    batch.status = "confirmed"
    batch.confirmed_by = actor
    batch.confirmed_at = timezone.now()
    batch.full_clean()
    batch.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    for row in rows:
        row.validation_status = "created"
        row.full_clean()
        row.save(
            update_fields=[
                "validation_status",
                "created_object_type",
                "created_object_id",
            ]
        )
    _audit_batch(
        batch=batch,
        actor=actor,
        action="import_confirm",
        old_data={"status": "validated"},
        new_data={
            "status": "confirmed",
            "import_type": batch.import_type,
            "created_count": len(rows),
            "file_sha256": batch.file_sha256,
            "created_objects": [
                {
                    "row_number": row.row_number,
                    "object_type": row.created_object_type,
                    "object_id": row.created_object_id,
                }
                for row in rows
            ],
        },
        request=request,
    )
    return batch
