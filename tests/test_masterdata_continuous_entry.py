"""Continuous entry carries only an allowed grouping choice and list context."""
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse

from apps.masterdata.models import AssetCategory, Department, Employee, Location
from tests.test_sprint3_support import (
    make_category, make_company, make_department,
    make_location, make_user,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def ctx():
    company = make_company("CONTINUOUS")
    actor = make_user("continuous-admin", "system_admin", "hr", "equipment")
    return {"company": company, "actor": actor}


@pytest.mark.parametrize("slug,model,field,code,factory", [
    ("department", Department, "parent", "code", make_department),
    ("employee", Employee, "department", "employee_no", make_department),
    ("location", Location, "parent", "code", make_location),
    ("category", AssetCategory, "parent", "code", make_category),
])
def test_save_continue_clears_identity_keeps_group_and_returns_to_source(client, ctx, slug, model, field, code, factory):
    parent = factory(ctx["company"], "GROUP")
    client.force_login(ctx["actor"])
    source = reverse(f"masterdata:{slug}-list") + "?" + urlencode({"q": "同组", "page": 2})
    create = reverse(f"masterdata:{slug}-create")
    page = client.get(create, {field: parent.pk, "return_to": source})
    assert page.status_code == 200 and page.context["form"][field].value() == parent.pk
    assert "保存并继续新增" in page.content.decode()
    data = {"name": "同组 第一条", code: "", field: str(parent.pk), "return_to": source,
            "employment_status": "active", "action": "continue"}
    result = client.post(create, data)
    assert result.status_code == 302, result.context["form"].errors
    saved = model.objects.get(company=ctx["company"], name=data["name"])
    assert getattr(saved, code)
    assert getattr(saved, f"{field}_id") == parent.pk
    query = parse_qs(urlsplit(result.url).query)
    assert query == {"return_to": [source], field: [str(parent.pk)]}
    following = client.get(result.url)
    form = following.context["form"]
    assert not form.is_bound and form[field].value() == parent.pk
    assert form["name"].value() in (None, "") and form[code].value() in (None, "")
    if slug == "employee":
        assert form["mobile"].value() in (None, "") and form["hire_date"].value() in (None, "")
    second = client.post(result.url, {**data, "name": "同组 第二条", "action": "save"})
    assert second.status_code == 302 and second.url == source
    assert model.objects.filter(company=ctx["company"], name__startswith="同组 ").count() == 2


def test_context_rejects_foreign_inactive_and_external_values_and_preserves_error_input(client, ctx):
    parent = make_department(ctx["company"], "VALID")
    inactive = make_department(ctx["company"], "INACTIVE", active=False)
    foreign = make_company("FOREIGN", active=False)
    other = make_department(foreign, "FOREIGN")
    client.force_login(ctx["actor"])
    url = reverse("masterdata:employee-create")
    for bad in (other.pk, inactive.pk, "bad-id", "9" * 25):
        page = client.get(url, {"department": bad, "return_to": "https://outside.example/employees/"})
        assert page.status_code == 200
        assert page.context["form"]["department"].value() is None
        assert "请重新选择" in page.context["masterdata_context_warning"]
        assert page.context["cancel_url"] == reverse("masterdata:employee-list")
        assert not page.context["masterdata_return_to"]
    source = reverse("masterdata:employee-list") + "?q=保留"
    invalid = client.post(url, {"department": parent.pk, "name": "保留当前输入", "employment_status": "active",
        "hire_date": "not-a-date", "return_to": source, "action": "continue"})
    assert invalid.status_code == 200 and "hire_date" in invalid.context["form"].errors
    assert invalid.context["form"]["name"].value() == "保留当前输入"
    assert invalid.context["masterdata_return_to"] == source
    assert not Employee.objects.filter(name="保留当前输入").exists()
    employee = make_user("continuous-readonly", "employee")
    client.force_login(employee)
    assert client.get(url, {"department": parent.pk}).status_code == 403


def test_regular_create_still_opens_detail_and_edit_retains_an_explicit_list(client, ctx):
    department = make_department(ctx["company"], "EDITGROUP")
    client.force_login(ctx["actor"])
    url = reverse("masterdata:employee-create")
    data = {"name": "待编辑人员", "employee_no": "", "department": department.pk, "employment_status": "active"}
    saved = client.post(url, data)
    assert saved.status_code == 302, saved.context["form"].errors
    employee = Employee.objects.get(name=data["name"])
    assert saved.url == reverse("masterdata:employee-detail", args=[employee.pk])
    source = reverse("masterdata:employee-list") + "?" + urlencode({"q": "待编辑", "status": "all"})
    detail = client.get(saved.url, {"return_to": source})
    edit_url = detail.context["edit_url"]
    assert parse_qs(urlsplit(edit_url).query) == {"return_to": [source]}
    assert f'href="{edit_url.replace("&", "&amp;")}"' in detail.content.decode()
    editing = client.get(edit_url)
    assert not editing.context["masterdata_can_continue"]
    result = client.post(edit_url, {**data, "employee_no": employee.employee_no,
                                  "name": "已编辑人员", "return_to": source, "action": "continue"})
    assert result.url == source and Employee.objects.filter(company=ctx["company"]).count() == 1
    employee.refresh_from_db()
    assert employee.name == "已编辑人员"
