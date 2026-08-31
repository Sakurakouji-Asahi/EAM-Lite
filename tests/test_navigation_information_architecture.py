from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from django.urls import reverse

from tests.test_sprint3_support import (
    complete_initialization,
    grant_scope,
    make_company,
    make_department,
    make_employee,
    make_user,
)


pytestmark = pytest.mark.django_db(transaction=True)


class DesktopNavigationParser(HTMLParser):
    """Parse the first rendered app navigation instead of matching strings."""

    def __init__(self):
        super().__init__()
        self.in_navigation = False
        self.completed = False
        self.sections = []
        self.items = []
        self.hrefs = []
        self.active_sections = []
        self.active_items = []
        self.group_item_counts = {}
        self.section_labels = {}
        self.current_group = None
        self.in_summary = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        if tag == "nav" and "app-nav" in classes and not self.completed:
            self.in_navigation = True
            return
        if not self.in_navigation:
            return

        section = attributes.get("data-nav-section")
        if section and section not in self.sections:
            self.sections.append(section)
        if tag == "details" and section:
            self.current_group = section
            self.group_item_counts.setdefault(section, 0)
            if "is-current" in classes:
                self.active_sections.append(section)
        if tag == "summary" and self.current_group:
            self.in_summary = True
        if tag == "a":
            item = attributes.get("data-nav-item")
            if item:
                self.items.append(item)
                self.group_item_counts[self.current_group] += 1
                if "active" in classes:
                    self.active_items.append(item)
            elif section == "home" and "active" in classes:
                self.active_sections.append("home")
            href = attributes.get("href")
            if href:
                self.hrefs.append(href)

    def handle_endtag(self, tag):
        if not self.in_navigation:
            return
        if tag == "summary":
            self.in_summary = False
        if tag == "details":
            self.current_group = None
        if tag == "nav":
            self.in_navigation = False
            self.completed = True

    def handle_data(self, data):
        if self.in_navigation and self.in_summary and self.current_group:
            label = data.strip()
            if label:
                self.section_labels[self.current_group] = label


class TopbarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.search_forms = []
        self.in_search = False
        self.search_inputs = []
        self.create_hrefs = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and "data-global-asset-search" in attributes:
            self.in_search = True
            self.search_forms.append(attributes)
        elif tag == "input" and self.in_search:
            self.search_inputs.append(attributes)
        elif tag == "a" and "data-topbar-asset-create" in attributes:
            self.create_hrefs.append(attributes.get("href"))

    def handle_endtag(self, tag):
        if tag == "form" and self.in_search:
            self.in_search = False


def parse_navigation(response):
    parser = DesktopNavigationParser()
    parser.feed(response.content.decode())
    return parser


def parse_topbar(response):
    parser = TopbarParser()
    parser.feed(response.content.decode())
    return parser


@pytest.fixture
def navigation_users():
    company = make_company("NAVIA")
    users = {
        role: make_user(f"nav-{role}", role)
        for role in (
            "system_admin",
            "finance",
            "equipment",
            "department_manager",
            "employee",
            "warehouse",
            "hr",
            "management",
        )
    }
    department = make_department(company, "NAVD")
    grant_scope(
        users["department_manager"],
        company,
        department,
        assigned_by=users["system_admin"],
    )
    make_employee(
        company,
        department,
        "NAVE",
        user=users["employee"],
    )
    complete_initialization(company, users["system_admin"])
    return company, users


def test_primary_navigation_is_task_oriented_for_all_eight_roles(
    client, navigation_users
):
    _company, users = navigation_users
    expected = {
        "system_admin": ["home", "supplies", "finance_reports", "settings"],
        "finance": [
            "home",
            "assets",
            "supplies",
            "tasks",
            "finance_reports",
            "settings",
        ],
        "equipment": [
            "home",
            "assets",
            "supplies",
            "tasks",
            "finance_reports",
            "settings",
        ],
        "department_manager": ["home", "assets", "tasks", "finance_reports"],
        "employee": ["home", "tasks"],
        "warehouse": [
            "home",
            "assets",
            "supplies",
            "tasks",
            "finance_reports",
            "settings",
        ],
        "hr": ["home", "tasks", "finance_reports", "settings"],
        "management": ["home", "supplies", "finance_reports"],
    }

    for role, user in users.items():
        client.force_login(user)
        response = client.get(reverse("home"))
        assert response.status_code == 200
        navigation = parse_navigation(response)
        assert navigation.sections == expected[role]
        assert len(navigation.sections) <= 6
        assert all(count > 0 for count in navigation.group_item_counts.values())
        assert navigation.section_labels == {
            section: {
                "assets": "资产管理",
                "supplies": "办公用品与低值品",
                "tasks": "我的工作",
                "finance_reports": "报表与财务",
                "settings": "基础资料与设置",
            }[section]
            for section in navigation.sections
            if section != "home"
        }


def test_key_secondary_navigation_respects_role_focus(client, navigation_users):
    _company, users = navigation_users

    cases = {
        "employee": {
            "present": {
                "task_center",
                "my_assets",
                "my_custodies",
                "asset_inventory",
                "supply_inventory",
                "maintenance_tasks",
                "offboarding",
            },
            "absent": {"pending_finance", "supply_stock", "imports"},
        },
        "warehouse": {
            "present": {
                "asset_ledger",
                "asset_create",
                "individual_durables",
                "supply_overview",
                "supply_stock",
                "supply_documents",
                "supply_custodies",
                "task_center",
                "task_labels",
                "report_center",
                "supply_category",
                "supply_warehouse",
                "imports",
            },
            "absent": {"pending_finance", "depreciation", "finance_policy"},
        },
        "equipment": {
            "present": {
                "asset_ledger",
                "asset_create",
                "asset_labels",
                "maintenance_plans",
                "supply_overview",
                "supply_stock",
                "supply_documents",
                "supply_custodies",
                "supply_items",
                "task_center",
                "report_center",
                "asset_category",
                "location",
                "imports",
            },
            "absent": {
                "pending_finance",
                "tplus",
                "supply_category",
                "supply_warehouse",
            },
        },
        "hr": {
            "present": {
                "task_center",
                "offboarding",
                "report_center",
                "employee",
                "imports",
                "audit",
            },
            "absent": {"asset_ledger", "supply_stock", "pending_finance"},
        },
        "management": {
            "present": {
                "supply_overview",
                "supply_stock",
                "supply_documents",
                "supply_custodies",
                "supply_items",
                "report_center",
            },
            "absent": {"asset_create", "pending_finance", "depreciation", "imports"},
        },
        "system_admin": {
            "present": {
                "supply_overview",
                "supply_stock",
                "supply_documents",
                "supply_custodies",
                "supply_items",
                "report_center",
                "settings_center",
                "supply_category",
                "supply_warehouse",
                "coding_scheme",
                "user_permissions",
                "imports",
                "audit",
                "backup",
            },
            "absent": {
                "asset_create",
                "pending_finance",
                "depreciation",
                "asset_ledger",
                "tplus",
            },
        },
    }

    for role, assertions in cases.items():
        client.force_login(users[role])
        navigation = parse_navigation(client.get(reverse("home")))
        items = set(navigation.items)
        assert assertions["present"] <= items
        assert assertions["absent"].isdisjoint(items)


def test_active_group_and_item_follow_role_specific_entry(client, navigation_users):
    _company, users = navigation_users
    cases = (
        ("employee", "assets:asset-list", "tasks", "my_assets"),
        ("warehouse", "supplies:category-list", "settings", "supply_category"),
        ("warehouse", "assets:asset-list", "assets", "asset_ledger"),
        ("finance", "reports:report-center", "finance_reports", "report_center"),
        ("equipment", "maintenance:plan-list", "assets", "maintenance_plans"),
        ("equipment", "supplies:item-list", "supplies", "supply_items"),
        ("system_admin", "supplies:dashboard", "supplies", "supply_overview"),
        ("management", "supplies:stock-balance-list", "supplies", "supply_stock"),
        ("hr", "offboarding:clearance-list", "tasks", "offboarding"),
        ("hr", "reports:report-center", "finance_reports", "report_center"),
    )
    for role, view_name, section, item in cases:
        client.force_login(users[role])
        response = client.get(reverse(view_name))
        assert response.status_code == 200
        navigation = parse_navigation(response)
        assert navigation.active_sections == [section]
        assert navigation.active_items == [item]
        assert response.content.decode().count('aria-label="面包屑"') == 1


def test_task_and_settings_centers_are_role_gated(client, navigation_users):
    _company, users = navigation_users
    for role in (
        "finance",
        "equipment",
        "department_manager",
        "employee",
        "warehouse",
        "hr",
    ):
        client.force_login(users[role])
        assert client.get(reverse("task-center")).status_code == 200
    for role in ("system_admin", "management"):
        client.force_login(users[role])
        assert client.get(reverse("task-center")).status_code == 403

    for role in ("system_admin", "finance", "equipment", "warehouse", "hr"):
        client.force_login(users[role])
        assert client.get(reverse("settings-center")).status_code == 200
    for role in ("department_manager", "employee", "management"):
        client.force_login(users[role])
        assert client.get(reverse("settings-center")).status_code == 403


def test_employee_stays_compact_and_non_finance_roles_do_not_receive_finance_values(
    client, navigation_users
):
    _company, users = navigation_users
    client.force_login(users["employee"])
    employee = client.get(reverse("home"))
    employee_navigation = parse_navigation(employee)
    assert employee_navigation.sections == ["home", "tasks"]

    for role in ("employee", "system_admin", "equipment", "warehouse", "hr"):
        client.force_login(users[role])
        response = client.get(reverse("home"))
        html = response.content.decode()
        navigation = parse_navigation(response)
        assert "pending_finance" not in navigation.items
        assert "depreciation" not in navigation.items
        assert "固定资产原值" not in html
        assert "本月折旧" not in html
        assert " CNY" not in html


def test_management_navigation_is_read_only_and_warehouse_has_no_finance_actions(
    client, navigation_users
):
    _company, users = navigation_users

    client.force_login(users["management"])
    management = client.get(reverse("home"))
    management_navigation = parse_navigation(management)
    assert management_navigation.sections == [
        "home",
        "supplies",
        "finance_reports",
    ]
    assert {"asset_create", "pending_finance", "depreciation"}.isdisjoint(
        management_navigation.items
    )

    client.force_login(users["warehouse"])
    warehouse_navigation = parse_navigation(client.get(reverse("home")))
    assert "assets" in warehouse_navigation.sections
    assert "supplies" in warehouse_navigation.sections
    assert "finance_reports" in warehouse_navigation.sections
    assert "asset_create" in warehouse_navigation.items
    assert {"pending_finance", "depreciation", "tplus"}.isdisjoint(
        warehouse_navigation.items
    )


def test_main_navigation_links_reach_an_authorized_page(client, navigation_users):
    _company, users = navigation_users
    for role, user in users.items():
        client.force_login(user)
        navigation = parse_navigation(client.get(reverse("home")))
        for href in dict.fromkeys(navigation.hrefs):
            split = urlsplit(href)
            response = client.get(
                split.path + (f"?{split.query}" if split.query else ""),
                follow=True,
            )
            assert response.status_code == 200, (role, href, response.status_code)


def test_global_asset_search_is_available_only_for_asset_scoped_navigation(
    client, navigation_users
):
    _company, users = navigation_users
    expected_roles = {
        "finance",
        "equipment",
        "department_manager",
        "employee",
        "warehouse",
    }

    for role, user in users.items():
        client.force_login(user)
        topbar = parse_topbar(client.get(reverse("home")))
        if role in expected_roles:
            assert len(topbar.search_forms) == 1
            assert topbar.search_forms[0]["method"] == "get"
            assert topbar.search_forms[0]["action"] == reverse("assets:asset-list")
            assert [field.get("name") for field in topbar.search_inputs] == ["q"]
            assert client.get(
                topbar.search_forms[0]["action"],
                {"q": "不存在的资产"},
            ).status_code == 200
        else:
            assert topbar.search_forms == []
            assert topbar.search_inputs == []


def test_asset_create_entry_uses_scope_aware_application_permission(
    client, navigation_users
):
    company, users = navigation_users
    expected_roles = {"finance", "equipment", "department_manager", "warehouse"}

    for role, user in users.items():
        client.force_login(user)
        response = client.get(reverse("home"))
        navigation = parse_navigation(response)
        topbar = parse_topbar(response)
        assert ("asset_create" in navigation.items) is (role in expected_roles)
        assert bool(topbar.create_hrefs) is (role in expected_roles)
        if role in expected_roles:
            assert topbar.create_hrefs == [reverse("assets:asset-create")]

    unscoped_manager = make_user("nav-unscoped-manager", "department_manager")
    client.force_login(unscoped_manager)
    response = client.get(reverse("home"))
    navigation = parse_navigation(response)
    topbar = parse_topbar(response)
    assert company is not None
    assert "asset_create" not in navigation.items
    assert topbar.create_hrefs == []


def test_individual_durable_redirects_keep_asset_navigation_and_breadcrumb(
    client, navigation_users
):
    _company, users = navigation_users
    client.force_login(users["finance"])

    cases = (
        ("supplies:individual-durable-list", "逐件低值耐用品"),
        ("supplies:individual-durable-create", "新增逐件低值耐用品"),
    )
    for view_name, page_label in cases:
        response = client.get(reverse(view_name), follow=True)
        assert response.status_code == 200
        navigation = parse_navigation(response)
        assert navigation.active_sections == ["assets"]
        assert navigation.active_items == ["individual_durables"]
        html = response.content.decode()
        assert 'aria-label="面包屑"' in html
        assert f">{page_label}</li>" in html
        assert ">资产管理</a>" in html


def test_navigation_action_entries_do_not_exceed_role_permissions(
    client, navigation_users
):
    _company, users = navigation_users
    expected_asset_create = {
        "finance",
        "equipment",
        "department_manager",
        "warehouse",
    }
    expected_finance_actions = {"finance"}
    expected_supply_master_actions = {"system_admin", "finance", "warehouse"}

    for role, user in users.items():
        client.force_login(user)
        items = set(parse_navigation(client.get(reverse("home"))).items)
        assert ("asset_create" in items) is (role in expected_asset_create)
        assert ({"pending_finance", "depreciation"} <= items) is (
            role in expected_finance_actions
        )
        assert ("tplus" in items) is (role in expected_finance_actions)
        assert ({"supply_category", "supply_warehouse"} <= items) is (
            role in expected_supply_master_actions
        )


def test_navigation_uses_business_facing_chinese_terms(client, navigation_users):
    _company, users = navigation_users
    client.force_login(users["finance"])
    html = client.get(reverse("home")).content.decode()
    for label in (
        "资产管理",
        "办公用品与低值品",
        "我的工作",
        "报表与财务",
        "基础资料与设置",
        "耐用品保管",
        "逐件低值耐用品",
        "操作日志",
    ):
        assert label in html
    for old_label in (
        "员工／部门保管",
        "逐件低值资产",
        "操作审计",
    ):
        assert old_label not in html


def test_report_main_entry_is_unique_and_routes_to_supply_reports(
    client, navigation_users
):
    _company, users = navigation_users
    client.force_login(users["finance"])
    home_navigation = parse_navigation(client.get(reverse("home")))
    assert home_navigation.items.count("report_center") == 1
    assert "supply_report" not in home_navigation.items

    report_center = client.get(reverse("reports:report-center"))
    html = report_center.content.decode()
    assert html.count('data-report-entry="supplies"') == 1
    supply_index = client.get(reverse("reports:supply-report-index"))
    assert supply_index.status_code == 200
    assert "办公用品与低值品报表" in supply_index.content.decode()


def test_task_and_settings_hubs_keep_previous_business_pages_reachable(
    client, navigation_users
):
    _company, users = navigation_users

    client.force_login(users["employee"])
    task_html = client.get(reverse("task-center")).content.decode()
    for url_name in (
        "assets:asset-list",
        "supplies:my-custodies",
        "inventory:task-list",
        "supplies:count-task-list",
        "maintenance:due-list",
        "offboarding:clearance-list",
    ):
        assert reverse(url_name) in task_html

    client.force_login(users["warehouse"])
    settings_html = client.get(reverse("settings-center")).content.decode()
    assert reverse("supplies:category-list") in settings_html
    assert reverse("supplies:warehouse-list") in settings_html
    assert reverse("imports:home") in settings_html

    client.force_login(users["finance"])
    asset_navigation = parse_navigation(client.get(reverse("home")))
    assert "individual_durables" in asset_navigation.items
    assert reverse("supplies:individual-durable-list") in asset_navigation.hrefs
