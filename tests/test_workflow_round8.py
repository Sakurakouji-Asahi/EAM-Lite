"""Constructed business examples for workflow feedback, roundtrips and scope."""
import io
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.audit.models import AuditLog
from apps.reports.models import ReportPreset, ExportLog
from apps.supplies.count_transfer import count_workbook, read_count_rows, import_count_rows
from apps.supplies.posting_preview import document_posting_preview, preview_token, post_with_preview, posting_blockers, posting_comparison
from apps.supplies.services import create_supply_document, post_supply_document, publish_supply_count_task, record_supply_count
from tests.test_sprint15_services import supply_context
from tests.test_sprint15_support import seed_supply_stock, make_issue_document
from tests.test_sprint17_services import make_count
from tests.test_sprint3_support import make_user, JPEG_BYTES
from tests.test_sprint7_support import active_asset_context
from tests.test_sprint9_support import maintenance_context
from tests.test_sprint9_services import _complete

pytestmark = pytest.mark.django_db(transaction=True)


def test_post_preview_compares_locked_actual_cost_and_keeps_one_audit_on_retry(client):
    company, actor, department, employee, warehouse, _, item, _ = supply_context()
    seed_supply_stock(actor=actor,company=company,warehouse=warehouse,item=item,quantity='5',unit_cost='10')
    issue = make_issue_document(actor=actor,company=company,warehouse=warehouse,item=item,department=department,quantity='2')
    preview = document_posting_preview(actor=actor,document=issue)
    assert preview['total_amount'] == Decimal('20.00')
    token = preview_token(actor,issue,preview)
    receipt = create_supply_document(actor=actor,company=company,document_type='receipt',
        data={'business_date':date(2026,8,26),'target_warehouse':warehouse,'idempotency_key':'new-receipt'},
        lines=[{'item':item,'quantity':Decimal('5'),'entered_unit_cost':Decimal('30')}])
    post_supply_document(actor=actor,document=receipt)
    # 5*10 + 5*30 = 200; 200/10*2 = 40, compared with the earlier estimate 20.
    post_with_preview(actor=actor,document=issue,token=token)
    post_with_preview(actor=actor,document=issue,token=token)
    comparison = posting_comparison(actor,issue)
    assert comparison == {'before':'20.00','rows':{str(issue.lines.get().pk):'40.00'},'total':'40.00','difference':'20.00','changed':True}
    assert AuditLog.objects.filter(action='supply_posting_comparison').count() == 1
    client.force_login(actor)
    detail = client.get(reverse('supplies:document-detail',args=[issue.pk]))
    assert detail.status_code == 200 and '差额 ¥20.00' in detail.content.decode()


def test_post_preview_surfaces_stock_count_freeze_before_submit(client):
    company, actor, department, employee, warehouse, _, item, _ = supply_context()
    seed_supply_stock(actor=actor,company=company,warehouse=warehouse,item=item,quantity='5',unit_cost='10')
    issue = make_issue_document(actor=actor,company=company,warehouse=warehouse,item=item,department=department,quantity='2')
    task = make_count(actor=actor,company=company,domain='warehouse_stock',warehouse=warehouse,key='frozen-preview')
    publish_supply_count_task(actor=actor,task=task)
    assert any('正在盘点' in error for error in posting_blockers(issue))
    client.force_login(actor)
    page = client.get(reverse('supplies:document-post',args=[issue.pk]))
    assert page.status_code == 200 and ' disabled' in page.content.decode()
    with pytest.raises(ValidationError):
        post_with_preview(actor=actor,document=issue)
    issue.refresh_from_db()
    assert issue.status == 'draft'


def test_post_error_redisplay_refreshes_snapshot_to_match_displayed_estimate(client):
    from django.core import signing
    company,actor,department,employee,warehouse,_,item,_ = supply_context()
    seed_supply_stock(actor=actor,company=company,warehouse=warehouse,item=item,quantity='5',unit_cost='10')
    issue = make_issue_document(actor=actor,company=company,warehouse=warehouse,item=item,department=department,quantity='2')
    token = preview_token(actor,issue,document_posting_preview(actor=actor,document=issue))
    receipt = create_supply_document(actor=actor,company=company,document_type='receipt',
        data={'business_date':date(2026,8,26),'target_warehouse':warehouse,'idempotency_key':'error-receipt'},
        lines=[{'item':item,'quantity':Decimal('5'),'entered_unit_cost':Decimal('30')}])
    post_supply_document(actor=actor,document=receipt)
    client.force_login(actor)
    response = client.post(reverse('supplies:document-post',args=[issue.pk]),{'preview_token':token,'idempotency_key':issue.idempotency_key})
    assert response.status_code == 200 and response.context['form'].errors
    new_token = response.context['form']['preview_token'].value()
    assert signing.loads(new_token,salt='supply-posting-preview')['total'] == '40.00'
    assert response.context['posting_preview']['total_amount'] == Decimal('40.00')
    issue.refresh_from_db();assert issue.status == 'draft'


def test_posting_preview_rejects_modified_draft_but_not_market_cost_changes():
    from apps.supplies.services import update_draft_document
    company,actor,department,employee,warehouse,_,item,_ = supply_context()
    seed_supply_stock(actor=actor,company=company,warehouse=warehouse,item=item,quantity='5',unit_cost='10')
    issue = make_issue_document(actor=actor,company=company,warehouse=warehouse,item=item,department=department,quantity='2')
    token = preview_token(actor,issue,document_posting_preview(actor=actor,document=issue))
    update_draft_document(actor=actor,document=issue,data={},lines=[{'item':item,'quantity':Decimal('3'),'entered_unit_cost':None}])
    with pytest.raises(ValidationError,match='草稿内容已被修改'):
        post_with_preview(actor=actor,document=issue,token=token)
    issue.refresh_from_db()
    assert issue.status == 'draft'


def _count_sheet_context():
    company, actor, department, employee, warehouse, _, first, second = supply_context()
    for index,item in enumerate((first,second)):
        seed_supply_stock(actor=actor,company=company,warehouse=warehouse,item=item,quantity='5',unit_cost='10',key=f'sheet-{index}')
    task = make_count(actor=actor,company=company,domain='warehouse_stock',warehouse=warehouse,key='sheet-task')
    publish_supply_count_task(actor=actor,task=task)
    workbook = count_workbook(actor,task)
    rows = read_count_rows({'file':SimpleUploadedFile('count.xlsx',workbook)})
    return actor, task, rows, workbook


def test_count_sheet_roundtrip_zero_blank_retry_and_whole_batch_rollback():
    actor,task,rows,_ = _count_sheet_context()
    rows[0][5], rows[0][6] = '0','现场已无实物'
    assert import_count_rows(actor=actor,task=task,rows=rows) == 1
    assert sorted(task.lines.values_list('counted_quantity',flat=True),key=lambda v:v is None) == [Decimal('0'),None]
    assert import_count_rows(actor=actor,task=task,rows=rows) == 0
    fresh = read_count_rows({'file':SimpleUploadedFile('count.xlsx',count_workbook(actor,task))})
    second = task.lines.get(item_code_snapshot=rows[1][1])
    record_supply_count(actor=actor,line=second,counted_quantity=Decimal('5'),remark='')
    fresh[0][5],fresh[0][6] = '2','再次核对'
    fresh[1][5],fresh[1][6] = '3','发现盘亏'
    with pytest.raises(ValidationError,match='已被更新'):
        import_count_rows(actor=actor,task=task,rows=fresh)
    assert task.lines.get(item_code_snapshot=rows[0][1]).counted_quantity == Decimal('0')


def test_count_sheet_rejects_duplicate_tampering_formula_and_wrong_actor():
    actor,task,rows,content = _count_sheet_context()
    rows[0][5] = '5'
    with pytest.raises(ValidationError,match='重复'):
        import_count_rows(actor=actor,task=task,rows=[rows[0],rows[0]])
    with pytest.raises(ValidationError,match='校验码'):
        import_count_rows(actor=make_user('sheet-outsider','employee'),task=task,rows=rows)
    rows[0][3] = '假单位'
    with pytest.raises(ValidationError,match='单位被修改'):
        import_count_rows(actor=actor,task=task,rows=rows)
    workbook = load_workbook(io.BytesIO(content));workbook.active['F2'] = '=1+1'
    stream = io.BytesIO();workbook.save(stream)
    with pytest.raises(ValidationError):
        read_count_rows({'file':SimpleUploadedFile('count.xlsx',stream.getvalue())})
    assert task.lines.filter(counted_quantity__isnull=False).count() == 0


def test_count_sheet_download_paste_http_and_current_scope(client):
    actor,task,rows,_ = _count_sheet_context()
    from apps.supplies.count_transfer import HEADERS
    client.force_login(actor)
    url = reverse('supplies:count-sheet',args=[task.pk])
    assert client.get(url).status_code == 200
    assert client.get(url,{'download':'1'})['Content-Type'].startswith('application/vnd.openxmlformats')
    rows[0][5] = '5'
    pasted = '\n'.join('\t'.join(str(v or '') for v in row) for row in [HEADERS,*rows])
    assert client.post(url,{'pasted':pasted}).status_code == 302
    client.force_login(make_user('sheet-http-outsider','employee'))
    assert client.get(url,{'download':'1'}).status_code in (403,404)


def test_personal_presets_are_user_scoped_requery_and_recheck_current_roles(client):
    ctx,asset,_ = active_asset_context('PRESETS8')
    client.force_login(ctx['finance'])
    response = client.get(reverse('reports:report-center'),{'report_type':'fixed_asset_detail','q':asset.asset_code})
    assert response.status_code == 200
    token = response.context['preset_token']
    response = client.post(reverse('reports:preset-save'),{'name':'每月复核','preset_token':token})
    assert response.status_code == 302
    preset = ReportPreset.objects.get()
    assert preset.query['q'] == asset.asset_code and 'page' not in preset.query
    assert client.post(reverse('reports:preset-save'),{'name':'每月复核','preset_token':token}).status_code == 302
    assert ReportPreset.objects.count() == 1
    apply = reverse('reports:preset-apply',args=[preset.pk])
    assert client.get(apply,follow=True).status_code == 200
    ctx['finance'].groups.clear()
    assert client.get(apply).status_code == 403
    client.force_login(ctx['equipment'])
    assert client.get(apply).status_code == 404
    assert client.post(reverse('reports:preset-delete',args=[preset.pk])).status_code == 404
    assert client.post(reverse('reports:preset-save'),{'name':'窃取','preset_token':token}).status_code == 400


def test_all_export_history_pages_without_finance_metadata_leak(client):
    ctx,asset,_ = active_asset_context('EXPORTLIST8')
    for index in range(32):
        ExportLog.objects.create(company=ctx['company'],export_type='asset_ledger',requested_by=ctx['equipment'],
            idempotency_key=f'all-history-{index}',request_hash='a'*64)
    ExportLog.objects.create(company=ctx['company'],export_type='monthly_depreciation',requested_by=ctx['finance'],
        idempotency_key='hidden-finance',request_hash='b'*64)
    client.force_login(ctx['equipment'])
    page = client.get(reverse('reports:export-history'),{'page':2})
    assert page.status_code == 200 and page.context['page_obj'].paginator.count == 32
    assert len(page.context['page_obj']) == 7
    assert '月度折旧' not in page.content.decode()
    assert client.get(reverse('reports:export-history'),{'date_from':'2026-02-30'}).status_code == 400
    client.force_login(ctx['finance'])
    assert client.get(reverse('reports:export-history')).context['page_obj'].paginator.count == 33


def test_maintenance_date_preview_and_handover_do_not_mutate_or_expand_rights(client):
    from apps.maintenance.assignment import assign_problem, assignment_version
    from apps.maintenance.handover import employee_maintenance_handover
    from apps.maintenance.services import update_maintenance_plan
    ctx = maintenance_context('HANDOVER8')
    original = ctx['plan']
    ctx['plan'] = update_maintenance_plan(actor=ctx['equipment'],plan=original,name=original.name,
        cycle_value=1,cycle_unit='month',responsible_employee=ctx['responsible'],advance_notice_days=3,
        standard_content=original.standard_content,first_due_date=date(2026,1,31))
    record = _complete(ctx,'handover8-record',completed_date=date(2026,1,31),result='problem_found',problem_description='待复查')
    assign_problem(actor=ctx['equipment'],problem=record.problem,owner_employee=ctx['responsible'],target_date=date(2026,9,29),
        reason='安排接手',idempotency_key='handover8-owner',expected_assignment=assignment_version(record.problem))
    handover = employee_maintenance_handover(ctx['equipment'],ctx['responsible'])
    assert len(handover['plans']) == len(handover['problems']) == 1
    assert not employee_maintenance_handover(make_user('handover-hr','hr'),ctx['responsible'])['plans']
    client.force_login(ctx['equipment'])
    plan = ctx['plan'];plan.refresh_from_db()
    assert plan.next_maintenance_date == date(2026,2,28)
    url = reverse('maintenance:plan-edit',args=[plan.pk])
    data = {'asset':plan.asset_id,'name':plan.name,'cycle_value':2,'cycle_unit':'month',
        'responsible_employee':ctx['responsible'].pk,'advance_notice_days':3,'standard_content':plan.standard_content,
        'first_due_date':plan.first_due_date.isoformat(),'action':'preview'}
    response = client.post(url,data)
    assert response.status_code == 200 and response.context['date_preview']['next_date'] == date(2026,3,31)
    plan.refresh_from_db();assert plan.next_maintenance_date == date(2026,2,28)
    del data['action']
    assert client.post(url,data).status_code == 302
    plan.refresh_from_db();assert plan.next_maintenance_date == date(2026,3,31)


def test_multi_evidence_upload_rolls_back_files_and_links_on_one_invalid_file(client,settings,tmp_path):
    from apps.assets.models import AttachmentLink
    settings.MEDIA_ROOT = tmp_path
    ctx,asset,_ = active_asset_context('FILES8')
    client.force_login(ctx['equipment'])
    url = reverse('assets:attachment-upload',args=[asset.pk])
    before = AttachmentLink.objects.count()
    def jpeg(name):return SimpleUploadedFile(name,JPEG_BYTES,content_type='image/jpeg')
    response = client.post(url,{'role':'photo','security_class':'A0','file':[jpeg('one.jpg'),jpeg('two.jpg')]})
    assert response.status_code == 302
    assert AttachmentLink.objects.count() == before+2
    files = {p for p in tmp_path.rglob('*') if p.is_file()}
    invalid = SimpleUploadedFile('bad.jpg',b'invalid jpeg',content_type='image/jpeg')
    response = client.post(url,{'role':'photo','security_class':'A0','file':[jpeg('three.jpg'),invalid]})
    assert response.status_code == 200 and 'bad.jpg' in response.content.decode()
    assert AttachmentLink.objects.count() == before+2
    assert {p for p in tmp_path.rglob('*') if p.is_file()} == files


def test_source_links_reach_maintenance_records_and_supply_documents(client):
    ctx = maintenance_context('SOURCELINK8')
    record = _complete(ctx,'source-link-record')
    client.force_login(ctx['equipment'])
    page = client.get(reverse('reports:report-center'),{'report_type':'maintenance_records'})
    assert page.status_code == 200
    assert reverse('maintenance:record-detail',args=[record.pk]) in page.content.decode()
    assert reverse('maintenance:plan-detail',args=[ctx['plan'].pk]) in page.content.decode()


def test_inventory_evidence_accepts_all_selected_files(client):
    from apps.assets.models import AttachmentLink
    from apps.inventory.services import publish_inventory_task, scan_inventory_asset
    from tests.test_sprint8_services import _draft
    from tests.test_sprint8_support import inventory_context
    ctx,asset,qr = inventory_context('MULTICOUNT8')
    task = publish_inventory_task(actor=ctx['finance'],task=_draft(ctx,'MULTICOUNT8-T'))
    scan = scan_inventory_asset(actor=ctx['equipment'],task=task,qr_identity=qr,
        actual_location=asset.location,actual_employee=asset.responsible_employee,
        actual_status=asset.asset_status,idempotency_key='multi-count-scan')
    client.force_login(ctx['equipment'])
    response = client.post(reverse('inventory:attachment-upload',args=[task.pk,'scan',scan.pk]),{
        'uploaded_file':[SimpleUploadedFile(name,JPEG_BYTES,content_type='image/jpeg') for name in ('front.jpg','back.jpg')]})
    assert response.status_code == 302
    assert AttachmentLink.objects.filter(inventory_scan=scan).count() == 2


def test_safe_return_url_maintains_query_and_rejects_redirect_injection(client):
    from apps.core.return_navigation import safe_return_url
    ctx,asset,_ = active_asset_context('RETURN8')
    url = reverse('reports:external-reference-edit',args=[asset.pk])
    target = reverse('reports:external-reference-list')+'?q=RETURN8&reference_state=missing&page=2'
    client.force_login(ctx['finance'])
    assert client.get(url,{'return_to':target}).status_code == 200
    result = client.post(url+'?'+urlencode({'return_to':target}),{'reference_value':'CARD8','reason':'补充卡片','note':''})
    assert result.status_code == 302 and result.url == target
    for bad in ('https://example.com/','//example.com/','/\\example.com/','/logout/'):
        request = RequestFactory().get('/',{'return_to':bad})
        assert safe_return_url(request,'/safe/') == '/safe/'


def test_loan_and_disposal_workbenches_match_steps_and_preserve_scope(client,monkeypatch):
    from tests.test_sprint7_lifecycle_services import _loan
    from tests.test_sprint7_http import _open_disposal
    from tests.test_sprint8_support import add_active_asset
    ctx,asset,_ = active_asset_context('BENCH8')
    today = timezone.localdate()
    _loan(ctx,asset,'loan-workbench',loan_date=today,expected_return_date=today)
    second,_ = add_active_asset(ctx,'DISPOSAL8')
    disposal = _open_disposal(ctx,second,'disposal-workbench')
    monkeypatch.setattr('apps.assets.workbenches.timezone.localdate',lambda:today+timedelta(days=1))
    client.force_login(ctx['equipment'])
    page = client.get(reverse('assets:loan-workbench'),{'state':'overdue'})
    assert page.status_code == 200 and page.context['page_obj'].paginator.count == 1
    assert '逾期未归还' in page.content.decode()
    page = client.get(reverse('assets:disposal-workbench'),{'state':'actual'})
    assert page.status_code == 200 and page.context['page_obj'][0].pk == disposal.pk
    assert client.get(reverse('task-center')).status_code == 200
    client.force_login(make_user('workbench-other','employee'))
    assert client.get(reverse('assets:loan-workbench')).context['page_obj'].paginator.count == 0
    client.force_login(make_user('workbench-hr','hr'))
    assert client.get(reverse('assets:loan-workbench')).status_code == 403
    assert client.get(reverse('assets:disposal-workbench')).status_code == 403


def test_personal_preset_migration_roundtrip_keeps_business_records():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL migration verification')
    ctx,asset,_ = active_asset_context('PRESETMIG8')
    from apps.assets.models import Asset
    before = Asset.objects.filter(pk=asset.pk).values().get()
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([('reports','0002_sprint18_supply_report_types')])
        assert Asset.objects.filter(pk=asset.pk).values().get() == before
    finally:
        MigrationExecutor(connection).migrate(leaves)
    assert ReportPreset.objects.count() == 0
