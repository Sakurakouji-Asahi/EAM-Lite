from django import forms
from django.db.models import Q
from apps.masterdata.models import Department, Employee
from .forms import MaintenanceRecordFilterForm, _style
from .models import MaintenancePlan, MaintenanceRecord


class PlanQueryForm(forms.Form):
    q = forms.CharField(label='设备编号、资产编号或计划', required=False, max_length=200)
    department = forms.ModelChoiceField(label='部门',required=False,queryset=Department.objects.none())
    responsible_employee = forms.ModelChoiceField(label='负责人',required=False,queryset=Employee.objects.none())
    status = forms.ChoiceField(label='计划状态',required=False,choices=(('', '全部状态'),*MaintenancePlan.Status.choices))
    due_scope = forms.ChoiceField(label='到期情况',required=False,choices=(('', '全部待保养'),('upcoming','即将到期'),('due_today','今日到期'),('overdue','已逾期')))

    def __init__(self,*args,plans,due=False,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['department'].queryset = Department.objects.filter(pk__in=plans.values('asset__department_id')).order_by('normalized_code')
        self.fields['responsible_employee'].queryset = Employee.objects.filter(pk__in=plans.values('responsible_employee_id')).order_by('normalized_employee_no')
        self.fields.pop('status' if due else 'due_scope')
        _style(self)

    def apply(self,plans):
        if not self.is_valid():
            return plans.none()
        values = self.cleaned_data
        if values['q']:
            q=values['q']
            plans=plans.filter(Q(name__icontains=q)|Q(asset__asset_code__icontains=q)|Q(asset__equipment_number__icontains=q)|Q(asset__asset_name__icontains=q))
        if values['department']:
            plans=plans.filter(asset__department=values['department'])
        if values['responsible_employee']:
            plans=plans.filter(responsible_employee=values['responsible_employee'])
        if values.get('status'):
            plans=plans.filter(status=values['status'])
        return plans


class PlanHistoryForm(MaintenanceRecordFilterForm):
    result = forms.ChoiceField(label='保养结果',required=False,choices=(('', '全部结果'),*MaintenanceRecord.Result.choices))

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['q'].label='内容或备注'
        self.fields['q'].widget.attrs['placeholder']='实际内容、备注或问题描述'

    def apply(self,records):
        if not self.is_valid():
            return records.none()
        data=self.cleaned_data
        if data['q']:
            records=records.filter(Q(actual_content__icontains=data['q'])|Q(remark__icontains=data['q'])|Q(problem__description__icontains=data['q']))
        for field in ('status','result'):
            if data[field]: records=records.filter(**{field:data[field]})
        if data['date_from']: records=records.filter(completed_date__gte=data['date_from'])
        if data['date_to']: records=records.filter(completed_date__lte=data['date_to'])
        return records
