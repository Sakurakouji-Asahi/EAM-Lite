"""Read-only estimates using the same rounding rules as posting."""
from decimal import Decimal
import hashlib
import json

from django.core.exceptions import ValidationError
from django.core import signing
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .domain import allocate_custody_amount, calculate_issue, calculate_receipt_amount, quantize_money, quantize_quantity
from .models import SupplyStockBalance
from .permissions import can_view_supply_cost


def posting_blockers(document, *, persisted_lines=None):
    """Early feedback only; posting repeats all checks under database locks."""
    from .models import SupplyCountTask
    from .services import ACTIVE_SUPPLY_COUNT_STATUSES, _assert_custody_count_action_allowed
    if document.status != 'draft':
        return []
    errors = []
    warehouses = {w.pk: w for w in (document.source_warehouse, document.target_warehouse) if w}
    for warehouse in warehouses.values():
        if not warehouse.is_active:
            errors.append(f'仓库 {warehouse.name} 已停用，请更正或取消草稿。')
        if SupplyCountTask.objects.filter(company=document.company, warehouse=warehouse,
                count_domain='warehouse_stock', status__in=ACTIVE_SUPPLY_COUNT_STATUSES).exists():
            errors.append(f'仓库 {warehouse.name} 正在盘点，盘点关闭或取消后才能过账。')
    lines = persisted_lines if persisted_lines is not None else document.lines.select_related('item','source_custody')
    for line in lines:
        if not line.item.is_active:
            errors.append(f'物品 {line.item.item_code} 已停用，不能过账。')
        if line.source_custody_id:
            try:
                _assert_custody_count_action_allowed(custody=line.source_custody)
            except ValidationError as exc:
                errors.extend(exc.messages)
    if document.document_type == 'issue':
        department, employee = document.department, document.employee
        if not department or not department.is_active:
            errors.append('领用部门必须处于启用状态。')
        if employee and (employee.employment_status != 'active' or not employee.is_active):
            errors.append('领用员工已离职、离职办理中或停用，不能新增领用。')
        if employee and employee.department_id != document.department_id:
            errors.append('领用员工与所选部门不一致，请更正草稿。')
    return errors


def preview_token(actor, document, preview):
    if not preview or preview['has_errors']:
        return ''
    return signing.dumps({'actor':actor.pk, 'document':str(document.pk),
        'at':preview['as_of'].isoformat(), 'total':str(preview['total_amount']), 'contents':preview['contents'],
        'rows':{str(row['line'].pk):str(row['amount']) for row in preview['rows']}},
        salt='supply-posting-preview', compress=True)


def document_contents(document, *, persisted_lines=None):
    fields = ('document_type','business_date','source_warehouse_id','target_warehouse_id','department_id','employee_id')
    content = {key:str(getattr(document,key)) for key in fields}
    lines = (document.lines.order_by('pk') if persisted_lines is None
             else sorted(persisted_lines, key=lambda line: line.pk))
    content['lines'] = [[str(getattr(line,key)) for key in ('pk','item_id','quantity','entered_unit_cost','source_issue_line_id','source_custody_id')]
                         for line in lines]
    return hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()


@transaction.atomic
def post_with_preview(*, actor, document, token='', **kwargs):
    from .services import post_supply_document, _audit
    from .models import SupplyDocument
    snapshot = None
    # Serialize retries so a single actual posting has a single comparison record.
    document = SupplyDocument.objects.select_for_update().get(pk=document.pk)
    already_posted = document.status == 'posted'
    if token and not already_posted:
        try:
            snapshot = signing.loads(token, salt='supply-posting-preview', max_age=28800)
            if snapshot['actor'] != actor.pk or snapshot['document'] != str(document.pk):
                raise ValueError
        except (signing.BadSignature, ValueError, KeyError, TypeError):
            raise ValidationError('金额预估已失效，请刷新页面核对后提交。')
        if snapshot.get('contents') != document_contents(document):
            raise ValidationError('草稿内容已被修改，请刷新核对物品、数量和责任范围后再提交。')
    result = post_supply_document(actor=actor, document=document, **kwargs)
    if snapshot and can_view_supply_cost(actor):
        actual = {str(line.pk):str(line.posted_amount) for line in result.lines.all()}
        total = sum((Decimal(amount) for amount in actual.values()), Decimal('0.00'))
        _audit(actor=actor, action='supply_posting_comparison', instance=result,
            old=snapshot, new={'rows':actual, 'total':str(total),
                'difference':str(total - Decimal(snapshot['total'])),
                'changed':actual != snapshot['rows']}, request=kwargs.get('request'))
    return result


def posting_comparison(actor, document):
    from apps.audit.models import AuditLog
    if not can_view_supply_cost(actor):
        return None
    log = AuditLog.objects.filter(company=document.company, object_type='SupplyDocument',
        object_id=str(document.pk), action='supply_posting_comparison').first()
    if not log:
        return None
    return {'before':log.old_data_json['total'], **log.new_data_json}


def consumable_return_amount(*, source, quantity, returned_quantity, returned_amount):
    returned_quantity = quantize_quantity(returned_quantity or Decimal('0'))
    returned_amount = quantize_money(returned_amount or Decimal('0'))
    remaining_quantity = quantize_quantity(source.quantity - returned_quantity)
    remaining_amount = quantize_money(source.posted_amount - returned_amount)
    if remaining_quantity < 0 or remaining_amount < 0:
        raise ValidationError("原领用累计退回数量或金额异常，请先执行库存核对。")
    if quantity > remaining_quantity:
        raise ValidationError(f"退回数量超过原领用未退数量：当前最多可退 {remaining_quantity} {source.item.unit}。")
    if quantity == remaining_quantity:
        amount = remaining_amount
    else:
        cumulative = min(quantize_money((returned_quantity + quantity) * source.posted_unit_cost), source.posted_amount)
        amount = quantize_money(cumulative - returned_amount)
    if amount < 0 or amount > remaining_amount:
        raise ValidationError("原领用剩余可退金额异常，请先执行库存核对。")
    return amount


def document_posting_preview(*, actor, document, persisted_lines=None):
    if document.status != 'draft' or not can_view_supply_cost(actor):
        return None
    lines = (list(document.lines.select_related('item', 'source_issue_line__item', 'source_issue_line__document', 'source_custody'))
             if persisted_lines is None else list(persisted_lines))
    balances = {row.item_id:(row.quantity_on_hand,row.amount_on_hand) for row in SupplyStockBalance.objects.filter(
        company=document.company, warehouse_id=document.source_warehouse_id, item_id__in=[line.item_id for line in lines])}
    result = []
    total = Decimal('0.00')
    for line in lines:
        amount, cost, error = None, None, ''
        try:
            if document.document_type in {'opening', 'receipt'}:
                if line.entered_unit_cost is None:
                    raise ValidationError('尚未填写单位成本。')
                cost = line.entered_unit_cost
                amount = calculate_receipt_amount(line.quantity, cost)
            elif document.document_type in {'issue', 'transfer'}:
                quantity_before, amount_before = balances.get(line.item_id,(Decimal('0'),Decimal('0')))
                calculated = calculate_issue(quantity_before, amount_before, line.quantity)
                cost, amount = calculated.issue_unit_cost, calculated.issue_amount
                balances[line.item_id] = (calculated.quantity_after, calculated.amount_after)
            elif document.document_type == 'return' and line.source_issue_line_id:
                source = line.source_issue_line
                if source.document.status != 'posted':
                    raise ValidationError('原领用单已失效。')
                returned = source.return_lines.filter(document__document_type='return',document__status='posted').aggregate(
                    quantity=Sum('quantity'),amount=Sum('posted_amount'))
                cost = source.posted_unit_cost
                amount = consumable_return_amount(source=source,quantity=line.quantity,
                    returned_quantity=returned['quantity'],returned_amount=returned['amount'])
            elif document.document_type == 'return' and line.source_custody_id:
                custody = line.source_custody
                if custody.status != 'open':
                    raise ValidationError('来源保管已结清。')
                cost = custody.unit_cost_snapshot
                amount = allocate_custody_amount(current_quantity=custody.current_quantity,
                    current_amount=custody.current_amount,unit_cost_snapshot=cost,action_quantity=line.quantity).action_amount
            else:
                raise ValidationError('本单据类型不支持草稿金额预估。')
        except ValidationError as exc:
            error = '；'.join(exc.messages)
        result.append({'line':line,'unit_cost':cost,'amount':amount,'error':error})
        if amount is not None:
            total += amount
    has_errors = any(row['error'] for row in result)
    return {'rows':result, 'total_amount':None if has_errors else total, 'has_errors':has_errors,
            'as_of':timezone.now(), 'contents':document_contents(document, persisted_lines=lines)}
