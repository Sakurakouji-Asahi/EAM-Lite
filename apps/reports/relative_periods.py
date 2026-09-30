from calendar import monthrange
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.utils import timezone


def relative_choices(report_key):
    from .catalog import FILTERS_BY_REPORT
    from .supply_forms import FILTERS_BY_REPORT as SUPPLY_FILTERS
    keys=SUPPLY_FILTERS.get(report_key,FILTERS_BY_REPORT.get(report_key,set()))
    choices=[('', '固定日期')]
    if {'period_start','period_end'}<=set(keys) or {'date_from','date_to'}<=set(keys):
        choices.extend((('month','本月'),('previous_month','上月'),('year','本年')))
    elif 'as_of_date' in keys:
        choices.append(('today','今日'))
    return choices


def apply_relative_period(report_key,query):
    params=dict(query)
    mode=params.pop('_relative_period','')
    if mode not in dict(relative_choices(report_key)):
        raise ValidationError('该报表不支持保存的动态期间，请重新保存查询。')
    if not mode:return params
    today=timezone.localdate()
    if mode=='today':
        params['as_of_date']=today.isoformat()
        return params
    if mode=='previous_month':
        end=today.replace(day=1)-timedelta(days=1)
        start=end.replace(day=1)
    elif mode=='year':
        start=today.replace(month=1,day=1);end=today.replace(month=12,day=31)
    else:
        start=today.replace(day=1);end=today.replace(day=monthrange(today.year,today.month)[1])
    names=('date_from','date_to') if 'date_from' in params else ('period_start','period_end')
    params.update({names[0]:start.isoformat(),names[1]:end.isoformat()})
    return params
