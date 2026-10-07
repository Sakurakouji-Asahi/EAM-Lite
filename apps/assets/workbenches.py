from datetime import timedelta
from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, DateField, F, Q, Value, When
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from apps.core.pagination import paginate_query
from apps.masterdata.permissions import current_company, role_names_for
from .models import AssetLoan
from .permissions import scoped_assets_p1
from .lifecycle_permissions import scoped_disposals, can_lifecycle_action


def loan_rows(actor, company):
    return AssetLoan.objects.filter(company=company, asset__in=scoped_assets_p1(actor, company)).select_related(
        'asset__department', 'borrower_employee', 'received_by_employee', 'return_department')


DISPOSAL_STEPS = {
    'actual': Q(status='draft', actual_disposal_date__isnull=True),
    'finance': Q(status='draft', actual_disposal_date__isnull=False),
    'complete': Q(status='finance_locked'),
    'closed': Q(status__in=('confirmed','cancelled','reversed')),
}


def lifecycle_work_summary(actor, company):
    today = timezone.localdate()
    loans = loan_rows(actor, company)
    disposals = scoped_disposals(actor, company)
    roles = role_names_for(actor)
    return {'can_view_loans':bool(roles.intersection({'system_admin','finance','equipment','warehouse','management','department_manager','employee'})),
        'can_view_disposals':bool(roles.intersection({'system_admin','finance','equipment','warehouse','management','department_manager','employee'})),
        'loans':loans.aggregate(active=Count('pk',filter=Q(status='active')),
            overdue=Count('pk',filter=Q(status='active',expected_return_date__lt=today)),
            due=Count('pk',filter=Q(status='active',expected_return_date__range=(today,today+timedelta(days=7))))),
        'disposals':disposals.aggregate(**{key:Count('pk',filter=value) for key,value in DISPOSAL_STEPS.items()})}


class WorkFilter(forms.Form):
    q = forms.CharField(label='资产编号、设备编号或名称', required=False, max_length=200)
    state = forms.ChoiceField(label='办理阶段', required=False)
    order = forms.ChoiceField(label='排列方式', required=False, initial='priority')

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['state'].choices = ((('', '全部记录'), ('active','未归还'), ('overdue','已逾期'), ('due','未来 7 天内到期'), ('returned','已归还'))
            if kind == 'loans' else (('', '全部记录'), ('open','未完成'), ('actual','待补实际结果'), ('finance','待财务核对'), ('complete','待完成处置'), ('closed','已结束')))
        if kind == 'loans':
            self.fields['q'].label = '资产或借用方'
            self.fields['q'].widget.attrs['placeholder'] = '资产编号、设备编号、名称、借用人或借用单位'
        else:
            self.fields['q'].widget.attrs['placeholder'] = '资产编号、设备编号或名称'
        self.fields['order'].choices = (('priority', '未完成事项优先'),
            ('date', '预计归还日期从早到晚' if kind == 'loans' else '拟处置日期从早到晚'),
            ('recent', '最近记录优先'))
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'


@never_cache
@login_required
@require_GET
def loan_workbench(request):
    return _workbench(request, 'loans')


@never_cache
@login_required
@require_GET
def disposal_workbench(request):
    return _workbench(request, 'disposals')


def _workbench(request, kind):
    from django.core.exceptions import PermissionDenied
    company = current_company()
    if not company:
        raise PermissionDenied('您没有查看此待办的权限。')
    summary = lifecycle_work_summary(request.user, company)
    if not company or not summary['can_view_'+kind]:
        raise PermissionDenied('您没有查看此待办的权限。')
    rows = loan_rows(request.user, company) if kind == 'loans' else scoped_disposals(request.user,company).select_related('asset__department')
    data = request.GET.copy()
    if 'state' not in data:
        data['state'] = 'active' if kind == 'loans' else 'open'
    form = WorkFilter(data, kind=kind)
    today = timezone.localdate()
    valid = form.is_valid()
    if valid:
        q, state = form.cleaned_data['q'], form.cleaned_data['state']
        if q:
            search = Q(asset__asset_code__icontains=q)|Q(asset__equipment_number__icontains=q)|Q(asset__asset_name__icontains=q)
            if kind == 'loans':
                search |= Q(borrower_name_snapshot__icontains=q)|Q(borrower_name__icontains=q)|Q(borrower_organization__icontains=q)
            rows = rows.filter(search)
    else:
        rows = rows.none()
        q, state = '', ''
    # Status totals reflect the search, but stay useful while changing stage.
    if kind == 'loans':
        counts = rows.aggregate(active=Count('pk', filter=Q(status='active')),
            overdue=Count('pk', filter=Q(status='active', expected_return_date__lt=today)),
            due=Count('pk', filter=Q(status='active', expected_return_date__range=(today, today+timedelta(days=7)))),
            returned=Count('pk', filter=Q(status='returned')))
    else:
        counts = rows.aggregate(**{key:Count('pk', filter=value) for key,value in DISPOSAL_STEPS.items()})
    if valid:
        if kind == 'loans':
            if state in ('active','returned'):
                rows = rows.filter(status=state)
            elif state == 'overdue':
                rows = rows.filter(status='active',expected_return_date__lt=today)
            elif state == 'due':
                rows = rows.filter(status='active',expected_return_date__range=(today,today+timedelta(days=7)))
        elif state == 'open':
            rows = rows.filter(status__in=('draft','finance_locked'))
        elif state:
            rows = rows.filter(DISPOSAL_STEPS[state])
    order = form.cleaned_data.get('order') or 'priority'
    date_field = 'expected_return_date' if kind == 'loans' else 'planned_disposal_date'
    if order == 'recent':
        rows = rows.order_by('-loan_date', '-created_at', 'pk') if kind == 'loans' else rows.order_by('-application_date', '-created_at', 'pk')
    elif order == 'date':
        rows = rows.order_by(date_field, 'pk')
    elif kind == 'loans':
        rows = rows.annotate(
            _work_rank=Case(When(status='active', then=0), default=1),
            _active_due=Case(When(status='active', then=F('expected_return_date')), default=Value(None), output_field=DateField()),
        ).order_by('_work_rank', '_active_due', '-returned_at', '-loan_date', 'pk')
    else:
        rows = rows.annotate(_work_rank=Case(When(status__in=('draft','finance_locked'), then=0), default=1)).order_by('_work_rank', date_field, 'pk')
    page, query = paginate_query(request, rows)
    for row in page:
        if kind == 'loans':
            row.overdue = row.status == 'active' and row.expected_return_date < today
            row.can_return = row.status == 'active' and can_lifecycle_action(request.user,row.asset,'loan_return')
            gap = (row.expected_return_date - today).days
            row.due_timing = '已归还' if row.status == 'returned' else (f'逾期 {-gap} 天' if gap < 0 else '今日到期' if gap == 0 else f'距归还 {gap} 天')
        else:
            row.work_step = ('待补实际结果' if row.actual_disposal_date is None else '待财务核对') if row.status == 'draft' else ('待完成处置' if row.status == 'finance_locked' else row.get_status_display())
            row.work_action_url = None
            action = None
            if row.status == 'draft':
                action = ('disposal_actual_details', 'assets:disposal-actual', '登记实际结果') if row.actual_disposal_date is None else ('disposal_finance_lock', 'assets:disposal-finance-lock', '财务核对')
            elif row.status == 'finance_locked':
                action = ('disposal_complete', 'assets:disposal-complete', '完成处置')
            if action and can_lifecycle_action(request.user, row.asset, action[0]):
                row.work_action_url = reverse(action[1], args=[row.pk])
                row.work_action_label = action[2]
    links = {}
    link_params = request.GET.copy()
    link_params.pop('page', None)
    for key in counts:
        link_params['state'] = key
        links[key] = '?' + link_params.urlencode()
    active_filters = []
    if valid:
        for name in ('q', 'state', 'order'):
            value = form.cleaned_data[name]
            if not value or (name == 'order' and value == 'priority'):
                continue
            params = request.GET.copy()
            params.pop('page', None)
            if name == 'state':
                params['state'] = ''
            else:
                params.pop(name, None)
            label = dict(form.fields[name].choices).get(value, value) if name != 'q' else value
            active_filters.append({'label':form.fields[name].label, 'value':label, 'url':'?' + params.urlencode()})
    return render(request,'assets/workbench.html', {'kind':kind, 'title':'借用归还待办' if kind == 'loans' else '资产处置待办',
        'form':form, 'filter_form':form, 'page_obj':page, 'pagination_query':query, 'summary':summary,
        'work_counts':counts, 'work_links':links, 'active_filters':active_filters, 'order':order}, status=200 if valid else 400)
