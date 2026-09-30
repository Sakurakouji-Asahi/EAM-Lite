from django import forms
from apps.core.query_forms import DateRangeQueryForm
from apps.reports.models import ExportLog


class ExportHistoryFilterForm(DateRangeQueryForm):
    period = forms.DateField(label="导出会计月份",required=False,input_formats=['%Y-%m'],
                             widget=forms.DateInput(format='%Y-%m',attrs={'type':'month'}))
    status = forms.ChoiceField(label="导出状态",required=False,choices=(('', '全部状态'),*ExportLog.Status.choices))

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['date_from'].label = '请求开始日期'
        self.fields['date_to'].label = '请求结束日期'
        for field in self.fields.values():
            field.widget.attrs.setdefault('class','form-select' if isinstance(field.widget,forms.Select) else 'form-control')
