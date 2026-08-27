from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_sprint17_upgrade_from_sprint16_preserves_clearance_and_adds_without_cycle():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    old_targets = [
        ("offboarding", "0002_postgresql_clearance_guards"),
        ("supplies", "0007_sprint16_durable_custody_lifecycle"),
    ]
    try:
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        User = old_apps.get_model("accounts", "User")
        Company = old_apps.get_model("masterdata", "Company")
        Department = old_apps.get_model("masterdata", "Department")
        Employee = old_apps.get_model("masterdata", "Employee")
        Clearance = old_apps.get_model("offboarding", "EmployeeAssetClearance")
        user = User.objects.create(
            username="s17-migration-hr",
            password="unusable",
            display_name="迁移 HR",
        )
        company = Company.objects.create(
            code="MIG-S17",
            normalized_code="mig-s17",
            name="Sprint 17 迁移公司",
            short_name="MIG-S17",
        )
        department = Department.objects.create(
            company=company,
            code="HR",
            normalized_code="hr",
            name="人力资源部",
        )
        employee = Employee.objects.create(
            company=company,
            department=department,
            employee_no="MIG-E",
            normalized_employee_no="mig-e",
            name="迁移员工",
            employment_status="leaving",
            is_active=False,
        )
        initiated_at = timezone.now() - timedelta(days=1)
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_clearance_insert','on',true)"
                    )
            clearance = Clearance.objects.create(
                company=company,
                employee=employee,
                initiated_at=initiated_at,
                initiated_by=user,
                total_assets_snapshot=0,
                unresolved_assets=0,
                status="open",
                idempotency_key="s17-migration-clearance",
            )
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        clearance_id = clearance.pk

        new_targets = [
            ("offboarding", "0003_sprint17_supply_clearance_counters"),
            ("supplies", "0008_sprint17_counts_and_offboarding"),
        ]
        MigrationExecutor(connection).migrate(new_targets)
        new_apps = MigrationExecutor(connection).loader.project_state(
            new_targets
        ).apps
        NewClearance = new_apps.get_model(
            "offboarding", "EmployeeAssetClearance"
        )
        migrated = NewClearance.objects.get(pk=clearance_id)
        assert migrated.total_assets_snapshot == 0
        assert migrated.unresolved_assets == 0
        assert migrated.total_supply_custodies_snapshot == 0
        assert migrated.unresolved_supply_custodies == 0
        assert new_apps.get_model("supplies", "SupplyCountTask") is not None
        assert new_apps.get_model("supplies", "SupplyCountLine") is not None
        assert (
            new_apps.get_model("supplies", "EmployeeSupplyClearanceItem")
            is not None
        )
        dependency = executor.loader.graph.node_map[
            ("supplies", "0008_sprint17_counts_and_offboarding")
        ]
        assert (
            "offboarding",
            "0003_sprint17_supply_clearance_counters",
        ) in dependency.parents
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)
