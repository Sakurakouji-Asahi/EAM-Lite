from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.assets.models import Asset, AttachmentLink
from apps.maintenance.models import MaintenancePlan, MaintenanceProblem, MaintenanceRecord
from apps.maintenance.services import complete_maintenance, set_maintenance_plan_status
from tests.test_sprint3_support import make_employee
from tests.test_sprint9_support import maintenance_context
from tests.test_sprint10_support import formal_asset

pytestmark=pytest.mark.django_db


def _source_state(source):
    return {
        'plan':MaintenancePlan._base_manager.filter(pk=source.pk).values().get(),
        'records':list(MaintenanceRecord._base_manager.filter(maintenance_plan=source).order_by('pk').values()),
        'problems':list(MaintenanceProblem._base_manager.filter(maintenance_record__maintenance_plan=source).order_by('pk').values()),
        'asset':Asset._base_manager.filter(pk=source.asset_id).values().get(),
        'attachments':list(AttachmentLink._base_manager.filter(maintenance_record__maintenance_plan=source).order_by('pk').values()),
    }


def _payload(ctx, source):
    return {'reference_plan':str(source.pk),'asset':str(ctx['asset'].pk),'name':'DEMO 新保养计划',
            'cycle_value':source.cycle_value,'cycle_unit':source.cycle_unit,'advance_notice_days':source.advance_notice_days,
            'responsible_employee':ctx['equipment_employee'].pk,'standard_content':'核对修改后的独立标准',
            'first_due_date':(timezone.localdate()+timedelta(days=9)).isoformat()}


def test_reference_get_prefills_only_reusable_fields_without_writes(client):
    ctx=maintenance_context('REFGET')
    source=set_maintenance_plan_status(actor=ctx['equipment'],plan=ctx['plan'],status='suspended',reason='参考暂停计划标准')
    client.force_login(ctx['equipment'])
    baseline=_source_state(source)
    counts=(MaintenancePlan.objects.count(),AuditLog.objects.count())
    return_to=reverse('maintenance:plan-list')+'?'+urlencode({'q':'REFGET','status':'suspended','page':2})
    page=client.get(reverse('maintenance:plan-create'),{'reference_plan':source.pk,'return_to':return_to})
    assert page.status_code==200
    form=page.context['form']
    for name in ('name','cycle_value','cycle_unit','advance_notice_days','standard_content'):
        assert form[name].value()==getattr(source,name)
    for name in ('asset','responsible_employee','first_due_date'):
        assert form[name].value() is None
    assert 'expected_revision' not in form.fields
    assert page.context['reference_source_url']==reverse('maintenance:plan-detail',args=[source.pk])+'?'+urlencode({'return_to':return_to})
    assert page.context['plan_return_to']==return_to
    assert 'name="reference_plan"' in page.content.decode()
    assert _source_state(source)==baseline
    assert (MaintenancePlan.objects.count(),AuditLog.objects.count())==counts
    detail=client.get(reverse('maintenance:plan-detail',args=[source.pk]),{'return_to':return_to})
    assert '参考计划新建' in detail.content.decode()


def test_reference_save_creates_independent_plan_and_keeps_source_history(client):
    ctx=maintenance_context('REFSAVE')
    source=ctx['plan']
    complete_maintenance(actor=ctx['equipment'],plan=source,scheduled_date=source.next_maintenance_date,
                         completed_date=timezone.localdate(),actual_content='来源已保存的现场记录',result='problem_found',
                         problem_description='来源的独立问题',idempotency_key='REFSAVE-complete')
    source.refresh_from_db()
    source=set_maintenance_plan_status(actor=ctx['equipment'],plan=source,status='ended',reason='保留历史标准作参考')
    asset,_=formal_asset(ctx,'REFSAVE-TARGET',employee=ctx['equipment_employee'])
    asset.is_maintenance_required=True
    asset.save(update_fields={'is_maintenance_required'})
    client.force_login(ctx['equipment'])
    baseline=_source_state(source)
    asset_values=Asset._base_manager.filter(pk=asset.pk).values().get()
    data=_payload(ctx,source)
    data.update(asset=str(asset.pk),standard_content=source.standard_content)
    response=client.post(reverse('maintenance:plan-create'),data)
    assert response.status_code==302
    created=MaintenancePlan.objects.get(name=data['name'])
    assert created.pk!=source.pk and created.asset_id==asset.pk
    assert created.responsible_employee_id==ctx['equipment_employee'].pk
    assert created.first_due_date.isoformat()==data['first_due_date']
    assert created.next_maintenance_date==created.first_due_date and created.last_maintenance_date is None
    assert created.status=='active' and created.ended_reason is None
    assert not created.records.exists()
    assert not MaintenanceProblem.objects.filter(maintenance_record__maintenance_plan=created).exists()
    assert not AttachmentLink._base_manager.filter(maintenance_record__maintenance_plan=created).exists()
    assert _source_state(source)==baseline
    assert Asset._base_manager.filter(pk=asset.pk).values().get()==asset_values


def test_reference_error_keeps_edited_input_and_original_choices(client):
    ctx=maintenance_context('REFBOUND')
    inactive=make_employee(ctx['company'],ctx['department'],'REFBOUND-OFF',active=False)
    source=ctx['plan']
    client.force_login(ctx['equipment'])
    baseline=_source_state(source)
    data=_payload(ctx,source)
    data.update(cycle_value=0,responsible_employee=inactive.pk,first_due_date='invalid-date')
    response=client.post(reverse('maintenance:plan-create'),data)
    assert response.status_code==200
    form=response.context['form']
    assert set(form.errors)=={'cycle_value','responsible_employee','first_due_date'}
    assert form['name'].value()==data['name'] and form['standard_content'].value()==data['standard_content']
    assert response.context['reference_source'].pk==source.pk
    assert inactive.pk not in form.fields['responsible_employee'].queryset.values_list('pk',flat=True)
    assert _source_state(source)==baseline
    assert MaintenancePlan.objects.count()==1


def test_reference_requires_creation_permission_and_current_company_source_on_post(client):
    ctx=maintenance_context('REFSCOPE')
    source=ctx['plan']
    create=reverse('maintenance:plan-create')
    client.force_login(ctx['finance'])
    detail=client.get(reverse('maintenance:plan-detail',args=[source.pk]))
    assert detail.status_code==200 and '参考计划新建' not in detail.content.decode()
    assert client.get(create,{'reference_plan':source.pk}).status_code==403
    assert client.post(create,_payload(ctx,source)).status_code==403
    client.force_login(ctx['equipment'])
    assert client.get(create,{'reference_plan':'bad-uuid'}).status_code==404
    assert client.post(create,{**_payload(ctx,source),'reference_plan':str(uuid4())}).status_code==404
    # Build another actual company through existing factories, then restore the original current company.
    ctx['company'].is_active=False
    ctx['company'].save(update_fields={'is_active'})
    foreign=maintenance_context('REFFOREIGN')['plan']
    foreign.company.is_active=False
    foreign.company.save(update_fields={'is_active'})
    ctx['company'].is_active=True
    ctx['company'].save(update_fields={'is_active'})
    baseline=_source_state(source)
    assert client.get(create,{'reference_plan':foreign.pk}).status_code==404
    assert client.post(create,{**_payload(ctx,source),'reference_plan':str(foreign.pk)}).status_code==404
    assert _source_state(source)==baseline
