"""Role-aware application navigation without changing backend authorization."""

from __future__ import annotations

from django.db.utils import OperationalError, ProgrammingError
from django.urls import reverse

from apps.masterdata.permissions import (
    can_access_setup,
    can_manage_masterdata,
    can_view_masterdata,
    current_company,
    resolve_department_ids,
    role_names_for,
)


ASSET_PRIMARY_ROLES = frozenset({"finance", "equipment", "warehouse"})
SUPPLY_PRIMARY_ROLES = frozenset(
    {"system_admin", "finance", "warehouse", "equipment", "management"}
)
TASK_PRIMARY_ROLES = frozenset(
    {"finance", "equipment", "employee", "warehouse", "hr"}
)
REPORT_PRIMARY_ROLES = frozenset(
    {
        "system_admin",
        "finance",
        "equipment",
        "department_manager",
        "warehouse",
        "hr",
        "management",
    }
)
SETTINGS_PRIMARY_ROLES = frozenset(
    {"system_admin", "finance", "equipment", "warehouse", "hr"}
)

_TASK_SUPPLY_VIEWS = frozenset(
    {
        "supplies:count-task-list",
        "supplies:count-task-create",
        "supplies:count-task-detail",
        "supplies:count-task-publish",
        "supplies:count-task-add-item",
        "supplies:count-line-record",
        "supplies:count-line-adjustment-cost",
        "supplies:count-line-resolve",
        "supplies:count-task-stop",
        "supplies:count-task-close",
        "supplies:count-task-cancel",
    }
)
_SETTINGS_SUPPLY_VIEWS = frozenset(
    {
        "supplies:category-list",
        "supplies:category-create",
        "supplies:category-edit",
        "supplies:category-deactivate",
        "supplies:warehouse-list",
        "supplies:warehouse-create",
        "supplies:warehouse-edit",
        "supplies:warehouse-deactivate",
        "supplies:item-import",
        "supplies:opening-stock-import",
        "supplies:opening-custody-import",
    }
)
_SETTINGS_FINANCE_VIEWS = frozenset(
    {
        "finance:policy-list",
        "finance:policy-create",
        "finance:policy-detail",
        "finance:policy-edit",
        "finance:policy-action",
        "finance:category-policy",
        "finance:fixed-category-list",
        "finance:fixed-category-create",
        "finance:fixed-category-edit",
        "finance:fixed-category-deactivate",
        "finance:settings",
    }
)
_ASSET_MAINTENANCE_VIEWS = frozenset(
    {
        "maintenance:plan-list",
        "maintenance:plan-create",
        "maintenance:plan-detail",
        "maintenance:plan-edit",
        "maintenance:plan-status",
    }
)
_INDIVIDUAL_DURABLE_VIEWS = frozenset(
    {
        "supplies:individual-durable-list",
        "supplies:individual-durable-create",
    }
)

_PAGE_LABELS = {
    "home": "首页",
    "task-center": "我的待办",
    "settings-center": "设置首页",
    "assets:asset-list": "资产台账",
    "assets:asset-create": "新增资产",
    "assets:asset-detail": "资产详情",
    "assets:label-queue": "标签与二维码",
    "supplies:dashboard": "库存总览",
    "supplies:document-list": "入库、领用与调拨",
    "supplies:stock-balance-list": "当前库存",
    "supplies:stock-ledger-list": "出入库明细",
    "supplies:custody-list": "耐用品保管",
    "supplies:my-custodies": "我的领用与保管",
    "supplies:item-list": "物品档案",
    "supplies:reconciliation-help": "库存核对帮助",
    "inventory:task-list": "资产盘点",
    "supplies:count-task-list": "物品盘点",
    "maintenance:due-list": "保养任务",
    "maintenance:plan-list": "保养计划",
    "offboarding:clearance-list": "离职清退",
    "finance:pending-list": "待财务确认",
    "finance:batch-list": "固定资产折旧",
    "reports:report-center": "报表中心",
    "reports:supply-report-index": "办公用品与低值品报表",
    "reports:tplus-export": "T+ 对账",
    "reports:external-reference-list": "T+ 资产卡片编码",
    "masterdata:company-list": "公司",
    "masterdata:department-list": "部门",
    "masterdata:employee-list": "人员",
    "masterdata:location-list": "位置",
    "masterdata:category-list": "资产分类",
    "masterdata:coding-scheme-list": "编码规则",
    "masterdata:system-settings": "系统参数",
    "masterdata:user-permissions-list": "用户与权限",
    "masterdata:setup": "初始化检查",
    "imports:home": "初始化与 Excel 导入",
    "audit:log-list": "操作日志",
    "operations:backup-list": "数据备份",
    "supplies:category-list": "物品分类",
    "supplies:warehouse-list": "仓库档案",
    "finance:policy-list": "折旧政策",
    "finance:fixed-category-list": "固定资产类别",
    "finance:settings": "财务参数",
    "supplies:individual-durable-list": "逐件低值耐用品",
    "supplies:individual-durable-create": "新增逐件低值耐用品",
}


def _initialized(company) -> bool:
    if company is None:
        return False
    from apps.masterdata.models import InitializationSetting

    return InitializationSetting.objects.filter(
        company=company,
        initialization_completed=True,
    ).exists()


def _active_section(view_name: str, namespace: str) -> str:
    if view_name == "home":
        return "home"
    if view_name in _ASSET_MAINTENANCE_VIEWS:
        return "assets"
    if view_name in _INDIVIDUAL_DURABLE_VIEWS:
        return "assets"
    if view_name == "task-center" or namespace in {
        "inventory",
        "maintenance",
        "offboarding",
    }:
        return "tasks"
    if view_name in _TASK_SUPPLY_VIEWS:
        return "tasks"
    if view_name == "settings-center" or namespace in {
        "masterdata",
        "imports",
        "audit",
        "operations",
    }:
        return "settings"
    if view_name in _SETTINGS_SUPPLY_VIEWS or view_name in _SETTINGS_FINANCE_VIEWS:
        return "settings"
    if namespace == "reports" or namespace == "finance":
        return "finance_reports"
    if namespace == "assets":
        return "assets"
    if namespace == "supplies":
        return "supplies"
    return "home"


def _is_individual_durable_entry(view_name: str, query) -> bool:
    if view_name in _INDIVIDUAL_DURABLE_VIEWS:
        return True
    if view_name == "assets:asset-list":
        return bool(
            query.get("accounting_treatment") == "controlled_non_fixed"
            or query.get("view") == "individual_durable"
        )
    if view_name == "assets:asset-create":
        return query.get("source") == "individual_durable"
    return False


def _active_item(view_name: str, active_section: str, query=None) -> str:
    query = query or {}
    if active_section == "assets":
        if _is_individual_durable_entry(view_name, query):
            return "individual_durables"
        if view_name == "assets:asset-create":
            return "asset_create"
        if view_name.startswith("assets:label-") or view_name.startswith(
            "assets:qr-"
        ):
            return "asset_labels"
        if view_name in _ASSET_MAINTENANCE_VIEWS:
            return "maintenance_plans"
        return "asset_ledger"
    if active_section == "supplies":
        if view_name == "supplies:dashboard":
            return "supply_overview"
        if view_name.startswith("supplies:document-"):
            return "supply_documents"
        if view_name.startswith("supplies:custody-") or view_name == "supplies:my-custodies":
            return "supply_custodies"
        if view_name.startswith("supplies:item-"):
            return "supply_items"
        return "supply_stock"
    if active_section == "tasks":
        if view_name == "task-center":
            return "task_center"
        if view_name.startswith("assets:label-") or view_name.startswith(
            "assets:qr-"
        ):
            return "task_labels"
        if view_name.startswith("assets:"):
            return "my_assets" if view_name == "assets:asset-list" else "task_center"
        if view_name == "supplies:my-custodies" or view_name.startswith(
            "supplies:custody-"
        ):
            return "my_custodies"
        if view_name.startswith("inventory:"):
            return "asset_inventory"
        if view_name in _TASK_SUPPLY_VIEWS:
            return "supply_inventory"
        if view_name.startswith("maintenance:"):
            return "maintenance_tasks"
        if view_name.startswith("offboarding:"):
            return "offboarding"
    if active_section == "finance_reports":
        if view_name.startswith("finance:pending-"):
            return "pending_finance"
        if view_name.startswith("finance:batch-"):
            return "depreciation"
        if view_name.startswith("finance:"):
            return "asset_finance"
        if view_name == "reports:tplus-export" or view_name.startswith(
            "reports:external-reference"
        ):
            return "tplus"
        return "report_center"
    if active_section == "settings":
        exact = {
            "settings-center": "settings_center",
            "masterdata:company-list": "company",
            "masterdata:department-list": "department",
            "masterdata:employee-list": "employee",
            "masterdata:location-list": "location",
            "masterdata:category-list": "asset_category",
            "masterdata:coding-scheme-list": "coding_scheme",
            "masterdata:user-permissions-list": "user_permissions",
            "masterdata:system-settings": "system_parameters",
            "masterdata:setup": "initialization",
            "imports:home": "imports",
            "supplies:category-list": "supply_category",
            "supplies:warehouse-list": "supply_warehouse",
            "finance:policy-list": "finance_policy",
            "finance:fixed-category-list": "fixed_asset_category",
            "finance:settings": "finance_settings",
            "audit:log-list": "audit",
            "operations:backup-list": "backup",
        }
        if view_name in exact:
            return exact[view_name]
        if view_name.startswith("masterdata:user-"):
            return "user_permissions"
        if view_name.startswith("masterdata:coding-"):
            return "coding_scheme"
        if view_name.startswith("masterdata:"):
            return "settings_center"
        if view_name.startswith("imports:"):
            return "imports"
        if view_name.startswith("audit:"):
            return "audit"
        if view_name.startswith("operations:"):
            return "backup"
        if view_name.startswith("supplies:category-"):
            return "supply_category"
        if view_name.startswith("supplies:warehouse-"):
            return "supply_warehouse"
        if view_name.startswith("supplies:item-"):
            return "supply_item"
        if view_name in _SETTINGS_FINANCE_VIEWS:
            return "finance_policy"
        return "settings_center"
    return "home"


def _page_label(view_name: str, active_section: str, query=None) -> str:
    query = query or {}
    if _is_individual_durable_entry(view_name, query):
        return (
            "新增逐件低值耐用品"
            if view_name in {
                "assets:asset-create",
                "supplies:individual-durable-create",
            }
            else "逐件低值耐用品"
        )
    if view_name in _PAGE_LABELS:
        return _PAGE_LABELS[view_name]
    prefixes = (
        ("assets:", "资产详情与操作"),
        ("supplies:count-", "物品盘点"),
        ("supplies:document-", "入库、领用与调拨"),
        ("supplies:custody-", "耐用品保管"),
        ("supplies:", "办公用品与低值品"),
        ("inventory:", "资产盘点"),
        ("maintenance:", "保养任务"),
        ("offboarding:", "离职清退"),
        ("finance:", "资产财务"),
        ("reports:supply-", "办公用品与低值品报表"),
        ("reports:", "报表与财务"),
        ("masterdata:", "基础资料"),
        ("imports:", "初始化与 Excel 导入"),
        ("audit:", "操作日志"),
        ("operations:", "数据备份"),
    )
    for prefix, label in prefixes:
        if view_name.startswith(prefix):
            return label
    return {
        "assets": "资产管理",
        "supplies": "办公用品与低值品",
        "tasks": "我的工作",
        "finance_reports": "报表与财务",
        "settings": "基础资料与设置",
    }.get(active_section, "首页")


def build_application_navigation(request) -> dict:
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {}

    try:
        company = current_company(include_inactive=True)
        initialized = _initialized(company)
        roles = role_names_for(user)
        manager_department_ids = (
            resolve_department_ids(user, company)
            if company and "department_manager" in roles
            else set()
        )
        manager_has_scope = bool(manager_department_ids)
    except (OperationalError, ProgrammingError):
        company = None
        initialized = False
        roles = set()
        manager_department_ids = set()
        manager_has_scope = False

    from apps.assets.permissions import can_create_asset_draft
    from apps.audit.permissions import can_view_audit_logs
    from apps.finance.permissions import can_manage_finance, can_view_finance
    from apps.operations.permissions import can_manage_backups
    from apps.reports.permissions import can_view_external_reference, can_view_report
    from apps.supplies.models import SupplyItemType
    from apps.supplies.permissions import (
        can_create_supply_document,
        can_import_opening_custody,
        can_manage_supply_category,
        can_manage_supply_item,
        can_manage_supply_warehouse,
        can_view_supply_custodies,
        can_view_supply_documents,
        can_view_supply_master_data,
        can_view_supply_module,
        can_view_supply_stock,
    )

    has_manager_work = "department_manager" in roles and manager_has_scope
    show_assets = initialized and bool(
        roles.intersection(ASSET_PRIMARY_ROLES) or has_manager_work
    )
    show_supplies = initialized and bool(
        roles.intersection(SUPPLY_PRIMARY_ROLES)
        and company
        and can_view_supply_module(user)
    )
    show_tasks = initialized and bool(
        roles.intersection(TASK_PRIMARY_ROLES) or has_manager_work
    )
    can_view_reports = bool(
        company
        and roles.intersection(REPORT_PRIMARY_ROLES)
        and (
            can_view_report(user, "asset_ledger")
            or can_view_report(user, "offboarding_unresolved")
        )
    )
    show_finance_reports = initialized and can_view_reports
    can_create_asset = bool(
        initialized and company and can_create_asset_draft(user, company)
    )
    if initialized and company and not can_create_asset and manager_department_ids:
        from apps.masterdata.models import Department

        scoped_department = Department.objects.filter(
            company=company,
            pk__in=manager_department_ids,
        ).first()
        can_create_asset = bool(
            scoped_department
            and can_create_asset_draft(user, company, scoped_department)
        )
    show_settings = bool(roles.intersection(SETTINGS_PRIMARY_ROLES))

    resolver_match = getattr(request, "resolver_match", None)
    view_name = getattr(resolver_match, "view_name", "") or ""
    namespace = getattr(resolver_match, "namespace", "") or ""
    active_section = _active_section(view_name, namespace)
    if active_section == "assets" and not show_assets:
        if show_tasks:
            active_section = "tasks"
        elif show_finance_reports:
            active_section = "finance_reports"
        elif show_settings:
            active_section = "settings"
    if active_section == "supplies" and not show_supplies:
        if view_name.startswith("supplies:item-") and show_settings:
            active_section = "settings"
        elif show_tasks:
            active_section = "tasks"
        elif show_finance_reports:
            active_section = "finance_reports"
        elif show_settings:
            active_section = "settings"
    if active_section == "finance_reports" and not show_finance_reports:
        if show_tasks:
            active_section = "tasks"
        elif show_settings:
            active_section = "settings"

    section_labels = {
        "assets": "资产管理",
        "supplies": "办公用品与低值品",
        "tasks": "我的工作",
        "finance_reports": "报表与财务",
        "settings": "基础资料与设置",
    }
    section_urls = {
        "assets": reverse("assets:asset-list"),
        "supplies": reverse("supplies:dashboard"),
        "tasks": reverse("task-center"),
        "finance_reports": reverse("reports:report-center"),
        "settings": reverse("settings-center"),
    }

    can_view_supply = bool(company and can_view_supply_module(user))
    can_view_stock = bool(company and can_view_supply_stock(user))
    can_view_documents = bool(company and can_view_supply_documents(user))
    can_view_custodies = bool(company and can_view_supply_custodies(user))
    can_view_supply_masters = bool(company and can_view_supply_master_data(user))
    can_manage_items = bool(
        company and can_manage_supply_item(user, SupplyItemType.DURABLE_QUANTITY)
    )

    return {
        "roles": roles,
        "initialized": initialized,
        "active_section": active_section,
        "active_item": _active_item(view_name, active_section, request.GET),
        "page_label": _page_label(view_name, active_section, request.GET),
        "section_label": section_labels.get(active_section, ""),
        "section_url": section_urls.get(active_section, ""),
        "show_assets": show_assets,
        "show_supplies": show_supplies,
        "show_tasks": show_tasks,
        "show_finance_reports": show_finance_reports,
        "show_settings": show_settings,
        "primary_count": 1
        + sum(
            bool(value)
            for value in (
                show_assets,
                show_supplies,
                show_tasks,
                show_finance_reports,
                show_settings,
            )
        ),
        "asset": {
            "can_create": can_create_asset,
            "can_manage_labels": initialized
            and bool(roles.intersection({"finance", "equipment"})),
            "can_view_maintenance_plans": initialized
            and bool(roles.intersection({"finance", "equipment"}) or has_manager_work),
        },
        "supplies": {
            "can_view_stock": can_view_stock,
            "can_view_documents": can_view_documents,
            "can_manage_documents": bool(
                company and can_create_supply_document(user)
            ),
            "can_view_custodies": can_view_custodies,
            "can_view_items": can_view_supply_masters,
            "can_manage_items": can_manage_items,
        },
        "tasks": {
            "can_view_asset_inventory": initialized
            and bool(
                roles.intersection({"finance", "equipment", "employee", "warehouse"})
                or has_manager_work
            ),
            "can_view_supply_inventory": initialized
            and can_view_supply
            and bool(
                roles.intersection({"finance", "equipment", "employee", "warehouse"})
                or has_manager_work
            ),
            "can_view_maintenance": initialized
            and bool(
                roles.intersection({"finance", "equipment", "employee", "warehouse"})
                or has_manager_work
            ),
            "can_view_offboarding": initialized
            and bool(
                roles.intersection(
                    {"finance", "equipment", "employee", "warehouse", "hr"}
                )
                or has_manager_work
            ),
            "can_manage_labels": initialized
            and bool(roles.intersection({"finance", "equipment", "warehouse"})),
            "can_view_my_assets": "employee" in roles,
            "can_view_my_custodies": can_view_custodies
            and bool(roles.intersection({"employee", "department_manager"})),
        },
        "finance_reports": {
            "can_manage_finance": can_manage_finance(user),
            "can_view_finance": can_view_finance(user),
            "can_view_reports": can_view_reports,
            "can_view_tplus": "finance" in roles,
            "can_view_external_references": can_view_external_reference(user),
            "can_view_audit": can_view_audit_logs(user),
        },
        "settings": {
            "can_access_setup": can_access_setup(user),
            "can_access_imports": bool(
                company
                and roles.intersection(
                    {"system_admin", "finance", "equipment", "warehouse", "hr"}
                )
            ),
            "can_view_company": can_view_masterdata(user, "company"),
            "can_view_department": can_view_masterdata(user, "department"),
            "can_view_employee": can_view_masterdata(user, "employee"),
            "can_show_employee_settings": can_view_masterdata(user, "employee")
            and bool(roles.intersection({"system_admin", "finance", "hr"})),
            "can_view_location": can_view_masterdata(user, "location"),
            "can_view_asset_category": can_view_masterdata(user, "asset_category"),
            "can_view_coding_scheme": can_view_masterdata(user, "coding_scheme"),
            "can_view_system_setting": can_view_masterdata(user, "system_setting"),
            "can_view_user_permissions": can_view_masterdata(
                user, "user_permissions"
            ),
            "can_manage_employee": can_manage_masterdata(user, "employee"),
            "can_view_supply_master_data": can_view_supply_masters,
            "can_manage_supply_categories": bool(
                company and can_manage_supply_category(user)
            ),
            "can_manage_supply_warehouses": bool(
                company and can_manage_supply_warehouse(user)
            ),
            "can_manage_supply_items": can_manage_items,
            "can_manage_supply_documents": bool(
                company and can_create_supply_document(user)
            ),
            "can_import_opening_custody": bool(
                company and can_import_opening_custody(user)
            ),
            "can_manage_finance": can_manage_finance(user),
            "can_view_audit": can_view_audit_logs(user),
            "can_manage_backups": can_manage_backups(user),
        },
        "home": {
            "show_asset_overview": initialized
            and bool(
                roles.intersection(
                    {"finance", "equipment", "employee", "warehouse", "management"}
                )
                or has_manager_work
            ),
            "show_supply_overview": initialized
            and can_view_supply
            and bool(
                roles.intersection(SUPPLY_PRIMARY_ROLES | {"employee"})
                or has_manager_work
            ),
            "show_admin_focus": roles == {"system_admin"},
            "show_hr_focus": roles == {"hr"},
            "show_management_focus": roles == {"management"},
        },
    }


def application_navigation(request):
    return {"app_navigation": build_application_navigation(request)}


def runtime_metadata(request):
    from django.conf import settings

    return {
        "runtime_metadata": {
            "version": settings.APP_VERSION,
            "commit": settings.APP_COMMIT_SHA,
            "short_commit": settings.APP_COMMIT_SHA[:7],
            "environment": settings.EAM_ENVIRONMENT,
            "is_development": settings.EAM_ENVIRONMENT == "development",
            "is_local": settings.EAM_ENVIRONMENT == "local",
        }
    }


__all__ = [
    "application_navigation",
    "build_application_navigation",
    "runtime_metadata",
]
