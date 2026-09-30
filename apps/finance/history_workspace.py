from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404,render
from django.utils import timezone
from apps.core.pagination import paginate_query
from apps.core.query_forms import DateRangeQueryForm
from apps.audit.display import localize_audit_payload
from apps.masterdata.permissions import current_company
from .forms import _bootstrap_widgets
from .models import DepreciationEntry,TheoreticalDepreciationRun
from .permissions import require_view_finance,scoped_finance_assets


class EntryQueryForm(forms.Form):
    year = forms.IntegerField(label='年度',required=False,min_value=1900,max_value=9999)
    source = forms.ChoiceField(label='分录来源',required=False,choices=(('', '全部来源'),*DepreciationEntry._meta.get_field('source_type').choices))
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        _bootstrap_widgets(self)


def entry_history_context(request,entries):
    form=EntryQueryForm(request.GET,prefix='entry')
    if form.is_valid():
        if form.cleaned_data['year']: entries=entries.filter(period_start__year=form.cleaned_data['year'])
        if form.cleaned_data['source']: entries=entries.filter(source_type=form.cleaned_data['source'])
    else:
        entries=entries.none()
    page,query=paginate_query(request,entries.order_by('-period_start','-created_at','pk'),parameter='entry_page')
    return {'entry_filter':form,'entries':page.object_list,'entry_page':page,'entry_query':query}


class TheoryQueryForm(DateRangeQueryForm):
    status=forms.ChoiceField(label='试算状态',required=False,choices=(('', '全部'),*TheoreticalDepreciationRun.Status.choices))
    compare_a=forms.ModelChoiceField(label='对照记录 A',required=False,queryset=TheoreticalDepreciationRun.objects.none())
    compare_b=forms.ModelChoiceField(label='对照记录 B',required=False,queryset=TheoreticalDepreciationRun.objects.none())
    def __init__(self,*args,runs,**kwargs):
        super().__init__(*args,**kwargs)
        for name in ('compare_a','compare_b'):
            self.fields[name].queryset=runs
            self.fields[name].label_from_instance=lambda run:f'{timezone.localtime(run.requested_at):%Y-%m-%d %H:%M} · 截止 {run.as_of_date} · {str(run.pk)[:8]}'
        self.fields['date_from'].label='试算截止日期起'
        self.fields['date_to'].label='试算截止日期止'
        _bootstrap_widgets(self)


@login_required
def theoretical_history(request,pk):
    require_view_finance(request.user)
    asset=get_object_or_404(scoped_finance_assets(request.user,current_company()),pk=pk)
    all_runs=asset.theoretical_depreciation_runs.select_related('requested_by').order_by('-requested_at','pk')
    form=TheoryQueryForm(request.GET,runs=all_runs)
    selected=all_runs
    comparison=[]
    if form.is_valid():
        data=form.cleaned_data
        if data['status']: selected=selected.filter(status=data['status'])
        if data['date_from']: selected=selected.filter(as_of_date__gte=data['date_from'])
        if data['date_to']: selected=selected.filter(as_of_date__lte=data['date_to'])
        a,b=data['compare_a'],data['compare_b']
        if a and b:
            left=localize_audit_payload(a.parameter_snapshot_json)
            right=localize_audit_payload(b.parameter_snapshot_json)
            comparison=[{'label':key,'a':left.get(key),'b':right.get(key),'changed':left.get(key)!=right.get(key)}
                        for key in sorted(set(left)|set(right))]
            comparison.insert(0,{'label':'试算截止日期','a':a.as_of_date,'b':b.as_of_date,'changed':a.as_of_date!=b.as_of_date})
    else: selected=selected.none()
    page,query=paginate_query(request,selected)
    return render(request,'finance/theoretical_history.html',{'asset':asset,'filter_form':form,'page_obj':page,
        'pagination_query':query,'comparison':comparison},status=200 if form.is_valid() else 400)
