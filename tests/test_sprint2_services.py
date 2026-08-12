from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.coding.domain import preview_codes
from apps.coding.services import (
    activate_scheme,
    clone_scheme,
    create_scheme,
    replace_segments,
    retire_scheme,
    set_category_default_scheme,
    set_default_scheme,
    update_draft_scheme,
)
from apps.masterdata.models import (
    AssetCategory,
    AssetCodingScheme,
    IssuedCode,
    SequenceCounter,
)
from apps.masterdata.services import (
    compute_initialization_progress,
    create_asset_category,
    refresh_initialization_progress,
)
from apps.masterdata.forms import AssetCategoryForm
from tests.test_sprint2_support import (
    make_active,
    make_company,
    make_draft,
    make_user,
    sequence_segments,
    standard_scheme_data,
)


pytestmark = pytest.mark.django_db


def preview_context(company):
    return {"company": company, "effective_date": timezone.localdate()}


def make_category(company, code="EQ"):
    return AssetCategory.objects.create(
        company=company,
        code=code,
        normalized_code=code.casefold(),
        name=code,
        category_type="equipment",
    )


def issued_fixture(*, company, scheme):
    """Direct fixture only; no production issuance service exists in Sprint 2."""
    return IssuedCode.objects.create(
        company=company,
        coding_scheme=scheme,
        scope_key="fixture-only",
        sequence_value=scheme.sequence_start,
        display_code=f"T-{scheme.sequence_start}",
        normalized_code=f"t-{scheme.sequence_start}",
        effective_date=timezone.localdate(),
        idempotency_key=f"fixture-{scheme.pk}",
    )


def test_create_scheme_is_transactional_audited_and_never_allocates():
    company = make_company()
    admin = make_user("admin", "system_admin")

    scheme = make_draft(actor=admin, company=company, key="CREATE", sequence_start=7)

    assert scheme.status == "draft"
    assert scheme.version == 1
    assert list(scheme.segments.values_list("segment_type", flat=True)) == ["sequence"]
    assert AuditLog.objects.filter(
        company=company,
        user=admin,
        action="coding_scheme_create",
        object_id=str(scheme.pk),
    ).exists()
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0


def test_create_scheme_rolls_back_scheme_segments_and_audit_on_invalid_matrix():
    company = make_company()
    admin = make_user("admin", "system_admin")
    before_audits = AuditLog.objects.count()
    invalid_segments = sequence_segments()
    invalid_segments[0]["format_string"] = "YYYY"

    with pytest.raises(ValidationError):
        create_scheme(
            actor=admin,
            company=company,
            data=standard_scheme_data(key="ROLLBACK"),
            segments=invalid_segments,
        )

    assert not AssetCodingScheme.objects.filter(scheme_key="ROLLBACK").exists()
    assert AuditLog.objects.count() == before_audits
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0


@pytest.mark.parametrize(
    "operation",
    (
        "create",
        "update",
        "replace",
        "clone",
        "activate",
        "retire",
        "default",
        "category",
    ),
)
def test_every_mutating_service_rejects_non_system_admin(operation):
    company = make_company()
    admin = make_user("admin", "system_admin")
    unauthorized = make_user(f"finance-{operation}", "finance")
    draft = make_draft(actor=admin, company=company, key="PERMISSION")
    active = make_active(actor=admin, company=company, key="ACTIVE-PERMISSION")
    category = make_category(company)

    calls = {
        "create": lambda: create_scheme(
            actor=unauthorized,
            company=company,
            data=standard_scheme_data(key="FORBIDDEN"),
            segments=sequence_segments(),
        ),
        "update": lambda: update_draft_scheme(
            actor=unauthorized, scheme=draft, data={"name": "forbidden"}
        ),
        "replace": lambda: replace_segments(
            actor=unauthorized, scheme=draft, segments=sequence_segments(length=5)
        ),
        "clone": lambda: clone_scheme(actor=unauthorized, scheme=draft),
        "activate": lambda: activate_scheme(actor=unauthorized, scheme=draft),
        "retire": lambda: retire_scheme(actor=unauthorized, scheme=active),
        "default": lambda: set_default_scheme(actor=unauthorized, scheme=active),
        "category": lambda: set_category_default_scheme(
            actor=unauthorized, category=category, scheme=active
        ),
    }

    with pytest.raises(PermissionDenied):
        calls[operation]()


def test_cross_company_service_access_is_rejected():
    current = make_company()
    other = make_company("C2", active=False)
    admin = make_user("admin", "system_admin")
    foreign = AssetCodingScheme.objects.create(
        company=other,
        name="foreign",
        scheme_key="FOREIGN",
        version=1,
        status="draft",
        reset_mode="never",
        sequence_start=1,
        effective_from=timezone.localdate(),
    )

    with pytest.raises(PermissionDenied):
        update_draft_scheme(actor=admin, scheme=foreign, data={"name": "hacked"})

    foreign.refresh_from_db()
    assert foreign.name == "foreign"
    assert current.is_active


def test_service_rejects_format_string_custom_field_and_unknown_fields():
    company = make_company()
    admin = make_user("admin", "system_admin")
    base = standard_scheme_data(key="NO-HIDDEN")

    with pytest.raises(ValidationError):
        create_scheme(
            actor=admin,
            company=company,
            data={**base, "format_string": "YYYY"},
            segments=sequence_segments(),
        )
    with pytest.raises(ValidationError):
        create_scheme(
            actor=admin,
            company=company,
            data=base,
            segments=[
                {
                    "sequence_order": 1,
                    "segment_type": "custom_field",
                    "fixed_value": "anything",
                    "format_string": None,
                    "sequence_length": None,
                    "zero_pad": None,
                }
            ],
        )


def test_replace_segments_is_atomic_and_audited():
    company = make_company()
    admin = make_user("admin", "system_admin")
    scheme = make_draft(actor=admin, company=company, key="SEGMENTS")
    original = list(scheme.segments.values("segment_type", "sequence_length"))
    invalid = sequence_segments(length=5)
    invalid.append(
        {
            "sequence_order": 2,
            "segment_type": "sequence",
            "fixed_value": None,
            "format_string": None,
            "sequence_length": 5,
            "zero_pad": True,
        }
    )

    with pytest.raises(ValidationError):
        replace_segments(actor=admin, scheme=scheme, segments=invalid)
    assert list(scheme.segments.values("segment_type", "sequence_length")) == original

    replace_segments(
        actor=admin, scheme=scheme, segments=sequence_segments(length=5, zero_pad=False)
    )
    assert scheme.segments.get().sequence_length == 5
    assert not scheme.segments.get().zero_pad
    assert AuditLog.objects.filter(
        action="coding_segments_replace", object_id=str(scheme.pk)
    ).exists()


def test_clone_creates_next_version_chain_without_copying_counter_or_registry():
    company = make_company()
    admin = make_user("admin", "system_admin")
    source = make_draft(
        actor=admin,
        company=company,
        key="CHAIN",
        sequence_start=10,
        segments=sequence_segments(length=5),
    )
    source_snapshot = (
        source.name,
        source.sequence_start,
        list(source.segments.values("sequence_order", "segment_type", "sequence_length")),
    )

    clone = clone_scheme(
        actor=admin,
        scheme=source,
        data={"sequence_start": 50, "effective_from": timezone.localdate()},
    )

    source.refresh_from_db()
    assert clone.previous_version == source
    assert clone.scheme_key == source.scheme_key
    assert clone.version == 2
    assert clone.status == "draft"
    assert not clone.is_default
    assert clone.sequence_start == 50
    assert list(clone.segments.values("sequence_order", "segment_type", "sequence_length")) == source_snapshot[2]
    assert (source.name, source.sequence_start, list(source.segments.values("sequence_order", "segment_type", "sequence_length"))) == source_snapshot
    assert preview_codes(clone, preview_context(company)) == ["00050"]
    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
    assert AuditLog.objects.filter(
        action="coding_scheme_clone", object_id=str(clone.pk)
    ).exists()


def test_active_or_used_versions_cannot_be_edited_in_place_but_can_be_cloned():
    company = make_company()
    admin = make_user("admin", "system_admin")
    active = make_active(actor=admin, company=company, key="IMMUTABLE")

    with pytest.raises(ValidationError):
        update_draft_scheme(actor=admin, scheme=active, data={"sequence_start": 999})
    with pytest.raises(ValidationError):
        replace_segments(actor=admin, scheme=active, segments=sequence_segments(length=6))

    clone = clone_scheme(actor=admin, scheme=active)
    assert clone.version == 2
    assert clone.status == "draft"
    active.refresh_from_db()
    assert active.sequence_start == 1

    used_version = make_active(actor=admin, company=company, key="USED-VERSION")
    issued_fixture(company=company, scheme=used_version)
    with pytest.raises(ValidationError):
        update_draft_scheme(actor=admin, scheme=used_version, data={"name": "changed"})
    with pytest.raises(ValidationError):
        replace_segments(actor=admin, scheme=used_version, segments=sequence_segments())
    cloned_used = clone_scheme(actor=admin, scheme=used_version)
    assert cloned_used.previous_version == used_version


def test_activation_rejects_invalid_structure_overlap_and_over_64_characters():
    company = make_company(code="C" * 63)
    admin = make_user("admin", "system_admin")
    too_long = make_draft(
        actor=admin,
        company=company,
        key="TOO-LONG",
        segments=[
            {
                "sequence_order": 1,
                "segment_type": "company_code",
                "fixed_value": None,
                "format_string": None,
                "sequence_length": None,
                "zero_pad": None,
            },
            {
                "sequence_order": 2,
                "segment_type": "sequence",
                "fixed_value": None,
                "format_string": None,
                "sequence_length": 2,
                "zero_pad": True,
            },
        ],
    )
    with pytest.raises(ValidationError):
        activate_scheme(actor=admin, scheme=too_long)
    too_long.refresh_from_db()
    assert too_long.status == "draft"

    first = make_active(
        actor=admin,
        company=company,
        key="OVERLAP",
        effective_from=timezone.localdate(),
        effective_to=timezone.localdate() + timedelta(days=20),
    )
    second = AssetCodingScheme.objects.create(
        company=company,
        name="overlap v2",
        scheme_key="OVERLAP",
        version=2,
        status="draft",
        reset_mode="never",
        sequence_start=1,
        effective_from=timezone.localdate() + timedelta(days=10),
        previous_version=first,
    )
    first_segment = first.segments.get()
    second.segments.create(
        sequence_order=1,
        segment_type="sequence",
        sequence_length=first_segment.sequence_length,
        zero_pad=first_segment.zero_pad,
    )
    with pytest.raises(ValidationError):
        activate_scheme(actor=admin, scheme=second)
    second.refresh_from_db()
    assert second.status == "draft"


def test_activate_default_switch_and_retire_update_setup_and_audit():
    company = make_company()
    admin = make_user("admin", "system_admin")
    first = make_draft(actor=admin, company=company, key="FIRST")
    second = make_draft(actor=admin, company=company, key="SECOND")

    activate_scheme(actor=admin, scheme=first)
    set_default_scheme(actor=admin, scheme=first)
    setting = company.initialization_setting
    setting.refresh_from_db()
    assert setting.coding_scheme_configured
    assert not setting.initialization_completed

    activate_scheme(actor=admin, scheme=second)
    set_default_scheme(actor=admin, scheme=second)
    first.refresh_from_db()
    second.refresh_from_db()
    assert not first.is_default
    assert second.is_default
    assert AssetCodingScheme.objects.filter(
        company=company, status="active", is_default=True
    ).count() == 1

    retire_scheme(actor=admin, scheme=second)
    second.refresh_from_db()
    setting.refresh_from_db()
    assert second.status == "retired"
    assert not second.is_default
    assert not setting.coding_scheme_configured
    assert not setting.initialization_completed
    assert {
        "coding_scheme_activate",
        "coding_scheme_default_set",
        "coding_scheme_default_unset",
        "coding_scheme_retire",
        "setup_coding_progress_update",
    } <= set(AuditLog.objects.values_list("action", flat=True))


def test_default_and_category_binding_require_current_effective_active_scheme():
    company = make_company()
    other = make_company("C2", active=False)
    admin = make_user("admin", "system_admin")
    category = make_category(company)
    current = make_active(actor=admin, company=company, key="CURRENT")
    draft = make_draft(actor=admin, company=company, key="DRAFT")
    foreign = AssetCodingScheme.objects.create(
        company=other,
        name="foreign",
        scheme_key="FOREIGN",
        version=1,
        status="draft",
        reset_mode="never",
        sequence_start=1,
        effective_from=timezone.localdate(),
    )
    foreign.segments.create(
        sequence_order=1, segment_type="sequence", sequence_length=4, zero_pad=True
    )
    AssetCodingScheme.objects.filter(pk=foreign.pk).update(status="active")
    foreign.refresh_from_db()

    with pytest.raises(ValidationError):
        set_default_scheme(actor=admin, scheme=draft)
    with pytest.raises(ValidationError):
        set_category_default_scheme(actor=admin, category=category, scheme=draft)
    with pytest.raises(ValidationError):
        set_category_default_scheme(actor=admin, category=category, scheme=foreign)

    set_category_default_scheme(actor=admin, category=category, scheme=current)
    category.refresh_from_db()
    assert category.default_coding_scheme == current
    assert AuditLog.objects.filter(
        action="category_default_coding_scheme_set", object_id=str(category.pk)
    ).exists()


def test_equipment_can_maintain_category_but_not_assign_its_coding_scheme():
    company = make_company()
    admin = make_user("admin", "system_admin")
    equipment = make_user("equipment", "equipment")
    scheme = make_active(actor=admin, company=company, key="CATEGORY-RBAC")

    equipment_form = AssetCategoryForm(actor=equipment, company=company)
    admin_form = AssetCategoryForm(actor=admin, company=company)
    assert "default_coding_scheme" not in equipment_form.fields
    assert "default_coding_scheme" in admin_form.fields
    assert scheme in admin_form.fields["default_coding_scheme"].queryset

    plain = create_asset_category(
        actor=equipment,
        company=company,
        data={
            "code": "PLAIN",
            "name": "普通分类",
            "category_type": "equipment",
        },
    )
    assert plain.pk is not None
    assert plain.default_coding_scheme is None

    with pytest.raises(PermissionDenied):
        create_asset_category(
            actor=equipment,
            company=company,
            data={
                "code": "FORBIDDEN",
                "name": "越权分类",
                "category_type": "equipment",
                "default_coding_scheme": scheme,
            },
        )
    assert not AssetCategory.objects.filter(code="FORBIDDEN").exists()

    allowed = create_asset_category(
        actor=admin,
        company=company,
        data={
            "code": "ADMIN",
            "name": "管理员分类",
            "category_type": "equipment",
            "default_coding_scheme": scheme,
        },
    )
    assert allowed.default_coding_scheme == scheme


def test_preview_one_and_ten_are_consecutive_and_do_not_touch_registry_tables():
    company = make_company()
    admin = make_user("admin", "system_admin")
    scheme = make_draft(
        actor=admin,
        company=company,
        key="PREVIEW",
        sequence_start=9,
        segments=sequence_segments(length=4),
    )
    before = (SequenceCounter.objects.count(), IssuedCode.objects.count())

    one = preview_codes(scheme, preview_context(company))
    ten = preview_codes(scheme, preview_context(company), count=10)

    assert one == ["0009"]
    assert ten == [f"{value:04d}" for value in range(9, 19)]
    assert (SequenceCounter.objects.count(), IssuedCode.objects.count()) == before == (0, 0)


def test_setup_step_six_requires_exactly_one_structurally_valid_current_default():
    company = make_company()
    admin = make_user("admin", "system_admin")
    draft = make_draft(actor=admin, company=company, key="SETUP")

    assert not compute_initialization_progress(company)["coding_scheme_configured"]
    draft.status = "active"
    draft.is_default = True
    draft.effective_from = timezone.localdate() - timedelta(days=10)
    draft.effective_to = timezone.localdate() - timedelta(days=1)
    draft.save(update_fields=["status", "is_default", "effective_from", "effective_to"])
    assert not compute_initialization_progress(company)["coding_scheme_configured"]

    draft.effective_to = None
    draft.save(update_fields=["effective_to"])
    assert compute_initialization_progress(company)["coding_scheme_configured"]

    setting = refresh_initialization_progress(company=company, actor=admin)
    assert setting.coding_scheme_configured
    assert not setting.finance_rules_configured
    assert not setting.initialization_completed


def test_all_normal_rule_and_preview_operations_end_with_empty_registry_tables():
    company = make_company()
    admin = make_user("admin", "system_admin")
    source = make_draft(actor=admin, company=company, key="EMPTY")
    preview_codes(source, preview_context(company), count=10)
    clone = clone_scheme(actor=admin, scheme=source)
    update_draft_scheme(actor=admin, scheme=clone, data={"sequence_start": 100})
    replace_segments(actor=admin, scheme=clone, segments=sequence_segments(length=5))
    activate_scheme(actor=admin, scheme=source)
    set_default_scheme(actor=admin, scheme=source)
    retire_scheme(actor=admin, scheme=source)

    assert SequenceCounter.objects.count() == 0
    assert IssuedCode.objects.count() == 0
