from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.masterdata.forms import AssetCodingSchemeForm, AssetCodingSegmentForm
from apps.masterdata.models import AssetCodingScheme, IssuedCode, SequenceCounter
from tests.test_sprint2_support import make_active, make_company, make_draft, make_user


pytestmark = pytest.mark.django_db


def test_forms_never_expose_format_string_or_custom_field_choices():
    company = make_company()
    admin = make_user("admin", "system_admin")
    scheme_form = AssetCodingSchemeForm(actor=admin, company=company)
    segment_form = AssetCodingSegmentForm()

    assert "format_string" not in scheme_form.fields
    assert "format_string" not in segment_form.fields
    assert "custom_field" not in scheme_form.fields
    assert "custom_field" not in segment_form.fields
    values = {value for value, _label in segment_form.fields["segment_type"].choices}
    assert "custom_field" not in values


def test_create_endpoint_rejects_hidden_format_and_custom_field_payloads(client):
    make_company()
    admin = make_user("admin", "system_admin")
    client.force_login(admin)
    base = {
        "scheme_key": "HTTP",
        "name": "HTTP",
        "description": "",
        "reset_mode": "never",
        "sequence_start": "1",
        "category_scope_level": "",
        "effective_from": timezone.localdate().isoformat(),
        "effective_to": "",
        "segments-TOTAL_FORMS": "1",
        "segments-INITIAL_FORMS": "0",
        "segments-MIN_NUM_FORMS": "1",
        "segments-MAX_NUM_FORMS": "1000",
        "segments-0-sequence_order": "1",
        "segments-0-segment_type": "sequence",
        "segments-0-fixed_value": "",
        "segments-0-sequence_length": "4",
        "segments-0-zero_pad": "True",
    }

    hidden_format = client.post(
        reverse("masterdata:coding-scheme-create"),
        {**base, "format_string": "YYYY", "segments-0-format_string": "YYYY"},
    )
    # Unknown form keys are ignored, but the server constructs format_string=None;
    # the injected value can never reach either the Service or the database.
    assert hidden_format.status_code == 302
    created = AssetCodingScheme.objects.get(scheme_key="HTTP")
    assert created.segments.get().format_string is None

    custom = client.post(
        reverse("masterdata:coding-scheme-create"),
        {
            **base,
            "scheme_key": "CUSTOM-HTTP",
            "segments-0-segment_type": "custom_field",
            "segments-0-fixed_value": "anything",
            "segments-0-sequence_length": "",
            "segments-0-zero_pad": "",
        },
    )
    assert custom.status_code == 200
    assert not AssetCodingScheme.objects.filter(scheme_key="CUSTOM-HTTP").exists()
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0


def test_finance_and_management_can_view_but_cannot_create_edit_or_act(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    scheme = make_draft(actor=admin, company=company, key="VIEW")

    for index, role in enumerate(("finance", "management"), start=1):
        viewer = make_user(f"viewer-{index}", role)
        client.force_login(viewer)
        assert client.get(reverse("masterdata:coding-scheme-list")).status_code == 200
        assert client.get(
            reverse("masterdata:coding-scheme-detail", args=[scheme.pk])
        ).status_code == 200
        assert client.get(reverse("masterdata:coding-scheme-create")).status_code == 403
        assert client.post(
            reverse("masterdata:coding-scheme-activate", args=[scheme.pk])
        ).status_code == 403


def test_ordinary_user_cannot_view_or_mutate_coding_configuration(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    ordinary = make_user("ordinary", "employee")
    scheme = make_draft(actor=admin, company=company, key="DENY")
    client.force_login(ordinary)

    assert client.get(reverse("masterdata:coding-scheme-list")).status_code == 403
    assert client.get(
        reverse("masterdata:coding-scheme-detail", args=[scheme.pk])
    ).status_code == 403
    assert client.post(
        reverse("masterdata:coding-scheme-clone", args=[scheme.pk])
    ).status_code == 403


def test_mutating_coding_actions_are_post_only(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    scheme = make_draft(actor=admin, company=company, key="POST-ONLY")
    client.force_login(admin)

    action_urls = (
        "masterdata:coding-scheme-activate",
        "masterdata:coding-scheme-retire",
        "masterdata:coding-scheme-set-default",
        "masterdata:coding-scheme-clone",
    )
    for action_url in action_urls:
        assert client.get(
            reverse(action_url, args=[scheme.pk])
        ).status_code == 405


def test_preview_page_and_ten_examples_are_non_consuming_and_nonofficial(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    scheme = make_draft(
        actor=admin, company=company, key="PREVIEW-HTTP", sequence_start=7
    )
    client.force_login(admin)
    before = (SequenceCounter.objects.count(), IssuedCode.objects.count())

    one = client.get(reverse("masterdata:coding-scheme-detail", args=[scheme.pk]))
    ten = client.get(
        reverse("masterdata:coding-scheme-detail", args=[scheme.pk]), {"examples": "10"}
    )

    assert one.status_code == ten.status_code == 200
    assert len(one.context["examples"]) == 1
    assert one.context["examples"][0] == "0007"
    assert ten.context["examples"] == [f"{value:04d}" for value in range(7, 17)]
    content = ten.content.decode()
    assert "正式资产发号尚未启用" in content
    assert "仅预览" in content
    assert "生成 10 个示例" in content
    assert "正式生成编号" not in content
    assert (SequenceCounter.objects.count(), IssuedCode.objects.count()) == before == (0, 0)


def test_coding_pages_do_not_render_hidden_configuration_inputs(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    client.force_login(admin)

    response = client.get(reverse("masterdata:coding-scheme-create"))

    assert response.status_code == 200
    content = response.content.decode()
    assert 'name="format_string"' not in content
    assert 'name="custom_field"' not in content
    assert 'value="custom_field"' not in content
    # Explanatory prose may name the unavailable fields; no accepting input may.
    assert 'segments-0-format_string' not in content


def test_setup_step_six_is_readable_by_view_role_but_refresh_requires_admin(client):
    company = make_company()
    admin = make_user("admin", "system_admin")
    viewer = make_user("finance-viewer", "finance")
    active = make_active(actor=admin, company=company, key="SETUP-HTTP")
    from apps.coding.services import set_default_scheme

    set_default_scheme(actor=admin, scheme=active)
    client.force_login(viewer)

    response = client.get(reverse("masterdata:setup-step", args=[6]))
    # Sprint 4 finance users can enter the setup shell for their own step 7,
    # and may inspect step 6, but cannot refresh the system_admin-owned step.
    assert response.status_code == 200
    assert client.post(reverse("masterdata:setup-step", args=[6])).status_code == 403

    client.force_login(admin)
    response = client.get(reverse("masterdata:setup-step", args=[6]))
    assert response.status_code == 200
    assert response.context["step"]["number"] == 6
    assert response.context["step"]["complete"]
    assert not response.context["setting"].initialization_completed
    assert client.post(reverse("masterdata:setup-step", args=[6])).status_code == 302


def test_cross_company_scheme_id_is_not_disclosed_or_mutated_by_view(client):
    current = make_company()
    other = make_company("C2", active=False)
    admin = make_user("admin", "system_admin")
    foreign = AssetCodingScheme.objects.create(
        company=other,
        name="foreign",
        scheme_key="FOREIGN-HTTP",
        version=1,
        status="draft",
        reset_mode="never",
        sequence_start=1,
        effective_from=timezone.localdate(),
    )
    client.force_login(admin)

    assert client.get(
        reverse("masterdata:coding-scheme-detail", args=[foreign.pk])
    ).status_code == 404
    assert client.post(
        reverse("masterdata:coding-scheme-activate", args=[foreign.pk])
    ).status_code == 404
    foreign.refresh_from_db()
    assert foreign.status == "draft"
    assert current.is_active
