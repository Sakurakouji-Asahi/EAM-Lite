from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.maintenance.assignment import assign_problem, assignment_version
from apps.maintenance.models import MaintenanceProblem
from apps.maintenance.services import close_maintenance_problem
from tests.test_sprint9_services import _complete
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db(transaction=True)


def test_assignment_retries_stale_updates_deadline_filter_and_existing_close_permissions(client):
    ctx = maintenance_context('ASSIGNWORK')
    record = _complete(ctx,'assign-complete',result='problem_found',problem_description='需要检查防护罩',
                       completed_date=timezone.localdate()-timedelta(days=2))
    problem = record.problem
    client.force_login(ctx['equipment'])
    url = reverse('maintenance:problem-assign',args=[problem.pk])
    page = client.get(url)
    data = {'owner_employee':ctx['responsible'].pk,'target_date':(timezone.localdate()-timedelta(days=1)).isoformat(),
            'reason':'安排复查','idempotency_key':page.context['form']['idempotency_key'].value(),
            'expected_assignment':page.context['form']['expected_assignment'].value()}
    assert client.post(url,data).status_code == 302
    assert client.post(url,data).status_code == 302
    assert AuditLog.objects.filter(action='maintenance.problem_assigned').count() == 1
    stale = client.post(url,{**data,'idempotency_key':'assignment-stale','reason':'不同调整'})
    assert stale.status_code == 200 and '已被更新' in stale.content.decode()
    listing = client.get(reverse('maintenance:problem-list'),{'due_scope':'overdue'})
    assert [row['problem'].pk for row in listing.context['items']] == [problem.pk]
    assert '已逾期' in listing.content.decode()
    detail = client.get(reverse('maintenance:record-detail',args=[record.pk]))
    assert '安排复查' in detail.content.decode() and '分派与期限调整历史' in detail.content.decode()
    client.force_login(ctx['responsible_user'])
    mine = client.get(reverse('maintenance:problem-list'),{'mine':'on'})
    assert len(mine.context['items']) == 1
    assert client.get(url).status_code == 403
    assert client.get(reverse('maintenance:problem-close',args=[problem.pk])).status_code == 403
    close_maintenance_problem(actor=ctx['equipment'],problem=problem,closure_note='复查通过',idempotency_key='assigned-close')
    problem.refresh_from_db()
    assert problem.owner_employee_id == ctx['responsible'].pk and problem.status == 'closed'


def test_assignment_migration_round_trip_preserves_assigned_records():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL guard verification')
    ctx = maintenance_context('ASSIGNMIG')
    record = _complete(ctx,'assign-migrate-record',result='problem_found',problem_description='复查')
    problem = record.problem
    assign_problem(actor=ctx['equipment'],problem=problem,owner_employee=ctx['responsible'],
        target_date=timezone.localdate()+timedelta(days=1),reason='安排期限',idempotency_key='assign-migrate',
        expected_assignment=assignment_version(problem))
    original = MaintenanceProblem.objects.filter(pk=problem.pk).values().get()
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([('maintenance','0001_initial')])
        assert MaintenanceProblem.objects.filter(pk=problem.pk).values().get() == original
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert MaintenanceProblem.objects.filter(pk=problem.pk).values().get() == original
