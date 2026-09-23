import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset, AssetIdentity
from apps.coding.presets import activate_standard_version, install_standard_coding_rule
from apps.coding.services import clone_scheme
from apps.masterdata.models import AssetCategory, AssetCodingScheme, IssuedCode, SequenceCounter
from tests.test_sprint3_support import make_category
from tests.test_sprint4_acceptance import _base_context
from tests.test_unified_asset_identity import context, physical_data, registered

pytestmark = pytest.mark.django_db(transaction=True)


def post_data(context, **overrides):
    values = physical_data(context)
    values.update({"quantity": 1, "idempotency_key": "unified-http-create", "asset_action": "register"})
    values.update(overrides)
    return {key: getattr(value, "pk", value) for key, value in values.items()}


def test_standard_setup_get_is_read_only_and_post_adopts_dictionary(client):
    ctx = _base_context("STDSETUP", initialize=False)
    client.force_login(ctx["admin"])
    before = (AssetCategory.objects.count(), AssetCodingScheme.objects.count())
    url = reverse("masterdata:standard-coding-setup")
    assert client.get(url).status_code == 200
    assert (AssetCategory.objects.count(), AssetCodingScheme.objects.count()) == before
    assert client.post(url, {"include_categories": "on"}).status_code == 302
    assert AssetCategory.objects.filter(code__in=("01","02","03","04","05","06","07","08","09","99")).count() == 10
    rule = AssetCodingScheme.objects.get(scheme_key="unified_asset_identity")
    assert rule.is_default and rule.status == "active"
    assert client.get(reverse("masterdata:coding-scheme-detail", args=[rule.pk])).status_code == 200
    assert client.post(url, {"include_categories": "on"}).status_code == 302
    assert AssetCodingScheme.objects.filter(scheme_key="unified_asset_identity").count() == 1
    assert not IssuedCode.objects.exists()


def test_standard_setup_does_not_overwrite_conflicting_category(client):
    ctx = _base_context("STDCONFLICT", initialize=False)
    category = make_category(ctx["company"], "02")
    category.name = "原有其他含义"
    category.save()
    before = AssetCategory.objects.count()
    client.force_login(ctx["admin"])
    response = client.post(reverse("masterdata:standard-coding-setup"), {"include_categories": "on"})
    assert response.status_code == 200
    assert "与文档大类不一致" in response.content.decode()
    assert AssetCategory.objects.count() == before
    assert not AssetCodingScheme.objects.filter(scheme_key="unified_asset_identity").exists()
    category.refresh_from_db()
    assert category.name == "原有其他含义"


def test_equipment_cannot_adopt_coding_rules(client, context):
    client.force_login(context["equipment"])
    assert client.post(reverse("masterdata:standard-coding-setup"), {"include_categories":"on"}).status_code == 403


def test_live_preview_does_not_create_counter_or_identity(client, context):
    client.force_login(context["equipment"])
    values = {"category": context["category"].pk, "department": context["department"].pk,
              "management_attribute": "FA", "acquisition_date": "2020-08-01"}
    url = reverse("assets:asset-code-preview")
    assert client.get(url, values).json()["code"] == "FA-02-2020-000001-00"
    assert client.get(url, values).json()["code"] == "FA-02-2020-000001-00"
    assert not SequenceCounter.objects.exists()
    assert not Asset.objects.exists()
    registered(context)
    assert client.get(url, values).json()["code"] == "FA-02-2020-000002-00"
    assert IssuedCode.objects.count() == 1


def test_preview_needs_no_free_text_evidence_but_registration_does(client, context):
    client.force_login(context["equipment"])
    query = {"category":context["category"].pk, "department":context["department"].pk,
             "management_attribute":"FA", "coding_year":"2019"}
    response = client.get(reverse("assets:asset-code-preview"), query)
    assert response.status_code == 200 and response.json()["code"] == "FA-02-2019-000001-00"
    response = client.post(reverse("assets:asset-create"), post_data(context, coding_year=2019))
    assert response.status_code == 200
    assert response.context["form"].errors.get("coding_year_note")
    assert not Asset.objects.exists() and not IssuedCode.objects.exists()


def test_new_asset_ui_registers_and_finance_page_explains_independence(client, context):
    client.force_login(context["equipment"])
    page = client.get(reverse("assets:asset-create"))
    assert page.status_code == 200
    assert page.context["form"].fields["management_attribute"].required
    assert "data-asset-identity-preview" in page.content.decode()
    response = client.post(reverse("assets:asset-create"), post_data(context))
    assert response.status_code == 302
    asset = Asset.objects.get()
    assert asset.asset_code == "FA-02-2020-000001-00"
    detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
    assert detail.status_code == 200
    assert "新增组件" in detail.content.decode()
    client.force_login(context["finance"])
    finance = client.get(reverse("finance:finance-confirm", args=[asset.pk]))
    assert finance.status_code == 200
    assert "选择不同的会计认定不会改变资产编号或二维码" in finance.content.decode()


def test_component_link_prefills_and_registers_with_shared_number(client, context):
    parent = registered(context)
    client.force_login(context["equipment"])
    page = client.get(reverse("assets:asset-create"), {"component_of": str(parent.pk)})
    assert page.status_code == 200
    assert page.context["form"].initial["component_of"].pk == parent.pk
    response = client.post(reverse("assets:asset-create"), post_data(context,
        idempotency_key="child-http", component_of=str(parent.pk), management_attribute=""))
    assert response.status_code == 302
    child = Asset.objects.get(component_of=parent)
    assert child.asset_code == "FA-02-2020-000001-01"
    assert AssetIdentity.objects.count() == 2


def test_standard_version_replacement_keeps_counter_and_initialization(context):
    first = registered(context)
    old = context["standard_scheme"]
    newer = clone_scheme(actor=context["admin"], scheme=old,
                         data={"effective_from": timezone.localdate()})
    activate_standard_version(actor=context["admin"], scheme=newer)
    second = registered(context, "after-version-change")
    assert second.asset_code == "FA-02-2020-000002-00"
    assert SequenceCounter.objects.count() == 1
    assert SequenceCounter.objects.get().coding_scheme_id == old.pk
    assert second.current_issued_code.coding_scheme_id == newer.pk
    assert context["company"].initialization_setting.initialization_completed
    first.refresh_from_db()
    assert first.asset_code == "FA-02-2020-000001-00"


def test_version_installation_does_not_require_resetting_a_completed_company(context):
    before = context["company"].initialization_setting.completed_at
    install_standard_coding_rule(actor=context["admin"], company=context["company"])
    context["company"].refresh_from_db()
    assert context["company"].initialization_setting.initialization_completed
    assert context["company"].initialization_setting.completed_at == before


def test_real_initialization_allows_finance_configuration_to_follow_later(client):
    from apps.masterdata.services import complete_initialization, compute_initialization_progress
    from apps.finance.models import DepreciationPolicy, AssetFinance
    from tests.test_unified_asset_identity import registered
    ctx = _base_context("PHYSICALFIRST", initialize=False, include_policy=False)
    progress = compute_initialization_progress(ctx["company"])
    assert not progress["finance_rules_configured"]
    assert all(value for key, value in progress.items() if key != "finance_rules_configured")
    setting = complete_initialization(company=ctx["company"], actor=ctx["admin"])
    assert setting.initialization_completed and not setting.finance_rules_configured
    assert not DepreciationPolicy.objects.exists()
    asset = registered(ctx)
    assert asset.asset_code and not AssetFinance.objects.exists()
    client.force_login(ctx["admin"])
    page = client.get(reverse("masterdata:setup"))
    assert page.status_code == 200
    assert "可后补" in page.content.decode()
