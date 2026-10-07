"""Directory links preserve applied filters and the existing personnel scope."""
from urllib.parse import parse_qs, urlsplit

import pytest
from django.http import QueryDict
from django.urls import reverse

from apps.masterdata.models import Employee
from tests.test_sprint3_support import make_company, make_department, make_employee, make_user, grant_scope


@pytest.fixture
def directory_context():
    company = make_company("EMPLOYEEWORKSPACE")
    root = make_department(company, "ROOT")
    child = make_department(company, "CHILD", parent=root)
    private = make_department(company, "PRIVATE")
    admin = make_user("employee-workspace-admin", "system_admin")
    employee_user = make_user("employee-workspace-private-login", "employee")
    employee = make_employee(company, root, "VISIBLE", user=employee_user)
    employee.mobile = "13800008888"
    employee.remark = "岗位：生产助理\n原表人员标记：正式人员\n原始说明 <需核对>"
    employee.save(update_fields=["mobile", "remark"])
    return {"company": company, "root": root, "child": child, "private": private, "admin": admin, "employee": employee}


@pytest.mark.django_db
def test_individual_filter_removal_keeps_other_conditions_and_phone_is_read_only(client, directory_context):
    context = directory_context
    before = list(Employee.objects.order_by("pk").values())
    client.force_login(context["admin"])
    params = {"q": "13800008888", "department": str(context["root"].pk), "position": "生产助理",
              "source_mark": "正式人员", "status": "active", "employment_status": "active", "page": "2"}
    page = client.get(reverse("masterdata:employee-list"), params)
    assert page.status_code == 200 and page.context["page_obj"].paginator.count == 1
    chips = {item["label"]: item for item in page.context["employee_filter_chips"]}
    assert chips["部门（含下级）"]["value"] == context["root"].name
    no_search = QueryDict(urlsplit(chips["关键词"]["remove_url"]).query).dict()
    assert no_search == {key: value for key, value in params.items() if key not in {"q", "page"}}
    include_disabled = QueryDict(urlsplit(chips["启用状态"]["remove_url"]).query).dict()
    assert include_disabled == {**{key: value for key, value in params.items() if key != "page"}, "status": "all"}
    assert "手机：13800008888" in page.content.decode()
    assert list(Employee.objects.order_by("pk").values()) == before


@pytest.mark.django_db
def test_employment_shortcuts_include_disabled_staff_and_recover_conflicting_filter(client, directory_context):
    context = directory_context
    leaving = make_employee(context["company"], context["child"], "LEAVING", status="leaving", active=False)
    leaving.remark = "岗位：生产助理\n原表人员标记：正式人员"
    leaving.save(update_fields=["remark"])
    client.force_login(context["admin"])
    params = {"q": "员工", "department": context["root"].pk, "position": "生产助理", "source_mark": "正式人员",
              "employment_status": "leaving", "status": "active", "page": 2}
    page = client.get(reverse("masterdata:employee-list"), params)
    assert page.context["page_obj"].paginator.count == 0 and page.context["employee_status_conflict"] is True
    assert "包含停用人员，保留其他条件" in page.content.decode()
    restored = client.get(page.context["employee_include_inactive_url"])
    assert [row.pk for row in restored.context["objects"]] == [leaving.pk]
    assert restored.context["filter_errors"] == [] and restored.context["employee_status_conflict"] is False
    shortcuts = {item["label"]: item for item in page.context["employee_status_shortcuts"]}
    assert QueryDict(urlsplit(shortcuts["在职"]["url"]).query).dict() == {
        "q": "员工", "department": str(context["root"].pk), "position": "生产助理", "source_mark": "正式人员",
        "employment_status": "active", "status": "all"}
    assert [row.pk for row in client.get(shortcuts["在职"]["url"]).context["objects"]] == [context["employee"].pk]


@pytest.mark.django_db
def test_directory_profile_links_and_contact_respect_scope_and_hide_technical_login(client, directory_context):
    context = directory_context
    private = make_employee(context["company"], context["private"], "HIDDEN")
    private.mobile = "13999997777"
    private.remark = "岗位：隐藏岗位\n原表人员标记：隐藏标记"
    private.save(update_fields=["mobile", "remark"])
    manager = make_user("employee-workspace-manager", "department_manager")
    grant_scope(manager, context["company"], context["root"], descendants=False)
    client.force_login(manager)
    page = client.get(reverse("masterdata:employee-list"), {"status": "all"})
    html = page.content.decode()
    assert "13800008888" in html
    assert "13999997777" not in html and "隐藏岗位" not in html and "登录账号" not in html
    detail = client.get(reverse("masterdata:employee-detail", args=[context["employee"].pk]))
    profile = detail.context["employee_directory"]
    assert profile["position"] == "生产助理" and profile["source_mark"] == "正式人员" and profile["mobile"] == "13800008888"
    assert "原始说明 &lt;需核对&gt;" in detail.content.decode() and "<br>" in detail.content.decode()
    assert "employee-workspace-private-login" not in detail.content.decode()
    assert [row.pk for row in client.get(profile["department_url"]).context["objects"]] == [context["employee"].pk]
    assert [row.pk for row in client.get(profile["position_url"]).context["objects"]] == [context["employee"].pk]
    assert client.get(reverse("masterdata:employee-detail", args=[private.pk])).status_code == 404
    rejected = client.get(reverse("masterdata:employee-list"), {"department": private.department_id})
    assert rejected.context["filter_errors"] and not rejected.context["employee_department_valid"]
    assert "所选部门无效或超出范围" in rejected.content.decode()
    assert "13999997777" not in rejected.content.decode()


@pytest.mark.django_db
def test_empty_keyword_filter_can_be_removed_without_losing_department_or_position(client, directory_context):
    context = directory_context
    client.force_login(context["admin"])
    page = client.get(reverse("masterdata:employee-list"), {"q": "不存在的人员", "department": context["root"].pk,
        "position": "生产助理", "source_mark": "正式人员"})
    assert page.context["page_obj"].paginator.count == 0
    assert "可逐项取消上方筛选，保留其他查找条件" in page.content.decode()
    remove_search = next(item["remove_url"] for item in page.context["employee_filter_chips"] if item["label"] == "关键词")
    restored = client.get(remove_search)
    assert [row.pk for row in restored.context["objects"]] == [context["employee"].pk]
    assert parse_qs(urlsplit(remove_search).query)["position"] == ["生产助理"]
