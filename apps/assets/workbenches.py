from datetime import timedelta
from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from apps.core.pagination import paginate_query
from apps.masterdata.permissions import current_company, role_names_for
from .models import AssetLoan
from .permissions import scoped_assets_p1
from .lifecycle_permissions import scoped_disposals, can_lifecycle_action


def loan_rows(actor, company):
    return AssetLoan.objects.filter(company=company, asset__in=scoped_assets_p1(actor, company)).select_related('asset__department','borrower_employee')


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

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['state'].choices = ((('', '全部记录'), ('active','未归还'), ('overdue','已逾期'), ('due','未来 7 天内到期'), ('returned','已归还'))
            if kind == 'loans' else (('', '全部记录'), ('open','未完成'), ('actual','待补实际结果'), ('finance','待财务核对'), ('complete','待完成处置'), ('closed','已结束')))
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
    summary = lifecycle_work_summary(request.user, company)
    if not company or not summary['can_view_'+kind]:
        raise PermissionDenied('您没有查看此待办的权限。')
    rows = loan_rows(request.user, company) if kind == 'loans' else scoped_disposals(request.user,company).select_related('asset__department')
    data = request.GET.copy()
    if 'state' not in data:
        data['state'] = 'active' if kind == 'loans' else 'open'
    form = WorkFilter(data, kind=kind)
    today = timezone.localdate()
    if form.is_valid():
        q, state = form.cleaned_data['q'], form.cleaned_data['state']
        if q:
            rows = rows.filter(Q(asset__asset_code__icontains=q)|Q(asset__equipment_number__icontains=q)|Q(asset__asset_name__icontains=q))
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
    else:
        rows = rows.none()
    page, query = paginate_query(request, rows.order_by('expected_return_date','pk') if kind == 'loans' else rows.order_by('planned_disposal_date','pk'))
    for row in page:
        if kind == 'loans':
            row.overdue = row.status == 'active' and row.expected_return_date < today
            row.can_return = row.status == 'active' and can_lifecycle_action(request.user,row.asset,'loan_return')
        else:
            row.work_step = ('待补实际结果' if row.actual_disposal_date is None else '待财务核对') if row.status == 'draft' else ('待完成处置' if row.status == 'finance_locked' else row.get_status_display())
    return render(request,'assets/workbench.html', {'kind':kind, 'title':'借用归还待办' if kind == 'loans' else '资产处置待办',
        'form':form, 'page_obj':page, 'pagination_query':query, 'summary':summary}, status=200 if form.is_valid() else 400)
