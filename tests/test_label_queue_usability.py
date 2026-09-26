import uuid
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlparse,parse_qs
import pytest
from django.urls import reverse
from apps.assets.models import AssetLabelPrintBatch,AssetLabelPrintItem,AssetQrIdentity
from apps.assets.registration import create_registered_asset
from apps.assets.qr_forms import LabelPrintForm
from apps.assets.qr_services import generate_print_batch
from apps.finance.services import confirm_asset_finance
from apps.finance.models import AssetFinance
from tests.test_unified_asset_identity import context,physical_data
from tests.test_sprint3_support import make_department,make_employee,make_company

pytestmark=pytest.mark.django_db(transaction=True)


def make_asset(ctx,index=1,**overrides):
    return create_registered_asset(actor=ctx['equipment'],company=ctx['company'],
        data=physical_data(ctx,asset_name=f'标签设备 {index}',equipment_number=f'EQUIP-{index:03}',**overrides),
        idempotency_key=f'label-asset-{index}')


class Forms(HTMLParser):
    def __init__(self):
        super().__init__();self.depth=0;self.nested=False;self.submit_form_ids=[];self.ids=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form':
            self.depth+=1;self.nested|=self.depth>1;self.ids.append(attrs.get('id'))
        if tag=='button' and attrs.get('type')=='submit':
            self.submit_form_ids.append(self.ids[-1] if self.ids else None)
    def handle_endtag(self,tag):
        if tag=='form':
            self.depth-=1
            if self.ids:self.ids.pop()


def test_queue_forms_and_cross_page_selection_data(context,client):
    for i in range(26):make_asset(context,i)
    client.force_login(context['finance'])
    response=client.get(reverse('assets:label-queue'))
    assert response.status_code==200
    assert response.context['all_filtered_count']==26 and len(response.context['all_filtered_ids'])==26
    assert len(response.context['rows'])==25
    parser=Forms();parser.feed(response.content.decode())
    assert not parser.nested
    assert 'bulk-selection-form' in parser.submit_form_ids
    assert 'data-page-jump' in response.content.decode()
    first_key=response.context['selection_key']
    second=client.get(reverse('assets:label-queue'),{'page':2})
    assert second.context['selection_key']==first_key and len(second.context['rows'])==1
    assert not AssetLabelPrintBatch.objects.exists()


def test_department_subtree_and_equipment_number_search(context,client):
    child=make_department(context['company'],'SUB',parent=context['department'])
    employee=make_employee(context['company'],child,'SUB-E')
    asset=make_asset(context,1,department=child,responsible_employee=employee)
    client.force_login(context['finance'])
    query={'department':context['department'].pk,'q':'EQUIP-001','page_size':100}
    queue=client.get(reverse('assets:label-queue'),query)
    assert [r['asset'].pk for r in queue.context['rows']]==[asset.pk]
    finance=client.get(reverse('finance:pending-list'),{'q':'EQUIP-001'})
    assert [a.pk for a in finance.context['assets']]==[asset.pk]
    other=make_company('FOREIGN',active=False)
    other_department=make_department(other,'NOPE')
    denied=client.get(reverse('assets:label-queue'),{'department':other_department.pk})
    assert denied.context['page_obj'].paginator.count==0 and denied.context['filter_form'].errors


def test_print_keeps_filter_context_and_clears_selected_ids(context,client):
    asset=make_asset(context)
    client.force_login(context['finance'])
    query={'q':'EQUIP-001','department':str(context['department'].pk),'page_size':'50','status':'ready_to_print'}
    get=client.get(reverse('assets:label-queue'),query)
    data={**query,'asset_ids':[str(asset.pk)],'idempotency_key':'print-test','include_model':'on'}
    response=client.post(reverse('assets:label-queue'),data)
    assert response.status_code==302
    print_page=client.get(response.url)
    assert print_page.status_code==200
    assert print_page.context['selection_key']==get.context['selection_key']
    assert print_page.context['cleared_asset_ids']==[str(asset.pk)]
    assert parse_qs(urlparse(print_page.context['queue_url']).query)['department']==[str(context['department'].pk)]
    assert AssetLabelPrintBatch.objects.count()==AssetLabelPrintItem.objects.count()==1
    assert AssetQrIdentity.objects.get(asset=asset).label_status=='printed'
    assert client.post(reverse('assets:label-queue'),data).status_code==302
    assert AssetLabelPrintBatch.objects.count()==1
    # Explicit reprinting still needs the existing acknowledgement.
    data['idempotency_key']='new-print'
    result=client.post(reverse('assets:label-queue'),data)
    assert result.status_code==200 and result.context['form'].errors
    assert AssetLabelPrintBatch.objects.count()==1


def test_invalid_selection_remains_checked_and_limits_are_server_side(context,client):
    asset=make_asset(context)
    generate_print_batch(actor=context['finance'],assets=[asset],idempotency_key='prior')
    client.force_login(context['finance'])
    response=client.post(reverse('assets:label-queue'),{'asset_ids':[str(asset.pk)],'status':'printed','idempotency_key':'attempt'})
    assert response.context['rows'][0]['selected'] and response.context['form'].errors
    from types import SimpleNamespace
    assets=[SimpleNamespace(pk=uuid.uuid4(),asset_code=str(i),asset_name='模拟设备') for i in range(201)]
    form=LabelPrintForm({'asset_ids':[str(a.pk) for a in assets]},assets=assets)
    assert not form.is_valid() and '200' in str(form.errors)
    form=LabelPrintForm({'asset_ids':[str(asset.pk)],'idempotency_key':'a'*129},assets=[asset])
    assert not form.is_valid() and 'idempotency_key' in form.errors


def test_stale_finance_link_redirects_without_changing_confirmed_amount(context,client):
    asset=make_asset(context)
    confirm_asset_finance(actor=context['finance'],asset=asset,finance_data={'accounting_treatment':'controlled_non_fixed',
        'original_cost':Decimal('1000.00')},idempotency_key='confirmed',reason='核对')
    client.force_login(context['finance'])
    url=reverse('finance:finance-confirm',args=[asset.pk])
    target=reverse('finance:asset-finance-detail',args=[asset.pk])
    assert client.get(url).url==target
    assert client.post(url,{'action':'save','original_cost':'9999.00'}).url==target
    assert AssetFinance.objects.get(asset=asset).original_cost==Decimal('1000.00')
    assert client.put(url).status_code==405
    client.force_login(context['equipment'])
    assert client.get(url).status_code==403
