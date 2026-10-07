"""Browser regressions for draft extension fields and their category boundary."""

from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from django.urls import reverse

from apps.assets.models import Asset, AssetRegistration
from apps.masterdata.models import IssuedCode
from tests.test_asset_registration_finance_separation import context, registered
from tests.test_asset_registration_http_reports import browser_data
from tests.test_asset_edit_revision import edit_revision
from tests.test_sprint3_support import (
    grant_scope,
    make_asset,
    make_category,
    make_company,
    make_custom_field,
    make_department,
    make_user,
)


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def required_fields(context):
    return {
        field_type: make_custom_field(
            context["company"], context["category"], f"REQ_{field_type.upper()}",
            field_type, required=True,
            options=["A", "B"] if field_type == "select" else None,
        )
        for field_type in ("text", "decimal", "date", "select", "boolean")
    }


def custom_name(field):
    return f"custom_{field.pk}-value"


def valid_custom_data(fields):
    values = {"text": "资料已核对", "decimal": "0", "date": "2026-10-03", "select": "A", "boolean": "false"}
    return {custom_name(fields[field_type]): value for field_type, value in values.items()}


def draft(context, **kwargs):
    return make_asset(
        actor=context["equipment"], company=context["company"],
        category=context["category"], department=context["department"],
        employee=context["employee"], location=context["location"], **kwargs,
    )


def assert_no_registration():
    assert not AssetRegistration.objects.exists()
    assert not IssuedCode.objects.exists()


class CustomBrowserValues(HTMLParser):
    """Read the values a browser would submit, including a select's default."""

    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.select = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name", "")
        if tag == "input" and name.startswith("custom_"):
            self.values[name] = attrs.get("value", "")
        elif tag == "select":
            self.select = name if name.startswith("custom_") else None
        elif tag == "option" and self.select:
            if self.select not in self.values or "selected" in attrs:
                self.values[self.select] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None


def test_explicit_draft_create_and_edit_allow_missing_required_extensions(client, context, required_fields):
    client.force_login(context["equipment"])
    response = client.post(reverse("assets:asset-create"), browser_data(context, asset_action="draft"))
    assert response.status_code == 302
    asset = Asset.objects.get()
    assert asset.asset_status == "draft" and asset.asset_code is None
    assert not asset.custom_values.exists()
    assert_no_registration()

    edit_url = reverse("assets:asset-edit", args=[asset.pk])
    page = client.get(edit_url)
    assert page.status_code == 200
    assert len(page.context["custom_value_forms"]) == len(required_fields)
    assert all(not form.fields["value"].required for form in page.context["custom_value_forms"])
    response = client.post(edit_url, browser_data(context, asset_action="draft", asset_name="稍后补全扩展字段",
                                               expected_revision=page.context["form"]["expected_revision"].value()))
    assert response.status_code == 302
    asset.refresh_from_db()
    assert asset.asset_name == "稍后补全扩展字段" and asset.asset_code is None
    assert not asset.custom_values.exists()
    assert_no_registration()


@pytest.mark.parametrize("editing", [False, True], ids=["new-draft", "existing-draft"])
def test_draft_rejects_supplied_invalid_types_and_displays_every_error(client, context, required_fields, editing):
    asset = draft(context, custom_values={str(required_fields["text"].pk): "原有资料"}) if editing else None
    original_name = asset.asset_name if asset else None
    url = reverse("assets:asset-edit", args=[asset.pk]) if asset else reverse("assets:asset-create")
    invalid = {"decimal": "不是数字", "date": "2026-13-45", "select": "未批准选项", "boolean": "maybe"}
    payload = browser_data(context, asset_action="draft", asset_name="")
    payload.update({custom_name(required_fields[field_type]): value for field_type, value in invalid.items()})
    client.force_login(context["equipment"])
    if editing:
        payload["expected_revision"] = edit_revision(client, url)
    response = client.post(url, payload)
    assert response.status_code == 200
    assert response.context["form"].errors["asset_name"]
    html = response.content.decode()
    assert 'id="asset-form-errors"' in html
    summary = html.split('id="asset-form-errors"', 1)[1].split("</ul>", 1)[0]
    assert 'href="#id_asset_name"' in summary
    forms = {form.custom_field.field_type: form for form in response.context["custom_value_forms"]}
    assert not forms["text"].errors  # Empty required values remain valid while saving a draft.
    for field_type in invalid:
        field = required_fields[field_type]
        assert forms[field_type].errors["value"]
        assert f'href="#id_{custom_name(field)}"' in summary
        assert field.name in summary
    if asset:
        asset.refresh_from_db()
        assert asset.asset_name == original_name
        assert asset.custom_values.get().value_text == "原有资料"
    else:
        assert not Asset.objects.exists()
    assert_no_registration()


def test_new_registration_still_requires_all_extensions_and_accepts_false_and_zero(client, context, required_fields):
    client.force_login(context["equipment"])
    url = reverse("assets:asset-create")
    response = client.post(url, browser_data(context))
    assert response.status_code == 200
    assert len(response.context["custom_value_forms"]) == len(required_fields)
    assert all(form.errors["value"] for form in response.context["custom_value_forms"])
    assert not Asset.objects.exists()
    assert_no_registration()

    payload = browser_data(context)
    payload.update(valid_custom_data(required_fields))
    response = client.post(url, payload)
    assert response.status_code == 302
    asset = Asset.objects.get()
    assert asset.asset_code and asset.asset_status == "pending_label"
    assert AssetRegistration.objects.count() == IssuedCode.objects.count() == 1
    values = {value.custom_field_id: value for value in asset.custom_values.all()}
    assert values[required_fields["decimal"].pk].value_decimal == Decimal("0")
    assert values[required_fields["boolean"].pk].value_boolean is False
    assert values[required_fields["date"].pk].value_date == date(2026, 10, 3)


def test_incomplete_draft_cannot_register_until_required_extensions_are_completed(client, context, required_fields):
    asset = draft(context)
    client.force_login(context["equipment"])
    submit_url = reverse("assets:asset-submit", args=[asset.pk])
    submit = {"confirm": "on", "idempotency_key": "complete-draft-register"}
    response = client.post(submit_url, submit)
    assert response.status_code == 200 and response.context["form"].errors
    asset.refresh_from_db()
    assert asset.asset_status == "draft" and asset.asset_code is None
    assert_no_registration()

    payload = browser_data(context, asset_action="draft")
    payload.update(valid_custom_data(required_fields))
    payload["expected_revision"] = edit_revision(client, reverse("assets:asset-edit", args=[asset.pk]))
    assert client.post(reverse("assets:asset-edit", args=[asset.pk]), payload).status_code == 302
    assert client.post(submit_url, submit).status_code == 302
    asset.refresh_from_db()
    assert asset.asset_code and asset.asset_status == "pending_label"
    assert AssetRegistration.objects.count() == IssuedCode.objects.count() == 1


def test_edit_page_and_fragment_preserve_saved_false_and_zero_on_browser_submission(client, context, required_fields):
    asset = draft(context, custom_values={
        str(required_fields["boolean"].pk): False,
        str(required_fields["decimal"].pk): Decimal("0"),
    })
    client.force_login(context["equipment"])
    edit_url = reverse("assets:asset-edit", args=[asset.pk])
    page = client.get(edit_url)
    assert page.status_code == 200
    page_values = CustomBrowserValues(page.content.decode()).values
    assert page_values[custom_name(required_fields["boolean"])] == "false"
    assert Decimal(page_values[custom_name(required_fields["decimal"])]) == Decimal("0")
    fragment = client.get(reverse("assets:asset-custom-fields", args=[asset.pk]), {"category": str(context["category"].pk)})
    assert fragment.status_code == 200
    values = CustomBrowserValues(fragment.json()["html"]).values
    assert values[custom_name(required_fields["boolean"])] == "false"
    assert Decimal(values[custom_name(required_fields["decimal"])]) == Decimal("0")
    payload = browser_data(context, asset_action="draft")
    payload.update(page_values)
    payload["expected_revision"] = page.context["form"]["expected_revision"].value()
    assert client.post(edit_url, payload).status_code == 302
    assert asset.custom_values.count() == 2
    assert asset.custom_values.get(custom_field=required_fields["boolean"]).value_boolean is False
    assert asset.custom_values.get(custom_field=required_fields["decimal"]).value_decimal == Decimal("0")


@pytest.mark.parametrize("editing", [False, True], ids=["new-draft", "existing-draft"])
def test_category_switch_uses_only_selected_active_company_fields(client, context, editing):
    old = make_custom_field(context["company"], context["category"], "OLD", "text")
    other_category = make_category(context["company"], "SECOND")
    selected = make_custom_field(context["company"], other_category, "NEW", "text", required=True)
    inactive = make_custom_field(context["company"], other_category, "INACTIVE", "text", active=False)
    foreign_company = make_company("FOREIGN", active=False)
    foreign = make_custom_field(foreign_company, make_category(foreign_company, "FOREIGNCAT"), "FOREIGN", "text")
    asset = draft(context, custom_values={str(old.pk): "旧分类私有资料"}) if editing else None
    client.force_login(context["equipment"])
    url = reverse("assets:asset-edit", args=[asset.pk]) if asset else reverse("assets:asset-create")
    page = client.get(url, {"category": str(other_category.pk)})
    assert page.status_code == 200
    assert page.context["selected_category"] == other_category
    assert str(page.context["form"]["category"].value()) == str(other_category.pk)
    html = page.content.decode()
    assert custom_name(selected) in html
    for field in (old, inactive, foreign):
        assert custom_name(field) not in html

    payload = browser_data(context, category=str(other_category.pk), asset_action="draft")
    payload.update({
        custom_name(selected): "新分类资料", custom_name(old): "伪造旧分类值",
        custom_name(inactive): "伪造停用值", custom_name(foreign): "伪造其他公司值",
    })
    if editing:
        payload["expected_revision"] = page.context["form"]["expected_revision"].value()
    response = client.post(url, payload)
    assert response.status_code == 302
    saved = Asset.objects.get()
    assert saved.category == other_category
    assert saved.custom_values.count() == 1
    value = saved.custom_values.get()
    assert value.custom_field == selected and value.value_text == "新分类资料"
    assert value.company == context["company"]
    assert_no_registration()


def test_fragment_returns_current_saved_values_only_for_requested_category(client, context):
    old = make_custom_field(context["company"], context["category"], "CURRENT", "text")
    category = make_category(context["company"], "SWITCH")
    new = make_custom_field(context["company"], category, "SWITCHED", "text")
    asset = draft(context, custom_values={str(old.pk): "只属于原分类的内容"})
    client.force_login(context["equipment"])
    url = reverse("assets:asset-custom-fields", args=[asset.pk])
    current = client.get(url, {"category": str(context["category"].pk)})
    assert current.status_code == 200
    assert set(current.json()) == {"category", "html"}
    assert current.json()["category"] == str(context["category"].pk)
    assert CustomBrowserValues(current.json()["html"]).values[custom_name(old)] == "只属于原分类的内容"
    assert "no-store" in current.headers["Cache-Control"]
    switched = client.get(url, {"category": str(category.pk)})
    assert switched.status_code == 200
    assert switched.json()["category"] == str(category.pk)
    assert CustomBrowserValues(switched.json()["html"]).values == {custom_name(new): ""}
    assert "只属于原分类的内容" not in switched.json()["html"]
    assert asset.custom_values.get().custom_field == old


def test_fragment_requires_login_get_and_active_current_company_category(client, context):
    inactive = make_category(context["company"], "DISABLED", active=False)
    foreign_company = make_company("EXTERNAL", active=False)
    foreign = make_category(foreign_company, "EXTERNALCAT")
    url = reverse("assets:asset-custom-fields")
    response = client.get(url, {"category": str(context["category"].pk)})
    assert response.status_code == 302 and "/login/" in response.url
    client.force_login(context["equipment"])
    assert client.post(url, {"category": str(context["category"].pk)}).status_code == 405
    for query in ({}, {"category": "not-a-uuid"}, {"category": str(inactive.pk)}, {"category": str(foreign.pk)}):
        assert client.get(url, query).status_code == 404
    assert client.get(url, {"category": str(context["category"].pk)}).status_code == 200


def test_fragment_and_category_changes_cannot_bypass_create_edit_or_department_permissions(client, context):
    asset = draft(context)
    category = make_category(context["company"], "OTHER")
    field = make_custom_field(context["company"], category, "RESTRICTED", "text")
    viewer = make_user("custom-fields-viewer", "management")
    client.force_login(viewer)
    create_url = reverse("assets:asset-custom-fields")
    edit_url = reverse("assets:asset-custom-fields", args=[asset.pk])
    assert client.get(create_url, {"category": str(category.pk)}).status_code == 403
    assert client.get(edit_url, {"category": str(category.pk)}).status_code == 403
    payload = browser_data(context, asset_action="draft", category=str(category.pk))
    payload[custom_name(field)] = "没有写权限"
    assert client.post(reverse("assets:asset-edit", args=[asset.pk]), payload).status_code == 403

    manager = make_user("custom-fields-manager", "department_manager")
    managed_department = make_department(context["company"], "MANAGED")
    grant_scope(manager, context["company"], managed_department)
    client.force_login(manager)
    assert client.get(edit_url, {"category": str(category.pk)}).status_code == 404
    assert client.post(reverse("assets:asset-edit", args=[asset.pk]), payload).status_code == 404
    assert client.post(reverse("assets:asset-create"), payload).status_code == 200
    asset.refresh_from_db()
    assert Asset.objects.count() == 1 and asset.category == context["category"]
    assert not asset.custom_values.exists()
    assert_no_registration()


def test_fragment_cannot_edit_registered_asset(client, context):
    asset = registered(context)
    client.force_login(context["equipment"])
    response = client.get(reverse("assets:asset-custom-fields", args=[asset.pk]), {"category": str(context["category"].pk)})
    assert response.status_code == 403
