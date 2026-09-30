from .models import MaintenanceProblem
from .permissions import scoped_maintenance_plans, can_manage_maintenance_plan, can_close_maintenance_problem


def employee_maintenance_handover(actor, employee):
    plans = scoped_maintenance_plans(actor, employee.company)
    assigned = plans.filter(responsible_employee=employee, status__in=('active','suspended')).select_related('asset')
    problems = MaintenanceProblem.objects.filter(company=employee.company, owner_employee=employee,
        status='open', maintenance_record__status='confirmed',
        maintenance_record__maintenance_plan__in=plans).select_related('asset__department','maintenance_record')
    return {
        'plans':[{'plan':p, 'can_edit':can_manage_maintenance_plan(actor,p)} for p in assigned],
        'problems':[{'problem':p, 'can_assign':can_close_maintenance_problem(actor,p)} for p in problems],
    }
