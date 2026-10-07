"""Failed permission writes preserve safe input without weakening service guards."""
from urllib.parse import urlencode
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.audit.models import AuditLog
from apps.masterdata.models import UserDepartmentScope
from apps.masterdata.services import assign_department_scope
from tests.test_sprint3_support import PASSWORD, make_company, make_department, make_user

pytestmark=pytest.mark.django_db


def _context(client, prefix):
    actor=make_user(prefix+'-admin','system_admin')
    target=make_user(prefix+'-target','employee')
    company=make_company(prefix)
    department=make_department(company,prefix+'-D')
    client.force_login(actor)
    origin=reverse('masterdata:user-permissions-list')+'?'+urlencode({'q':prefix,'role':'employee','active':'yes','page':2})
    return actor,target,company,department,origin


def _state(target):
    return (list(target.groups.order_by('pk').values_list('name',flat=True)),
            list(UserDepartmentScope._base_manager.filter(user=target).order_by('pk').values()),
            AuditLog.objects.count())


def test_role_field_failure_preserves_selection_and_safe_return(client):
    actor,target,company,department,origin=_context(client,'PERM-FIELD')
    baseline=_state(target)
    secret='never-render-this-password'
    response=client.post(reverse('masterdata:user-roles-update',args=[target.pk]),{
        'roles':['equipment','unknown-role'], 'reason':'本次角色说明', 'current_password':secret,'return_to':origin,
    })
    assert response.status_code==200
    form=response.context['role_form']
    assert form['roles'].value()==['equipment','unknown-role']
    assert form['reason'].value()=='本次角色说明'
    assert form.errors['roles']
    assert response.context['return_url']==origin
    assert response.context['assigned_roles']==['员工']
    assert not response.context['scope_form'].is_bound
    html=response.content.decode()
    assert secret not in html and 'value="never-render' not in html
    assert '角色变更未保存' in html and '当前已保存权限' in html
    assert 'href="#permission-roles-roles"' in html
    assert _state(target)==baseline
    listing=client.get(origin)
    assert urlencode({'return_to':origin}).replace('&','&amp;') in listing.content.decode()


def test_role_service_errors_preserve_choices_password_protection_and_last_admin(client):
    actor,target,company,department,origin=_context(client,'PERM-GUARD')
    baseline=_state(target)
    rejected=client.post(reverse('masterdata:user-roles-update',args=[target.pk]),{
        'roles':['employee','finance'],'reason':'核对财务角色','current_password':'wrong-password-only','return_to':origin,
    })
    assert rejected.status_code==200
    assert '当前密码验证失败' in str(rejected.context['role_form'].non_field_errors())
    assert rejected.context['role_form']['roles'].value()==['employee','finance']
    assert rejected.context['role_form']['reason'].value()=='核对财务角色'
    assert 'wrong-password-only' not in rejected.content.decode()
    assert not rejected.context['finance_fields_visible']
    assert _state(target)==baseline
    protected=_state(actor)
    last=client.post(reverse('masterdata:user-roles-update',args=[actor.pk]),{
        'roles':['employee'],'reason':'测试最后管理员保护','current_password':PASSWORD,'return_to':origin,
    })
    assert last.status_code==200
    assert '不能移除最后一名可登录' in str(last.context['role_form'].non_field_errors())
    assert last.context['role_form']['roles'].value()==['employee']
    assert PASSWORD not in last.content.decode()
    assert _state(actor)==protected


def test_scope_assign_and_revoke_failures_keep_only_the_submitted_form(client):
    actor,target,company,department,origin=_context(client,'PERM-SCOPE')
    scope=assign_department_scope(actor=actor,company=company,user=target,department=department,reason='测试原授权')
    baseline=_state(target)
    assign_url=reverse('masterdata:user-scope-assign',args=[target.pk])
    missing=client.post(assign_url,{'department':department.pk,'return_to':origin})
    assert missing.status_code==200
    assert missing.context['scope_form']['department'].value()==str(department.pk)
    assert missing.context['scope_form']['include_descendants'].value() is False
    assert missing.context['scope_form'].errors['reason']
    assert 'href="#permission-scope-reason"' in missing.content.decode()
    assert not missing.context['role_form'].is_bound
    duplicate=client.post(assign_url,{'department':department.pk,'reason':'重复范围说明保留','return_to':origin})
    assert duplicate.status_code==200
    assert '已有此部门的活动授权范围' in str(duplicate.context['scope_form'].non_field_errors())
    assert duplicate.context['scope_form']['reason'].value()=='重复范围说明保留'
    revoke=client.post(reverse('masterdata:user-scope-revoke',args=[target.pk,scope.pk]),{
        'reason':'撤销说明'*130,'return_to':origin,
    })
    assert revoke.status_code==200
    row=revoke.context['scope_rows'][0]
    assert row['form']['reason'].value()=='撤销说明'*130
    assert row['form'].errors['reason']
    assert row['form']['reason'].id_for_label==f'permission-revoke-{scope.pk}-reason'
    assert revoke.context['return_url']==origin
    assert not revoke.context['role_form'].is_bound and not revoke.context['scope_form'].is_bound
    assert _state(target)==baseline


def test_permission_boundaries_and_success_return_remain_controlled(client):
    actor,target,company,department,origin=_context(client,'PERM-BOUND')
    roles_url=reverse('masterdata:user-roles-update',args=[target.pk])
    detail_url=reverse('masterdata:user-permissions-detail',args=[target.pk])
    for unsafe in ['https://example.invalid/','//example.invalid/','/assets/','/masterdata/users/../roles/']:
        response=client.get(detail_url,{'return_to':unsafe})
        assert response.context['return_url']==reverse('masterdata:user-permissions-list')
    hr=make_user('perm-bound-hr','hr')
    client.force_login(hr)
    baseline=_state(target)
    assert client.post(roles_url,{'roles':['finance'],'reason':'不得授权'}).status_code==403
    assert client.post(reverse('masterdata:user-scope-assign',args=[target.pk]),{'department':department.pk,'reason':'不得授权'}).status_code==403
    assert _state(target)==baseline
    client.force_login(actor)
    recovery=get_user_model().objects.create_superuser(username='perm-recovery',password=PASSWORD)
    assert client.get(reverse('masterdata:user-permissions-detail',args=[recovery.pk])).status_code==404
    success=client.post(roles_url,{'roles':['equipment'],'reason':'独立测试对象受控更新','return_to':origin})
    assert success.status_code==302
    assert success['Location']==detail_url+'?'+urlencode({'return_to':origin})
    assert set(target.groups.values_list('name',flat=True))=={'equipment'}
