"""Read-only department links follow the destination lists' existing scopes."""
from urllib.parse import urlencode

from django.urls import reverse

from apps.assets.models import Asset
from apps.assets.permissions import scoped_assets
from apps.supplies.permissions import can_view_supply_custodies, scoped_supply_custodies

from .directory import employee_department_tree
from .hierarchy import descendant_ids
from .models import Department, InitializationSetting
from .permissions import can_view_masterdata, scoped_departments, scoped_employees


def department_business_context(actor, company, department):
    if not can_view_masterdata(actor, "department") or department.company_id != company.pk:
        return None
    departments = scoped_departments(actor, company)
    if not departments.filter(pk=department.pk).exists():
        return None

    def url(route, **params):
        return reverse(route) + "?" + urlencode(params)

    cards = []
    if InitializationSetting.objects.filter(company=company, initialization_completed=True).exists():
        assets = scoped_assets(actor, company).filter(record_status=Asset.RecordStatus.ACTIVE,
            department_id__in=descendant_ids(Department, company=company, identifier=department.pk))
        cards.append({"key":"assets", "label":"当前业务资产", "count":assets.count(), "unit":"项",
            "scope":"本部门及下级部门中的授权资产；未归档。",
            "action":"查看资产清单", "url":url("assets:asset-list", department=department.pk)})

    if can_view_masterdata(actor, "employee"):
        employees = scoped_employees(actor, company)
        _options, descendants, _labels = employee_department_tree(departments,
            employees.values_list("department_id", flat=True).distinct())
        count = employees.filter(department_id__in=descendants.get(department.pk, ())).count()
        cards.append({"key":"employees", "label":"员工档案", "count":count, "unit":"人",
            "scope":"本部门及下级部门中的授权人员；含停用档案与全部任职状态。",
            "action":"查看员工目录", "url":url("masterdata:employee-list", department=department.pk, status="all") if count else None})

    if can_view_supply_custodies(actor):
        count = scoped_supply_custodies(actor, company).filter(department=department, status="open").count()
        cards.append({"key":"custodies", "label":"未结清数量物品保管", "count":count, "unit":"条",
            "scope":"仅本部门；按保管记录计数，不合并物品数量。",
            "action":"查看未结清保管", "url":url("supplies:custody-list", department=department.pk, status="open")})
    return {"cards":cards} if cards else None
