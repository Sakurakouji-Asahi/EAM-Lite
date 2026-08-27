import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_sprint18_upgrade_from_sprint17_preserves_data_and_adds_report_contracts():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    old_targets = [
        ("reports", "0001_initial"),
        ("supplies", "0008_sprint17_counts_and_offboarding"),
    ]
    try:
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        User = old_apps.get_model("accounts", "User")
        Company = old_apps.get_model("masterdata", "Company")
        Category = old_apps.get_model("supplies", "SupplyCategory")
        Warehouse = old_apps.get_model("supplies", "SupplyWarehouse")
        Item = old_apps.get_model("supplies", "SupplyItem")
        ExportLog = old_apps.get_model("reports", "ExportLog")
        user = User.objects.create(
            username="s18-migration-user",
            password="unusable",
            display_name="Sprint 18 迁移用户",
        )
        company = Company.objects.create(
            code="MIG-S18",
            normalized_code="mig-s18",
            name="Sprint 18 迁移公司",
            short_name="MIG-S18",
        )
        category = Category.objects.create(
            company=company,
            code="CAT",
            normalized_code="cat",
            name="迁移分类",
        )
        warehouse = Warehouse.objects.create(
            company=company,
            code="WH",
            normalized_code="wh",
            name="迁移仓库",
        )
        item = Item.objects.create(
            company=company,
            item_code="ITEM",
            normalized_item_code="item",
            name="迁移物品",
            category=category,
            item_type="consumable",
            unit="个",
            default_warehouse=warehouse,
        )
        log = ExportLog.objects.create(
            company=company,
            export_type="asset_ledger",
            filters_json={},
            request_hash="a" * 64,
            idempotency_key="s18-migration-export",
            requested_by=user,
            status="pending",
        )
        ids = item.pk, log.pk

        new_targets = [
            ("reports", "0002_sprint18_supply_report_types"),
            ("supplies", "0009_sprint18_reporting_index"),
        ]
        MigrationExecutor(connection).migrate(new_targets)
        new_executor = MigrationExecutor(connection)
        new_apps = new_executor.loader.project_state(new_targets).apps
        NewItem = new_apps.get_model("supplies", "SupplyItem")
        NewExportLog = new_apps.get_model("reports", "ExportLog")
        assert NewItem.objects.get(pk=ids[0]).item_code == "ITEM"
        assert NewExportLog.objects.get(pk=ids[1]).status == "pending"
        choices = dict(NewExportLog._meta.get_field("export_type").choices)
        assert choices["supply_stock_balance"] == "当前库存余额表"
        assert choices["supply_management_amount"] == "综合管理金额表"
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, "supplies_supplystockledger"
            )
        assert constraints["supply_ledger_company_time_idx"]["index"] is True
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)


@pytest.mark.django_db(transaction=True)
def test_current_default_baseline_can_upgrade_to_sprint18_without_data_loss():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    old_targets = [
        ("masterdata", "0010_sprint13_supply_item_import"),
        ("offboarding", "0002_postgresql_clearance_guards"),
        ("reports", "0001_initial"),
        ("supplies", "0002_postgresql_integrity_guards"),
    ]
    try:
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        Company = old_apps.get_model("masterdata", "Company")
        Department = old_apps.get_model("masterdata", "Department")
        Employee = old_apps.get_model("masterdata", "Employee")
        Category = old_apps.get_model("supplies", "SupplyCategory")
        Warehouse = old_apps.get_model("supplies", "SupplyWarehouse")
        Item = old_apps.get_model("supplies", "SupplyItem")
        company = Company.objects.create(
            code="MIG-BASE-S18",
            normalized_code="mig-base-s18",
            name="默认基线模拟公司",
            short_name="MIGBASE",
        )
        department = Department.objects.create(
            company=company,
            code="D",
            normalized_code="d",
            name="迁移部门",
        )
        employee = Employee.objects.create(
            company=company,
            department=department,
            employee_no="E",
            normalized_employee_no="e",
            name="迁移员工",
            employment_status="active",
            is_active=True,
        )
        category = Category.objects.create(
            company=company,
            code="CAT",
            normalized_code="cat",
            name="迁移分类",
        )
        warehouse = Warehouse.objects.create(
            company=company,
            code="WH",
            normalized_code="wh",
            name="迁移仓库",
            manager_employee=employee,
        )
        item = Item.objects.create(
            company=company,
            item_code="BASE-ITEM",
            normalized_item_code="base-item",
            name="基线物品",
            category=category,
            item_type="durable_quantity",
            unit="把",
            default_warehouse=warehouse,
        )
        ids = company.pk, employee.pk, item.pk

        new_targets = [
            ("masterdata", "0012_sprint16_opening_custody_import"),
            ("offboarding", "0003_sprint17_supply_clearance_counters"),
            ("reports", "0002_sprint18_supply_report_types"),
            ("supplies", "0009_sprint18_reporting_index"),
        ]
        MigrationExecutor(connection).migrate(new_targets)
        new_apps = MigrationExecutor(connection).loader.project_state(
            new_targets
        ).apps
        assert new_apps.get_model("masterdata", "Company").objects.filter(
            pk=ids[0]
        ).exists()
        assert new_apps.get_model("masterdata", "Employee").objects.filter(
            pk=ids[1]
        ).exists()
        migrated_item = new_apps.get_model("supplies", "SupplyItem").objects.get(
            pk=ids[2]
        )
        assert migrated_item.item_type == "durable_quantity"
        assert new_apps.get_model("supplies", "SupplyCountTask") is not None
        assert (
            new_apps.get_model("offboarding", "EmployeeAssetClearance")
            ._meta.get_field("unresolved_supply_custodies")
            is not None
        )
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)
