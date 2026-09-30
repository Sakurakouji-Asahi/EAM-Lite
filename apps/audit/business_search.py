from django.db.models import Q,CharField
from django.db.models.functions import Cast
from apps.assets.permissions import scoped_assets
from apps.supplies.permissions import scoped_supply_documents
from .display import ACTION_LABELS


def filter_business_audit(queryset,*,actor,company,query):
    query=query.strip()
    if not query:return queryset
    asset_ids=scoped_assets(actor,company).filter(Q(asset_code__icontains=query)|Q(equipment_number__icontains=query)|Q(asset_name__icontains=query)).annotate(
        audit_id=Cast('pk',CharField())).values('audit_id')
    documents=scoped_supply_documents(actor,company).filter(document_no__icontains=query).annotate(audit_id=Cast('pk',CharField())).values('audit_id')
    match=Q(action__in=[key for key,label in ACTION_LABELS.items() if query in label])
    match|=Q(object_type='Asset',object_id__in=asset_ids)|Q(object_type='SupplyDocument',object_id__in=documents)
    # IDs resolve through the actor's present asset scope; payloads remain redacted.
    ids=list(asset_ids.values_list('audit_id',flat=True))
    if ids:
        for prefix in ('old_data_json','new_data_json'):
            for key in ('asset','asset_id'):
                match|=Q(**{f'{prefix}__{key}__in':ids})
    return queryset.filter(match)
