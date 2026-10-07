"""Repair completion preserves entered notes and replay semantics."""
from html import escape
from html.parser import HTMLParser
import hashlib
import json

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assets.lifecycle_services import complete_asset_repair, send_asset_for_repair
from apps.assets.models import AssetMovement
from apps.audit.models import AuditLog
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db


class _Controls(HTMLParser):
    def __init__(self, response):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.current = None
        self.feed(response.content.decode('utf-8'))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            assert self.current is None
            self.current = {'names': set(), 'values': {}}
        elif self.current is not None and tag in {'input', 'textarea', 'select'} and attrs.get('name'):
            name = attrs['name']
            self.current['names'].add(name)
            if tag == 'input':
                assert name not in self.current['values']
                self.current['values'][name] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form' and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def _repair(prefix):
    context, asset, _qr = active_asset_context(prefix)
    start = send_asset_for_repair(
        actor=context['equipment'], asset=asset, effective_at=timezone.now(),
        reason='建立真实送修前置状态', expected_status='in_use',
        remark='原送修情况不能丢失', idempotency_key=prefix + '-start',
    )
    asset.refresh_from_db()
    assert asset.asset_status == 'under_repair'
    return context, asset, start


def _browser(context, asset, *, note):
    client = Client(enforce_csrf_checks=True)
    client.force_login(context['equipment'])
    url = reverse('assets:lifecycle-repair-complete', args=[asset.pk])
    page = client.get(url)
    assert page.status_code == 200
    forms = [f for f in _Controls(page).forms if {'reason', 'remark'} <= f['names']]
    assert len(forms) == 1
    payload = dict(forms[0]['values'])
    assert payload['csrfmiddlewaretoken'] and payload['idempotency_key']
    assert payload['expected_status'] == 'under_repair' and payload['effective_at']
    payload.update(reason='维修完成且试运行正常', remark=note)
    return client, url, payload


def _marker(context, movement):
    return AuditLog.objects.get(
        company=context['company'], action='asset_lifecycle.idempotency.repair_complete',
        object_id=str(movement.pk),
    )


@override_settings(ALLOWED_HOSTS=['testserver'])
def test_real_repair_completion_form_preserves_multiline_2000_character_note_and_start_trace():
    context, asset, start = _repair('REPAIRNOTEHTTP')
    beginning = '更换接线端子并复测\n'
    note = beginning + '验' * (2000 - len(beginning))
    assert len(note) == 2000
    client, url, payload = _browser(context, asset, note=note)
    response = client.post(url, payload, HTTP_ORIGIN='http://testserver')
    assert response.status_code == 302
    completed = AssetMovement.objects.get(asset=asset, movement_type='repair_complete')
    trace = f'对应送修变动：{start.pk}'
    assert completed.remark == trace + '\n' + note
    assert completed.reason == payload['reason']
    assert completed.from_status == 'under_repair' and completed.to_status == 'in_use'
    start.refresh_from_db()
    assert start.remark == '原送修情况不能丢失'
    marker = _marker(context, completed)
    assert marker.new_data_json['payload']['remark'] == completed.remark
    assert marker.new_data_json['idempotency_key'] == payload['idempotency_key']
    assert AuditLog.objects.filter(company=context['company'], action='asset_lifecycle.repair_complete').count() == 1
    assert AuditLog.objects.filter(company=context['company'], action='asset_lifecycle.idempotency.repair_complete').count() == 1
    asset.refresh_from_db()
    assert asset.asset_status == 'in_use'
    before = (AssetMovement.objects.count(), AuditLog.objects.count())
    assert client.post(url, payload, HTTP_ORIGIN='http://testserver').status_code == 302
    assert (AssetMovement.objects.count(), AuditLog.objects.count()) == before
    completed.refresh_from_db()
    assert completed.remark == trace + '\n' + note


def test_trusted_default_none_and_blank_note_preserve_legacy_payload_hash_and_replay_without_writes():
    context, asset, start = _repair('REPAIRNOTELEGACY')
    effective_at = timezone.now()
    values = dict(actor=context['equipment'], asset=asset, effective_at=effective_at,
                  result='维修完成且试运行正常', idempotency_key='REPAIRNOTELEGACY-complete')
    completed = complete_asset_repair(**values)
    trace = f'对应送修变动：{start.pk}'
    assert completed.remark == trace
    expected_payload = {'asset_id': str(asset.pk), 'from_status': 'under_repair',
                        'to_status': 'in_use', 'movement_type': 'repair_complete',
                        'effective_at': effective_at.isoformat(), 'reason': values['result'], 'remark': trace}
    expected_hash = hashlib.sha256(json.dumps(expected_payload, ensure_ascii=False,
                                            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    marker = _marker(context, completed)
    assert marker.new_data_json['payload'] == expected_payload
    assert marker.new_data_json['request_hash'] == expected_hash
    before = (AssetMovement.objects.count(), AuditLog.objects.count())
    for optional in ({}, {'remark': None}, {'remark': ''}, {'remark': ' \t\n '}):
        replay = complete_asset_repair(**values, **optional)
        assert replay.pk == completed.pk
        assert replay.remark == trace
        assert (AssetMovement.objects.count(), AuditLog.objects.count()) == before
    marker.refresh_from_db()
    assert marker.new_data_json['payload'] == expected_payload and marker.new_data_json['request_hash'] == expected_hash
    asset.refresh_from_db()
    assert asset.asset_status == 'in_use'


@override_settings(ALLOWED_HOSTS=['testserver'])
def test_same_key_changed_note_is_rejected_and_rendered_without_extra_movements_or_audits():
    context, asset, start = _repair('REPAIRNOTECONFLICT')
    note = '首次提交的检查情况\n试运行正常'
    client, url, payload = _browser(context, asset, note=note)
    assert client.post(url, payload, HTTP_ORIGIN='http://testserver').status_code == 302
    completed = AssetMovement.objects.get(asset=asset, movement_type='repair_complete')
    before = (AssetMovement.objects.count(), AuditLog.objects.count(), _marker(context, completed).new_data_json.copy())
    changed_note = '后来修改的备注\n这次提交不能覆盖已保存内容'
    changed = dict(payload, remark=changed_note)
    response = client.post(url, changed, HTTP_ORIGIN='http://testserver')
    assert response.status_code == 200
    form = response.context['form']
    assert form.is_bound
    assert '相同幂等键已用于不同请求参数。' in form.non_field_errors()
    assert form['remark'].value() == changed_note and form['reason'].value() == payload['reason']
    assert form['idempotency_key'].value() == payload['idempotency_key']
    assert escape(changed_note) in response.content.decode('utf-8')
    assert 'data-unsaved-guard="true"' in response.content.decode('utf-8')
    completed.refresh_from_db()
    assert completed.remark == f'对应送修变动：{start.pk}\n{note}'
    assert (AssetMovement.objects.count(), AuditLog.objects.count(), _marker(context, completed).new_data_json) == before
    asset.refresh_from_db()
    assert asset.asset_status == 'in_use'
