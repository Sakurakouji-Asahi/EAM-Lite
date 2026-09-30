from django import forms
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.masterdata.models import Employee
from apps.masterdata.permissions import role_names_for, resolve_department_ids
from apps.maintenance.forms import _style
from apps.maintenance.domain import business_date
from apps.maintenance.models import MaintenanceProblem
from apps.maintenance.permissions import require_close_maintenance_problem
from apps.maintenance.services import _lock_plan, _base_update, _required, _audit, _check_operation_idempotency, _write_operation_marker, _request_hash


def assignment_version(problem):
    return _request_hash({'owner':problem.owner_employee_id,'target_date':problem.target_date,'status':problem.status})


def assignment_owners(actor,problem):
    query = Employee.objects.filter(company=problem.company,is_active=True,employment_status='active',department__is_active=True)
    if 'equipment' not in role_names_for(actor):
        query = query.filter(department_id__in=resolve_department_ids(actor,problem.company))
    return query.order_by('normalized_employee_no')


class ProblemAssignmentForm(forms.Form):
    owner_employee = forms.ModelChoiceField(label='跟进负责人',queryset=Employee.objects.none())
    target_date = forms.DateField(label='目标完成日期',widget=forms.DateInput(attrs={'type':'date'}))
    reason = forms.CharField(label='分派或调整说明',max_length=2000,widget=forms.Textarea(attrs={'rows':3}))
    idempotency_key = forms.CharField(widget=forms.HiddenInput)
    expected_assignment = forms.CharField(widget=forms.HiddenInput)

    def __init__(self,*args,actor,problem,**kwargs):
        import uuid
        require_close_maintenance_problem(actor,problem)
        self.problem = problem
        super().__init__(*args,**kwargs)
        self.fields['owner_employee'].queryset = assignment_owners(actor,problem)
        self.initial.update(owner_employee=problem.owner_employee_id,target_date=problem.target_date,
            idempotency_key=uuid.uuid4().hex,expected_assignment=assignment_version(problem))
        _style(self)

    def clean_target_date(self):
        value = self.cleaned_data['target_date']
        if value < self.problem.maintenance_record.completed_date:
            raise forms.ValidationError('目标完成日期不得早于发现问题的保养日期。')
        return value


@transaction.atomic
def assign_problem(*,actor,problem,owner_employee,target_date,reason,idempotency_key,expected_assignment,request=None):
    plan = _lock_plan(problem.maintenance_record.maintenance_plan_id)
    problem = MaintenanceProblem.objects.select_for_update(of=('self',)).select_related('maintenance_record','owner_employee','asset__department').get(pk=problem.pk,company=plan.company)
    require_close_maintenance_problem(actor,problem)
    owner = assignment_owners(actor,problem).select_for_update(of=('self',)).filter(pk=getattr(owner_employee,'pk',None)).first()
    if owner is None:
        raise PermissionDenied('负责人必须是本次可分派范围内的在职启用人员。')
    reason = _required(reason,'reason','请填写分派或调整说明。')
    if target_date is None:
        raise ValidationError({'target_date':'请填写目标完成日期。'})
    target_date = business_date(target_date)
    if target_date < problem.maintenance_record.completed_date:
        raise ValidationError({'target_date':'目标完成日期不得早于保养日期。'})
    payload = {'problem_id':problem.pk,'owner_employee_id':owner.pk,'target_date':target_date,'reason':reason,
               'expected_assignment':expected_assignment}
    key,digest,existing = _check_operation_idempotency(company=plan.company,operation='assign_problem',key=idempotency_key,payload=payload,model=MaintenanceProblem)
    if existing is not None:
        return existing
    if problem.status != 'open' or problem.maintenance_record.status != 'confirmed':
        raise ValidationError('已关闭或来源已作废的问题不能重新分派。')
    if expected_assignment != assignment_version(problem):
        raise ValidationError('负责人或期限已被更新，请刷新核对后再提交。')
    old = {'owner_employee_id':problem.owner_employee_id,'owner_name':problem.owner_employee.name if problem.owner_employee else '',
           'target_date':problem.target_date.isoformat() if problem.target_date else None}
    _base_update(MaintenanceProblem,problem.pk,{'owner_employee_id':owner.pk,'target_date':target_date},'controlled_maintenance_problem_assignment')
    problem.refresh_from_db()
    _audit(actor=actor,action='maintenance.problem_assigned',instance=problem,old=old,
        new={'owner_employee_id':owner.pk,'owner_name':owner.name,'target_date':target_date.isoformat(),'reason':reason},request=request)
    _write_operation_marker(actor=actor,operation='assign_problem',result=problem,key=key,digest=digest,payload=payload,request=request)
    return problem
