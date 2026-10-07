"""Find immutable audit evidence through currently accessible business objects."""

from django.db.models import CharField, Q, UUIDField, Value
from django.db.models.functions import Cast, Replace

from apps.assets.permissions import scoped_assets
from apps.inventory.models import (
    InventoryResolution, InventoryScan, InventorySurplus, InventoryTaskAsset,
    InventoryTaskAssignee,
)
from apps.inventory.permissions import scoped_inventory_tasks
from apps.maintenance.models import MaintenanceProblem, MaintenanceRecord
from apps.maintenance.permissions import scoped_maintenance_plans
from apps.masterdata.permissions import scoped_employees
from apps.offboarding.permissions import scoped_clearance_items, scoped_clearances
from apps.supplies.permissions import scoped_supply_documents

from .display import ACTION_LABELS


def _object_match(object_type, objects):
    # Audit object IDs are strings, including integer Employee primary keys.
    expression = Cast("pk", CharField())
    field = "object_id"
    if isinstance(objects.model._meta.pk, UUIDField):
        # UUID storage differs between native PostgreSQL and SQLite. Normalize
        # only the read query; saved evidence and exact ID filters stay intact.
        expression = Replace(expression, Value("-"), Value(""))
        field = "business_uuid_id"
    ids = objects.order_by().annotate(audit_id=expression).values("audit_id")
    return Q(object_type=object_type, **{f"{field}__in": ids})


def filter_business_audit(queryset, *, actor, company, query):
    query = query.strip()
    if not query:
        return queryset
    assets = scoped_assets(actor, company).filter(
        Q(asset_code__icontains=query) | Q(equipment_number__icontains=query)
        | Q(asset_name__icontains=query)
    )
    documents = scoped_supply_documents(actor, company).filter(document_no__icontains=query)
    employees = scoped_employees(actor, company).filter(
        Q(employee_no__icontains=query) | Q(name__icontains=query)
    )
    tasks = scoped_inventory_tasks(actor, company).filter(
        Q(task_code__icontains=query) | Q(name__icontains=query)
    )
    plans = scoped_maintenance_plans(actor, company).filter(name__icontains=query)
    clearances = scoped_clearances(actor, company).filter(employee__in=employees)

    match = Q(action__in=[key for key, label in ACTION_LABELS.items() if query in label])
    for object_type, objects in (
        ("Asset", assets), ("SupplyDocument", documents), ("Employee", employees),
        ("EmployeeAssetClearance", clearances),
        ("EmployeeAssetClearanceItem", scoped_clearance_items(actor, company).filter(clearance__in=clearances)),
        ("InventoryTask", tasks),
        ("InventoryTaskAssignee", InventoryTaskAssignee.objects.filter(company=company, inventory_task__in=tasks)),
        ("InventoryTaskAsset", InventoryTaskAsset.objects.filter(company=company, inventory_task__in=tasks)),
        ("InventoryScan", InventoryScan.objects.filter(company=company, inventory_task__in=tasks)),
        ("InventorySurplus", InventorySurplus.objects.filter(company=company, inventory_task__in=tasks)),
        ("InventoryResolution", InventoryResolution.objects.filter(company=company, inventory_task_asset__inventory_task__in=tasks)),
        ("MaintenancePlan", plans),
        ("MaintenanceRecord", MaintenanceRecord.objects.filter(company=company, maintenance_plan__in=plans)),
        ("MaintenanceProblem", MaintenanceProblem.objects.filter(company=company, maintenance_record__maintenance_plan__in=plans)),
    ):
        match |= _object_match(object_type, objects)

    # IDs resolve through the actor's present asset scope; payloads remain redacted.
    ids = [str(pk) for pk in assets.order_by().values_list("pk", flat=True)]
    if ids:
        for prefix in ("old_data_json", "new_data_json"):
            for key in ("asset", "asset_id"):
                match |= Q(**{f"{prefix}__{key}__in": ids})
    return queryset.annotate(
        business_uuid_id=Replace("object_id", Value("-"), Value(""))
    ).filter(match)
