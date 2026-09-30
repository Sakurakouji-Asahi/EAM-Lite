"""Monthly work usage entry through the existing accounting service."""
import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import OuterRef, Subquery, Q
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import write_business_audit_log
from apps.core.pagination import paginate_query
from apps.masterdata.models import Company
from apps.masterdata.permissions import current_company
from .forms import _bootstrap_widgets
from .models import AssetDepreciationProfile, AssetWorkUsage
from .permissions import require_manage_finance, scoped_finance_assets
from .services import record_work_usage


class UsageQueryForm(forms.Form):
    month = forms.DateField(label='工作量月份',input_formats=['%Y-%m'],widget=forms.DateInput(format='%Y-%m',attrs={'type':'month'}))
    q = forms.CharField(label='资产或设备编号、名称',required=False,max_length=200)
    state = forms.ChoiceField(label='录入情况',required=False,choices=(('', '全部'),('missing','待录入'),('recorded','已录入')))
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        _bootstrap_widgets(self)


class UsageLineForm(forms.Form):
    units = forms.DecimalField(label='本期工作量',required=False,min_value=Decimal('0'),max_digits=24,decimal_places=6)
    remark = forms.CharField(label='备注',required=False,max_length=2000)
    def __init__(self,*args,profile,**kwargs):
        super().__init__(*args,**kwargs)
        self.profile=profile
        _bootstrap_widgets(self)
        for field in self.fields.values(): field.widget.attrs['aria-label']=f'{profile.asset.asset_code} {field.label}'
    def clean(self):
        values=super().clean()
        if values.get('remark') and values.get('units') is None:
            self.add_error('units','请填写工作量；空白行不保存。')
        return values


def month_bounds(month):
    start=month.replace(day=1)
    end=date(start.year+1,1,1) if start.month == 12 else date(start.year,start.month+1,1)
    return start,end


def monthly_profiles(actor,company,start,end):
    profiles=AssetDepreciationProfile.objects.filter(company=company,asset__in=scoped_finance_assets(actor,company),
        effective_from__lt=end,actual_continuation_date__lt=end).filter(Q(effective_to__isnull=True)|Q(effective_to__gte=start))
    applicable=profiles.filter(asset_id=OuterRef('asset_id')).order_by('-version').values('pk')[:1]
    usage=AssetWorkUsage.objects.filter(depreciation_profile_id=OuterRef('pk'),period_start=start,period_end=end)
    return profiles.filter(pk=Subquery(applicable),method='units_of_production').select_related('asset').annotate(
        usage_id=Subquery(usage.values('pk')[:1])).order_by('asset__asset_code','pk')


@transaction.atomic
def save_usage_page(*,actor,company,start,end,entries,key,request=None):
    require_manage_finance(actor)
    Company.objects.select_for_update().get(pk=company.pk)
    payload={'start':str(start),'end':str(end),'entries':entries}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
    marker=AuditLog.objects.filter(company=company,user=actor,action='finance.bulk_usage_saved',new_data_json__key=key).first()
    if marker:
        if marker.new_data_json['digest'] != digest:
            raise ValidationError('此页面已提交其他内容，请重新打开月份清单。')
        return 0,marker.new_data_json.get('capped',0)
    if len(entries)>200 or len({row['profile'] for row in entries}) != len(entries):
        raise ValidationError('每次最多 200 行，且不得重复。')
    allowed={str(row.pk):row for row in monthly_profiles(actor,company,start,end).filter(pk__in=[row['profile'] for row in entries])}
    if len(allowed) != len(entries):
        raise ValidationError('部分折旧参数或权限已变化，请刷新月份清单。')
    capped=0
    for row in entries:
        profile=allowed[row['profile']]
        try:
            result=record_work_usage(actor=actor,profile=profile,period_start=start,period_end=end,
                current_units=row['units'],work_unit=profile.work_unit,remark=row['remark'],request=request)
        except ValidationError as exc:
            raise ValidationError([f'{profile.asset.asset_code}：{message}' for message in exc.messages]) from exc
        capped += result.current_units != row['units']
    if entries:
        write_business_audit_log(company=company,user=actor,action='finance.bulk_usage_saved',object_type='AssetWorkUsage',
            object_id=key,new_data={'key':key,'digest':digest,'count':len(entries),'capped':capped,'period_start':str(start)})
    return len(entries),capped


@login_required
def monthly_usage(request):
    require_manage_finance(request.user)
    company=current_company()
    data=request.GET.copy()
    data.setdefault('month',timezone.localdate().strftime('%Y-%m'))
    form=UsageQueryForm(data)
    context={'filter_form':form}
    if not form.is_valid():
        return render(request,'finance/monthly_usage.html',context,status=400)
    start,end=month_bounds(form.cleaned_data['month'])
    base=monthly_profiles(request.user,company,start,end)
    context.update(total=base.count(),missing=base.filter(usage_id__isnull=True).count(),month=start)
    selected=base
    q=form.cleaned_data['q']
    if q: selected=selected.filter(Q(asset__asset_code__icontains=q)|Q(asset__equipment_number__icontains=q)|Q(asset__asset_name__icontains=q))
    if form.cleaned_data['state']: selected=selected.filter(usage_id__isnull=form.cleaned_data['state']=='missing')
    page,query=paginate_query(request,selected,per_page=50)
    posted=request.method=='POST'
    if posted:
        try:
            manifest=signing.loads(request.POST.get('manifest',''),salt='monthly-usage',max_age=28800)
            if manifest['actor']!=request.user.pk or manifest['month']!=str(start) or len(manifest['ids'])>200:
                raise ValueError
            profiles=list(base.filter(pk__in=manifest['ids']))
            if len(profiles)!=len(manifest['ids']): raise ValueError
        except (signing.BadSignature,ValueError,KeyError,TypeError,ValidationError):
            form.add_error(None,'录入页面已失效，请重新查询月份。')
            return render(request,'finance/monthly_usage.html',context,status=400)
    else:
        profiles=[row for row in page if row.usage_id is None]
        manifest={'actor':request.user.pk,'month':str(start),'ids':[str(row.pk) for row in profiles],'key':uuid.uuid4().hex}
    row_forms=[UsageLineForm(request.POST if posted else None,prefix=f'usage-{row.pk}',profile=row) for row in profiles]
    if posted and all([row.is_valid() for row in row_forms]):
        entries=[{'profile':str(row.profile.pk),'units':row.cleaned_data['units'],'remark':row.cleaned_data['remark']}
                 for row in row_forms if row.cleaned_data.get('units') is not None]
        try:
            changed,capped=save_usage_page(actor=request.user,company=company,start=start,end=end,entries=entries,key=manifest['key'],request=request)
        except ValidationError as exc:
            for error in exc.messages: form.add_error(None,error)
        else:
            messages.success(request,f'已保存 {changed} 项工作量；空白未保存。'+(f'其中 {capped} 项按原规则限制到剩余预计工作量，原输入已记在备注中。' if capped else ''))
            return redirect(reverse('finance:monthly-usage')+'?'+data.urlencode())
    known=AssetWorkUsage.objects.filter(pk__in=[row.usage_id for row in page if row.usage_id])
    context.update(page_obj=page,pagination_query=query,row_forms=row_forms,
        recorded={str(row.depreciation_profile_id):row for row in known},manifest=signing.dumps(manifest,salt='monthly-usage',compress=True))
    context['recorded_rows']=[row for row in known]
    return render(request,'finance/monthly_usage.html',context)
