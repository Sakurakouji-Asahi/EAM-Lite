"""Portable count sheets with signed row identity and atomic, scoped import."""
import csv
import io
from decimal import Decimal, InvalidOperation

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from .count_workspace import BulkCountLineForm, visible_count_lines, record_count_page
from .permissions import can_record_supply_count, can_view_supply_cost

HEADERS = ('行校验码', '物品编号', '物品名称', '单位', '应盘数量', '实盘数量', '差异原因/备注', '零库存盘盈单位成本', '0 成本原因', '责任部门', '责任人', '仓库/保管来源')


def row_responsibility(task, line):
    return (line.department_snapshot, line.employee_snapshot,
        str(line.custody_id) if line.custody_id else task.warehouse.code)


class CountTransferForm(forms.Form):
    file = forms.FileField(label='盘点结果 XLSX', required=False)
    pasted = forms.CharField(label='或粘贴盘点表（含表头）', required=False, strip=False,
        widget=forms.Textarea(attrs={'rows':8, 'class':'form-control'}))

    def clean(self):
        data = super().clean()
        if bool(data.get('file')) == bool(data.get('pasted')):
            raise ValidationError('请选择一个 XLSX 文件，或粘贴一份表格。')
        if data.get('file') and data['file'].size > 5 * 1024 * 1024:
            raise ValidationError('文件不得超过 5 MB。')
        if len(data.get('pasted', '')) > 5 * 1024 * 1024:
            raise ValidationError('粘贴内容过大，每次最多 10000 行。')
        return data


def count_workbook(actor, task):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '盘点表'
    sheet.append(HEADERS)
    sheet.freeze_panes = 'F2'
    sheet.print_title_rows = '1:1'
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for column, width in zip('ABCDEFGHIJKL', (20,22,30,12,18,18,35,24,25,20,18,38)):
        sheet.column_dimensions[column].width = width
    rows = visible_count_lines(actor, task)
    if rows.count() > 10000:
        raise ValidationError('任务超过 10000 行，请使用分页录入。')
    for line in rows:
        if not can_record_supply_count(actor, line):
            continue
        token = signing.dumps({'actor':actor.pk, 'task':str(task.pk), 'line':str(line.pk),
            'version':line.counted_at.isoformat() if line.counted_at else ''}, salt='count-sheet', compress=True)
        values = (token, line.item_code_snapshot, line.item_name_snapshot, line.item.unit,
            str(line.expected_quantity), str(line.counted_quantity) if line.counted_quantity is not None else '',
            line.remark, str(line.adjustment_unit_cost) if can_view_supply_cost(actor) and task.count_domain == 'warehouse_stock' and line.expected_quantity == 0 and line.adjustment_unit_cost is not None else '',
            line.zero_cost_reason if can_view_supply_cost(actor) and task.count_domain == 'warehouse_stock' and line.expected_quantity == 0 else '',
            *row_responsibility(task,line))
        sheet.append(values)
        # Preserve exact decimals/identifiers and prevent formula injection.
        for cell in sheet[sheet.max_row]:
            cell.data_type = 's'
            cell.number_format = '@'
    sheet.auto_filter.ref = f'A1:L{sheet.max_row}'
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def read_count_rows(data):
    if data.get('pasted'):
        rows = list(csv.reader(io.StringIO(data['pasted']), delimiter='\t'))
    else:
        from apps.imports.services import _validate_xlsx_container, _worksheet_limits_error, _xlsx_decimal_literals
        content = data['file'].read(5 * 1024 * 1024 + 1)
        if len(content) > 5 * 1024 * 1024:
            raise ValidationError('文件不得超过 5 MB。')
        _validate_xlsx_container(content)
        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        except Exception as exc:
            raise ValidationError('无法读取盘点表，请使用下载的 XLSX 模板。') from exc
        try:
            if workbook.sheetnames != ['盘点表'] or _worksheet_limits_error(workbook):
                raise ValidationError('请保留单个“盘点表”工作表，不得使用公式或超出 10000 行。')
            sheet = workbook['盘点表']
            literals = _xlsx_decimal_literals(content, sheet._worksheet_path)
            rows = [[literals.get(cell.coordinate, cell.value) for cell in row] for row in sheet]
        finally:
            workbook.close()
    rows = [row for row in rows if any(value not in (None, '') for value in row)]
    if not rows or tuple(rows[0]) != HEADERS:
        raise ValidationError('表头与盘点模板不一致，请完整保留全部列。')
    if len(rows) > 10001:
        raise ValidationError('每次最多导入 10000 行。')
    return rows[1:]


@transaction.atomic
def import_count_rows(*, actor, task, rows, request=None):
    entries, seen, errors = [], set(), []
    visible = {str(line.pk):line for line in visible_count_lines(actor, task)}
    for number, row in enumerate(rows, 2):
        try:
            if len(row) != len(HEADERS):
                raise ValidationError('列数与模板不一致。')
            try:
                identity = signing.loads(str(row[0]), salt='count-sheet', max_age=30*86400)
                if identity['actor'] != actor.pk or identity['task'] != str(task.pk):
                    raise ValueError
                line = visible[identity['line']]
            except (signing.BadSignature, ValueError, TypeError, KeyError):
                raise ValidationError('行校验码失效、属于其他任务或不在当前权限范围，请重新下载。')
            if str(line.pk) in seen:
                raise ValidationError('同一盘点行重复出现。')
            seen.add(str(line.pk))
            if tuple(str(value or '') for value in row[1:4]) != (line.item_code_snapshot, line.item_name_snapshot, line.item.unit):
                raise ValidationError('物品编号、名称或单位被修改，请保留原样。')
            try:
                if Decimal(str(row[4])) != line.expected_quantity:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise ValidationError('应盘数量被修改，请保留下载时的数量。')
            if tuple(str(value or '') for value in row[9:]) != row_responsibility(task,line):
                raise ValidationError('责任部门、责任人或来源被修改，请保留原样。')
            values = dict(zip(('counted_quantity','remark','adjustment_unit_cost','zero_cost_reason'), row[5:9]))
            values = {key:'' if value is None else value for key,value in values.items()}
            values['expected_counted_at'] = identity['version']
            form = BulkCountLineForm(values, actor=actor, line=line)
            if any(values.get(key) != '' for key in ('adjustment_unit_cost','zero_cost_reason') if key not in form.fields):
                raise ValidationError('该行不允许填写盘盈成本。')
            if not form.is_valid():
                raise ValidationError([message for messages in form.errors.values() for message in messages])
            if form.cleaned_data.get('counted_quantity') is not None:
                entries.append({**form.cleaned_data, 'line_id':line.pk})
        except ValidationError as exc:
            errors.append(f'第 {number} 行：'+'；'.join(exc.messages))
    if errors:
        raise ValidationError(errors)
    changed = 0
    for start in range(0, len(entries), 200):
        changed += record_count_page(actor=actor, task=task, entries=entries[start:start+200], request=request)
    return changed
