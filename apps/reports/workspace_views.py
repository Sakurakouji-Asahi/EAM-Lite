from collections import Counter
from uuid import UUID

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseBadRequest, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.core.pagination import paginate_query
from apps.masterdata.permissions import current_company
from .models import ReportPreset, ExportLog
from .permissions import can_view_report, require_view_report, can_view_export
from .schemas import REPORT_REGISTRY, SUPPLY_REPORT_REGISTRY, RETIRED_REPORT_KEYS
from .catalog import report_url
from .relative_periods import relative_choices,apply_relative_period
from .history_forms import ExportHistoryFilterForm
from .export_history import export_file_context


def preset_context(actor, company, report_key, query):
    params = {key:value for key,value in query.items() if key not in {'page','summary_page'}}
    return {'relative_period_choices':relative_choices(report_key), 'report_presets':ReportPreset.objects.filter(company=company,user=actor,report_key=report_key),
        'preset_token':signing.dumps({'user':actor.pk,'company':str(company.pk), 'report':report_key,'query':params},salt='report-preset',compress=True)}


def _preset_url(preset):
    params = QueryDict(mutable=True)
    params.update(apply_relative_period(preset.report_key,preset.query))
    url = report_url(preset.report_key)
    if '?' in url:
        url = url.split('?',1)[0]
    return url + '?' + params.urlencode()


@never_cache
@login_required
@require_POST
def preset_save(request):
    company = current_company()
    try:
        payload = signing.loads(request.POST.get('preset_token',''),salt='report-preset',max_age=28800)
        if payload['user'] != request.user.pk or payload['company'] != str(company.pk):
            raise ValueError
        key, query = payload['report'], payload['query']
    except (signing.BadSignature, ValueError, KeyError, TypeError, AttributeError):
        return HttpResponseBadRequest('查询条件已失效，请重新查询后保存。')
    require_view_report(request.user, key)
    mode=request.POST.get('relative_period','')
    if mode not in dict(relative_choices(key)):
        return HttpResponseBadRequest('该报表不支持此动态期间。')
    query=dict(query)
    if mode: query['_relative_period']=mode
    name = request.POST.get('name','').strip()
    if not name or len(name) > 80:
        return HttpResponseBadRequest('查询名称需填写 1–80 个字符。')
    # Lock the owner to enforce the per-user bound under concurrent requests.
    with transaction.atomic():
        from django.contrib.auth import get_user_model
        get_user_model()._base_manager.select_for_update().get(pk=request.user.pk)
        presets = ReportPreset.objects.filter(company=company,user=request.user)
        existing = presets.filter(report_key=key,name=name).first()
        if existing is None and presets.count() >= 30:
            return HttpResponseBadRequest('最多保存 30 个常用查询，请先删除不用的查询。')
        preset, _ = ReportPreset.objects.update_or_create(company=company,user=request.user,report_key=key,name=name,defaults={'query':query})
    messages.success(request,'常用查询已保存；下次打开时重新读取数据并核对权限。')
    return redirect(_preset_url(preset))


@never_cache
@login_required
@require_GET
def preset_apply(request, pk):
    preset = get_object_or_404(ReportPreset, pk=pk, user=request.user, company=current_company())
    require_view_report(request.user, preset.report_key)
    # The destination validates all current department/employee/warehouse choices.
    return redirect(_preset_url(preset))


@never_cache
@login_required
@require_POST
def preset_delete(request, pk):
    preset = get_object_or_404(ReportPreset, pk=pk, user=request.user, company=current_company())
    key = preset.report_key
    preset.delete()
    messages.success(request,'常用查询已删除。')
    return redirect(report_url(key) if can_view_report(request.user,key) else reverse('reports:report-center'))


class ExportListForm(ExportHistoryFilterForm):
    q = forms.CharField(label='文件名、导出人或记录编号',required=False,max_length=200,
                        widget=forms.TextInput(attrs={'placeholder':'文件名、姓名、账号或完整记录编号'}))
    report_type = forms.ChoiceField(label='报表类型',required=False)
    mine = forms.BooleanField(label='只看本人导出',required=False)

    def __init__(self,*args,actor,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields.pop('period')
        self.fields['mine'].widget.attrs['class'] = 'form-check-input'
        self.fields['report_type'].choices = [('', '全部有权查看的报表')] + [(key,definition.title)
            for key,definition in {**REPORT_REGISTRY,**SUPPLY_REPORT_REGISTRY}.items() if can_view_report(actor,key)]


@never_cache
@login_required
@require_GET
def export_history(request):
    from .views import _company_or_400, _export_return_url
    from .export_history import history_search_context
    company = _company_or_400()
    form = ExportListForm(request.GET,actor=request.user)
    choices = [key for key,_ in form.fields['report_type'].choices if key]
    rows = ExportLog.objects.filter(company=company, export_type__in=choices).select_related('requested_by','output_attachment').order_by('-requested_at','-pk')
    valid = form.is_valid()
    if valid:
        data = form.cleaned_data
        if data['q']:
            search = Q(output_attachment__safe_filename__icontains=data['q']) | Q(requested_by__display_name__icontains=data['q']) | Q(requested_by__username__icontains=data['q'])
            try:
                search |= Q(pk=UUID(data['q']))
            except ValueError:
                pass
            rows = rows.filter(search)
        if data['report_type']:
            rows = rows.filter(export_type=data['report_type'])
        if data['date_from']:
            rows = rows.filter(requested_at__date__gte=data['date_from'])
        if data['date_to']:
            rows = rows.filter(requested_at__date__lte=data['date_to'])
        if data['mine']:
            rows = rows.filter(requested_by=request.user)
    else:
        rows = rows.none()
    # Metadata obeys the same permission, cost-column and historical-scope rules as downloads.
    visible = [row for row in rows.iterator() if can_view_export(request.user,row)]
    counts = Counter(row.status for row in visible)
    status_summary = []
    for status,label,count in [('', '全部状态',len(visible)), *((value,label,counts[value]) for value,label in ExportLog.Status.choices)]:
        params = request.GET.copy()
        params.pop('page',None)
        params.pop('status',None)
        if status:
            params['status'] = status
        status_summary.append({'label':label,'count':count,'url':reverse('reports:export-history') + ('?' + params.urlencode() if params else ''),
                               'selected':valid and form.cleaned_data['status'] == status})
    if valid and data['status']:
        visible = [row for row in visible if row.status == data['status']]
    page, query = paginate_query(request,visible)
    for row in page:
        row.file_context = export_file_context(request.user,row)
        row.source_url = _export_return_url(row)
    return render(request,'reports/export_history.html',{'form':form,'page_obj':page,'pagination_query':query,
        'history_query':request.GET.urlencode(),'status_summary':status_summary,'history_valid':valid,
        'history_has_filters':valid and any(form.cleaned_data.values()),
        **history_search_context(form)},status=200 if valid else 400)
