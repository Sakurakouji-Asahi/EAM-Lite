from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_sprint16_migrates_from_sprint15_and_preserves_custody_history():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    old_targets = [
        ("masterdata", "0011_sprint14_opening_stock_import"),
        ("supplies", "0006_sprint15_postgresql_guards"),
    ]
    try:
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        Company = old_apps.get_model("masterdata", "Company")
        Department = old_apps.get_model("masterdata", "Department")
        Category = old_apps.get_model("supplies", "SupplyCategory")
        Warehouse = old_apps.get_model("supplies", "SupplyWarehouse")
        Item = old_apps.get_model("supplies", "SupplyItem")
        Document = old_apps.get_model("supplies", "SupplyDocument")
        Line = old_apps.get_model("supplies", "SupplyDocumentLine")
        Balance = old_apps.get_model("supplies", "SupplyStockBalance")
        Ledger = old_apps.get_model("supplies", "SupplyStockLedger")
        Custody = old_apps.get_model("supplies", "SupplyCustody")
        Movement = old_apps.get_model("supplies", "SupplyCustodyMovement")
        company = Company.objects.create(
            code="MIG-S16",
            normalized_code="mig-s16",
            name="Sprint 16 迁移公司",
            short_name="MIG-S16",
        )
        department = Department.objects.create(
            company=company,
            code="USE",
            normalized_code="use",
            name="使用部门",
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
            item_code="CHAIR",
            normalized_item_code="chair",
            name="迁移椅子",
            category=category,
            item_type="durable_quantity",
            unit="把",
        )
        opening_document = Document.objects.create(
            company=company,
            document_no="QC-2026-000001",
            document_type="opening",
            business_date="2026-08-25",
            target_warehouse=warehouse,
            status="posted",
            idempotency_key="migration-opening",
            posted_at="2026-08-25T00:00:00Z",
        )
        opening_line = Line.objects.create(
            company=company,
            document=opening_document,
            line_no=1,
            item=item,
            quantity=Decimal("2.0000"),
            entered_unit_cost=Decimal("80.000000"),
            posted_unit_cost=Decimal("80.000000"),
            posted_amount=Decimal("160.00"),
        )
        document = Document.objects.create(
            company=company,
            document_no="LY-2026-000001",
            document_type="issue",
            business_date="2026-08-26",
            source_warehouse=warehouse,
            department=department,
            status="posted",
            idempotency_key="migration-issue",
            posted_at="2026-08-26T00:00:00Z",
        )
        line = Line.objects.create(
            company=company,
            document=document,
            line_no=1,
            item=item,
            quantity=Decimal("2.0000"),
            posted_unit_cost=Decimal("80.000000"),
            posted_amount=Decimal("160.00"),
        )
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_supply_balance_mutation','on',true)"
                    )
            Balance.objects.create(
                company=company,
                warehouse=warehouse,
                item=item,
                quantity_on_hand=Decimal("0.0000"),
                amount_on_hand=Decimal("0.00"),
                average_unit_cost=Decimal("0.000000"),
            )
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_supply_ledger_insert','on',true)"
                    )
            Ledger.objects.create(
                company=company,
                warehouse=warehouse,
                item=item,
                document=opening_document,
                document_line=opening_line,
                movement_type="opening_in",
                quantity_delta=Decimal("2.0000"),
                amount_delta=Decimal("160.00"),
                unit_cost=Decimal("80.000000"),
                quantity_before=Decimal("0.0000"),
                quantity_after=Decimal("2.0000"),
                amount_before=Decimal("0.00"),
                amount_after=Decimal("160.00"),
                average_unit_cost_before=Decimal("0.000000"),
                average_unit_cost_after=Decimal("80.000000"),
                occurred_at="2026-08-25T00:00:00Z",
            )
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_supply_ledger_insert','on',true)"
                    )
            Ledger.objects.create(
                company=company,
                warehouse=warehouse,
                item=item,
                document=document,
                document_line=line,
                movement_type="issue_out",
                quantity_delta=Decimal("-2.0000"),
                amount_delta=Decimal("-160.00"),
                unit_cost=Decimal("80.000000"),
                quantity_before=Decimal("2.0000"),
                quantity_after=Decimal("0.0000"),
                amount_before=Decimal("160.00"),
                amount_after=Decimal("0.00"),
                average_unit_cost_before=Decimal("80.000000"),
                average_unit_cost_after=Decimal("0.000000"),
                occurred_at="2026-08-26T00:00:00Z",
            )
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_supply_custody_mutation','on',true)"
                    )
            custody = Custody.objects.create(
                company=company,
                item=item,
                origin_issue_line=line,
                department=department,
                current_quantity=Decimal("2.0000"),
                current_amount=Decimal("160.00"),
                unit_cost_snapshot=Decimal("80.000000"),
                started_on="2026-08-26",
                status="open",
            )
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_supply_custody_movement_insert','on',true)"
                    )
            movement = Movement.objects.create(
                company=company,
                item=item,
                to_custody=custody,
                action="issue",
                quantity=Decimal("2.0000"),
                amount=Decimal("160.00"),
                unit_cost=Decimal("80.000000"),
                business_date="2026-08-26",
                source_document_line=line,
            )
        ids = custody.pk, movement.pk

        new_targets = [
            ("masterdata", "0012_sprint16_opening_custody_import"),
            ("supplies", "0007_sprint16_durable_custody_lifecycle"),
        ]
        MigrationExecutor(connection).migrate(new_targets)
        new_apps = MigrationExecutor(connection).loader.project_state(
            new_targets
        ).apps
        NewCustody = new_apps.get_model("supplies", "SupplyCustody")
        NewMovement = new_apps.get_model("supplies", "SupplyCustodyMovement")
        ImportBatch = new_apps.get_model("masterdata", "ImportBatch")
        migrated = NewCustody.objects.get(pk=ids[0])
        assert migrated.origin_issue_line_id == line.pk
        assert migrated.origin_import_row_id is None
        assert migrated.parent_custody_id is None
        assert NewMovement.objects.get(pk=ids[1]).idempotency_key is None
        assert ("opening_custody", "耐用品期初保管") in ImportBatch._meta.get_field(
            "import_type"
        ).choices
        assert ("cancelled", "已取消") in ImportBatch._meta.get_field(
            "status"
        ).choices
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)
