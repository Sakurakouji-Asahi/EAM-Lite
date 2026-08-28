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
        self.current_group = None

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
        if tag == "details":
            self.current_group = None
        if tag == "nav":
            self.in_navigation = False
            self.completed = True


def parse_navigation(response):
    parser = DesktopNavigationParser()
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
        "system_admin": ["home", "settings"],
        "finance": [
            "home",
            "assets",
            "supplies",
            "tasks",
            "finance_reports",
            "settings",
        ],
        "equipment": ["home", "assets", "tasks", "settings"],
        "department_manager": ["home", "assets", "tasks", "finance_reports"],
        "employee": ["home", "tasks"],
        "warehouse": ["home", "supplies", "tasks", "settings"],
        "hr": ["home", "tasks", "settings"],
        "management": ["home", "finance_reports"],
    }

    for role, user in users.items():
        client.force_login(user)
        response = client.get(reverse("home"))
        assert response.status_code == 200
        navigation = parse_navigation(response)
        assert navigation.sections == expected[role]
        assert len(navigation.sections) <= 6
        assert all(count > 0 for count in navigation.group_item_counts.values())


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
                "supply_overview",
                "supply_stock",
                "supply_documents",
                "supply_custodies",
                "task_center",
                "task_labels",
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
                "task_center",
                "asset_category",
                "location",
                "supply_item",
                "imports",
            },
            "absent": {"pending_finance", "tplus", "supply_stock"},
        },
        "hr": {
            "present": {"task_center", "offboarding", "employee", "imports", "audit"},
            "absent": {"asset_ledger", "supply_stock", "pending_finance"},
        },
        "management": {
            "present": {"report_center"},
            "absent": {"asset_create", "pending_finance", "depreciation", "imports"},
        },
        "system_admin": {
            "present": {
                "settings_center",
                "coding_scheme",
                "user_permissions",
                "imports",
                "audit",
                "backup",
            },
            "absent": {"pending_finance", "depreciation", "asset_ledger"},
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
        ("finance", "reports:report-center", "finance_reports", "report_center"),
        ("equipment", "maintenance:plan-list", "assets", "maintenance_plans"),
        ("equipment", "supplies:item-list", "settings", "supply_item"),
        ("hr", "offboarding:clearance-list", "tasks", "offboarding"),
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


def test_employee_and_system_admin_do_not_receive_finance_navigation_or_values(
    client, navigation_users
):
    _company, users = navigation_users
    for role in ("employee", "system_admin"):
        client.force_login(users[role])
        response = client.get(reverse("home"))
        html = response.content.decode()
        navigation = parse_navigation(response)
        assert "finance_reports" not in navigation.sections
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
    assert management_navigation.sections == ["home", "finance_reports"]
    assert {"asset_create", "pending_finance", "depreciation"}.isdisjoint(
        management_navigation.items
    )

    client.force_login(users["warehouse"])
    warehouse_navigation = parse_navigation(client.get(reverse("home")))
    assert "supplies" in warehouse_navigation.sections
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
