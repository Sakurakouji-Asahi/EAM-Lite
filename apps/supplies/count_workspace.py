"""Scoped count queries and atomic page entry with stale-edit protection."""
from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q

from apps.masterdata.permissions import role_names_for
from .forms import SupplyCountRecordForm, _bootstrap_widgets
from .models import SupplyCountLine
from .permissions import require_record_supply_count


class CountResultFilterForm(forms.Form):
    q = forms.CharField(label="物品、部门或责任人", required=False, max_length=200)
    row_view = forms.ChoiceField(label="查看范围", required=False, choices=(
        ('','全部明细'),('unrecorded','未录入'),('different','有差异'),('same','无差异'),
        ('needs_cost','待补盘盈成本'),('unresolved','待处理差异'),
    ))
    page_size = forms.TypedChoiceField(label="每页显示",required=False,coerce=int,
        choices=((25,'25 条'),(50,'50 条'),(100,'100 条'),(200,'200 条')))

    def __init__(self,*args,show_cost=True,**kwargs):
        super().__init__(*args,**kwargs)
        if not show_cost:
            self.fields['row_view'].choices = [choice for choice in self.fields['row_view'].choices if choice[0] != 'needs_cost']
        _bootstrap_widgets(self)


def visible_count_lines(actor, task):
    lines = SupplyCountLine.objects.filter(count_task=task).select_related(
        'item','count_task','stock_balance','custody__department','custody__employee',
        'adjustment_document_line__document','resolution_custody_movement','counted_by','resolved_by')
    roles = role_names_for(actor)
    if 'employee' in roles and not roles.intersection({'system_admin','finance','warehouse','equipment','management','department_manager'}):
        lines = lines.filter(custody__employee__user=actor)
    return lines.order_by('item_code_snapshot','pk')


def count_summary(lines):
    different = Q(difference_quantity__gt=0) | Q(difference_quantity__lt=0)
    return lines.aggregate(
        total=Count('pk'), recorded=Count('pk',filter=Q(counted_quantity__isnull=False)),
        unrecorded=Count('pk',filter=Q(counted_quantity__isnull=True)),
        different=Count('pk',filter=different),
        needs_cost=Count('pk',filter=Q(count_task__count_domain='warehouse_stock',expected_quantity=0,
                                     difference_quantity__gt=0,adjustment_unit_cost__isnull=True)),
        unresolved=Count('pk',filter=different & Q(adjustment_document_line__isnull=True,resolution_custody_movement__isnull=True)),
    )


def filter_count_lines(lines, form):
    if not form.is_valid():
        return lines.none()
    query = form.cleaned_data['q']
    if query:
        lines = lines.filter(Q(item_code_snapshot__icontains=query)|Q(item_name_snapshot__icontains=query)
            |Q(item__name__icontains=query)|Q(department_snapshot__icontains=query)|Q(employee_snapshot__icontains=query))
    view = form.cleaned_data['row_view']
    different = Q(difference_quantity__gt=0) | Q(difference_quantity__lt=0)
    if view == 'unrecorded':
        return lines.filter(counted_quantity__isnull=True)
    if view == 'different':
        return lines.filter(different)
    if view == 'same':
        return lines.filter(difference_quantity=0)
    if view == 'needs_cost':
        return lines.filter(count_task__count_domain='warehouse_stock',expected_quantity=0,
                            difference_quantity__gt=0,adjustment_unit_cost__isnull=True)
    if view == 'unresolved':
        return lines.filter(different,adjustment_document_line__isnull=True,resolution_custody_movement__isnull=True)
    return lines


class BulkCountLineForm(SupplyCountRecordForm):
    expected_counted_at = forms.CharField(required=False,widget=forms.HiddenInput)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['counted_quantity'].required = False
        self.fields['remark'].widget = forms.TextInput(attrs={'class':'form-control'})
        self.initial['expected_counted_at'] = self.line.counted_at.isoformat() if self.line.counted_at else ''
        for name,field in self.fields.items():
            if not isinstance(field.widget,forms.HiddenInput):
                field.widget.attrs['aria-label'] = f'{self.line.item_name_snapshot} {field.label}'

    def clean_counted_quantity(self):
        if self.cleaned_data.get('counted_quantity') is None:
            return None
        return super().clean_counted_quantity()

    def clean(self):
        cleaned = super().clean()
        quantity = cleaned.get('counted_quantity')
        if quantity is None:
            if cleaned.get('remark') or cleaned.get('adjustment_unit_cost') is not None or cleaned.get('zero_cost_reason'):
                self.add_error('counted_quantity','请填写实盘数量；空白行不会保存。')
        elif quantity != self.line.expected_quantity and not cleaned.get('remark'):
            self.add_error('remark','存在差异时必须填写原因。')
        return cleaned


@transaction.atomic
def record_count_page(*, actor, task, entries, request=None):
    from .services import _lock_supply_count_task, record_supply_count
    task = _lock_supply_count_task(task)
    if task.status != 'in_progress':
        raise ValidationError('只有进行中的盘点任务可以录入。')
    ids = [str(entry['line_id']) for entry in entries]
    if len(ids) != len(set(ids)) or len(ids) > 200:
        raise ValidationError('提交的盘点行重复或超过每次 200 行。')
    lines = {str(line.pk):line for line in SupplyCountLine.objects.select_for_update(of=("self",)).filter(
        count_task=task,pk__in=ids).select_related('count_task','item','custody__employee','custody__department').order_by('pk')}
    if len(lines) != len(ids):
        raise ValidationError('提交内容包含不属于本任务的盘点行。')
    changed = 0
    for entry in entries:
        line = lines[str(entry['line_id'])]
        require_record_supply_count(actor,line)
        remark = str(entry.get('remark') or '').strip()
        cost = entry.get('adjustment_unit_cost')
        zero_reason = entry.get('zero_cost_reason',line.zero_cost_reason)
        unchanged = (line.counted_quantity == entry['counted_quantity'] and line.remark == remark
                     and (cost is None or cost == line.adjustment_unit_cost) and zero_reason == line.zero_cost_reason)
        if unchanged:
            continue
        current_version = line.counted_at.isoformat() if line.counted_at else ''
        if entry.get('expected_counted_at','') != current_version:
            raise ValidationError({str(line.pk): f'{line.item_code_snapshot} 已被更新，请刷新核对后再提交。'})
        try:
            record_supply_count(actor=actor,line=line,counted_quantity=entry['counted_quantity'],remark=remark,
                adjustment_unit_cost=cost,zero_cost_reason=zero_reason,request=request)
        except ValidationError as exc:
            raise ValidationError({str(line.pk):exc.messages}) from exc
        changed += 1
    return changed
