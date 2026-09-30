"""Build links for the displayed page using each destination's object scope."""
from django.urls import reverse


def page_source_links(actor, company, rows):
    from apps.assets.permissions import scoped_assets
    from apps.assets.lifecycle_permissions import scoped_disposals
    from apps.inventory.permissions import scoped_inventory_tasks
    from apps.maintenance.permissions import scoped_maintenance_plans
    from apps.maintenance.models import MaintenanceRecord
    from apps.offboarding.permissions import scoped_clearances
    from apps.supplies.permissions import scoped_supply_documents, scoped_supply_count_tasks, scoped_supply_custodies

    routes = {'asset':'assets:asset-detail','disposal':'assets:disposal-detail', 'inventory':'inventory:task-detail',
        'plan':'maintenance:plan-detail','record':'maintenance:record-detail', 'clearance':'offboarding:clearance-detail',
        'document':'supplies:document-detail', 'count':'supplies:count-task-detail', 'custody':'supplies:custody-detail'}
    refs = []
    for row in rows:
        links = dict(row.get('_source_refs', {}))
        if row.get('_asset_id'):
            links['asset_code'] = ('asset', str(row['_asset_id']))
        refs.append(links)
    needed = {}
    for links in refs:
        for kind, pk in links.values():
            if pk:
                needed.setdefault(kind,set()).add(str(pk))
    scoped = {'asset':scoped_assets, 'disposal':scoped_disposals, 'inventory':scoped_inventory_tasks,
        'plan':scoped_maintenance_plans, 'clearance':scoped_clearances, 'document':scoped_supply_documents,
        'count':scoped_supply_count_tasks, 'custody':scoped_supply_custodies}
    allowed = {}
    for kind, ids in needed.items():
        if kind == 'record':
            queryset = MaintenanceRecord.objects.filter(company=company, maintenance_plan__in=scoped_maintenance_plans(actor,company))
        else:
            queryset = scoped[kind](actor,company)
        allowed[kind] = {str(pk) for pk in queryset.filter(pk__in=ids).values_list('pk',flat=True)}
    return [{column:reverse(routes[kind],args=[pk]) for column,(kind,pk) in links.items()
             if pk and str(pk) in allowed.get(kind,set())} for links in refs]
