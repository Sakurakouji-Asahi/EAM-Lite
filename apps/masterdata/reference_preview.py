from django.urls import reverse
from apps.assets.permissions import scoped_assets
from apps.assets.domain import TERMINAL_ASSET_STATUSES
from apps.maintenance.permissions import scoped_maintenance_plans
from apps.supplies.permissions import scoped_supply_documents,scoped_supply_custodies


def reference_preview(actor,resource,obj):
    """Only show existing visible references; never predict permission to deactivate."""
    field={'department':'department','employee':'responsible_employee','location':'location','asset_category':'category'}[resource]
    assets=scoped_assets(actor,obj.company).filter(**{field:obj})
    active=assets.exclude(asset_status__in=TERMINAL_ASSET_STATUSES).filter(record_status='active')
    groups=[]
    def add(label,queryset,route,title):
        count=queryset.count()
        if count:
            groups.append({'label':label,'count':count,'items':[{'name':title(row),'url':reverse(route,args=[row.pk])} for row in queryset.order_by('pk')[:5]]})
    add('逐件资产（未归档、未终结）',active,'assets:asset-detail',lambda row:f'{row.asset_code or "未编号"} · {row.asset_name}')
    plans=scoped_maintenance_plans(actor,obj.company).filter(status__in=('active','suspended'))
    plans=plans.filter(responsible_employee=obj) if resource=='employee' else plans.filter(asset__in=assets)
    add('启用或暂停的保养计划',plans,'maintenance:plan-detail',lambda row:row.name)
    if resource in ('department','employee'):
        relation='department' if resource=='department' else 'employee'
        documents=scoped_supply_documents(actor,obj.company).filter(status='draft',**{relation:obj})
        add('待过账物品单据',documents,'supplies:document-detail',lambda row:row.document_no)
        custodies=scoped_supply_custodies(actor,obj.company).filter(status='open',**{relation:obj}).select_related('item')
        add('未结清数量物品保管',custodies,'supplies:custody-detail',lambda row:f'{row.item.item_code} · {row.item.name}')
    return groups
