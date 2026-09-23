from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from importlib import import_module

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.assets.lifecycle_services import correct_asset_code
from apps.assets.models import Asset, AssetIdentity, AssetQrIdentity
from apps.assets.registration import create_registered_asset
from apps.coding.presets import activate_standard_version
from apps.coding.services import clone_scheme
from apps.masterdata.models import Company, AssetCategory, Department, Employee, Location, IssuedCode, SequenceCounter, UserDepartmentScope
from tests.test_sprint3_support import make_department, make_employee, make_user
from tests.test_sprint4_acceptance import _base_context
from tests.test_unified_asset_identity import context, physical_data, registered

pytestmark = pytest.mark.django_db(transaction=True)


def test_parallel_roots_and_components_are_unique_and_continuous(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL locking test")
    ids = {name: context[name].pk for name in ("company", "equipment", "category", "department", "employee", "location")}
    def create(number, parent_id=None):
        connections.close_all()
        try:
            local = {"company":Company.objects.get(pk=ids["company"]), "equipment":get_user_model().objects.get(pk=ids["equipment"]),
                     "category":AssetCategory.objects.get(pk=ids["category"]), "department":Department.objects.get(pk=ids["department"]),
                     "employee":Employee.objects.get(pk=ids["employee"]), "location":Location.objects.get(pk=ids["location"])}
            kwargs = {} if parent_id is None else {"component_of":Asset.objects.get(pk=parent_id), "management_attribute":""}
            return registered(local, f"parallel-{parent_id}-{number}", **kwargs).asset_code
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=4) as pool:
        roots = sorted(pool.map(create, range(4)))
    assert roots == [f"FA-02-2020-{number:06d}-00" for number in range(1, 5)]
    parent = Asset.objects.get(asset_code=roots[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        children = sorted(pool.map(lambda number:create(number,parent.pk), range(4)))
    assert children == [f"FA-02-2020-000001-{number:02d}" for number in range(1, 5)]
    assert SequenceCounter.objects.get().current_value == 4


def test_99_component_numbers_are_permanent_and_do_not_roll_over(context):
    parent = registered(context, "capacity-parent")
    for number in range(1, 100):
        child = registered(context, f"capacity-{number}", component_of=parent, management_attribute="")
        assert child.identity.subitem_number == number
    before = (Asset.objects.count(), IssuedCode.objects.count())
    with pytest.raises(ValidationError, match="99"):
        registered(context, "capacity-overflow", component_of=parent, management_attribute="")
    assert (Asset.objects.count(), IssuedCode.objects.count()) == before
    assert SequenceCounter.objects.get().current_value == 1


def test_source_scope_survives_version_change_and_explicit_correction(context):
    asset = registered(context)
    previous = asset.identity
    old_code = asset.asset_code
    newer = clone_scheme(actor=context["admin"], scheme=context["standard_scheme"], data={"effective_from":timezone.localdate()})
    activate_standard_version(actor=context["admin"], scheme=newer)
    issued = correct_asset_code(actor=context["admin"], asset=asset, effective_date=timezone.localdate(),
        idempotency_key="explicit-correction", reason="明确登记错误，受控更正并保留新旧映射", coding_scheme=newer)
    asset.refresh_from_db()
    assert issued.display_code == "FA-02-2020-000002-00"
    assert asset.identity.pk != previous.pk
    assert AssetIdentity.objects.filter(pk=previous.pk, issued_code__display_code=old_code).exists()
    assert asset.management_attribute == "FA" and asset.coding_year == 2020
    assert asset.code_history.filter(event_type="corrected").exists()


def test_identity_history_and_registered_year_reject_direct_database_edits(context):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL immutable guards")
    asset = registered(context)
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(AssetIdentity.objects.filter(pk=asset.identity.pk), management_attribute="LV")
    with pytest.raises(IntegrityError), transaction.atomic():
        QuerySet.update(Asset.objects.filter(pk=asset.pk), coding_year=2021)
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM assets_assetidentity WHERE id=%s", [asset.identity.pk])


def test_out_of_scope_parent_rejected_even_when_child_department_is_allowed(context):
    parent = registered(context, "private-parent")
    department = make_department(context["company"], "OTHER")
    employee = make_employee(context["company"], department, "OTHER-E")
    actor = make_user("limited-component-manager", "department_manager")
    UserDepartmentScope.objects.create(company=context["company"], user=actor, department=department,
                                      assigned_by=context["admin"], include_descendants=True)
    with pytest.raises(PermissionDenied):
        create_registered_asset(actor=actor, company=context["company"],
            data=physical_data(context, department=department, responsible_employee=employee,
                               component_of=parent, management_attribute=""), idempotency_key="out-of-scope-parent")
    assert Asset.objects.count() == 1


def test_year_matching_acquisition_date_has_real_evidence(context):
    asset = registered(context, coding_year=2020)
    assert asset.identity.year_source == "acquisition_date"
    assert asset.identity.year_note


def _legacy_targets(latest):
    return [(label, "0015_physical_registration" if label == "assets" else "0012_sprint16_opening_custody_import" if label == "masterdata" else name)
            for label, name in latest]


def test_upgrade_keeps_existing_legacy_registration_and_does_not_infer_attribute():
    ctx = _base_context("LEGACYUPGRADE")
    data = physical_data(ctx)
    data.pop("management_attribute")
    asset = create_registered_asset(actor=ctx["equipment"], company=ctx["company"], data=data, idempotency_key="legacy-registered")
    expected = (asset.pk, asset.asset_code, asset.current_issued_code_id,
                asset.qr_identities.get(status="active").pk, asset.registration.pk)
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(_legacy_targets(latest))
        old_apps = executor.loader.project_state(_legacy_targets(latest)).apps
        legacy = old_apps.get_model("assets", "Asset").objects.get(pk=asset.pk)
        assert legacy.asset_code == expected[1]
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        restored = Asset.objects.get(pk=asset.pk)
        assert (restored.pk, restored.asset_code, restored.current_issued_code_id,
                restored.qr_identities.get(status="active").pk, restored.registration.pk) == expected
        assert restored.management_attribute == "" and restored.coding_year is None
        assert restored.identity is None and not AssetIdentity.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_downgrade_refuses_to_discard_used_standard_identity(context):
    registered(context)
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="不能|不允许"):
            executor.migrate(_legacy_targets(latest))
    finally:
        MigrationExecutor(connection).migrate(latest)
    assert AssetIdentity.objects.count() == 1
