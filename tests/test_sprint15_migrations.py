from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_sprint15_migrates_from_sprint14_and_preserves_stock_history():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    old_target = [("supplies", "0004_sprint14_postgresql_guards")]
    try:
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        Company = old_apps.get_model("masterdata", "Company")
        Category = old_apps.get_model("supplies", "SupplyCategory")
        Warehouse = old_apps.get_model("supplies", "SupplyWarehouse")
        Item = old_apps.get_model("supplies", "SupplyItem")
        Document = old_apps.get_model("supplies", "SupplyDocument")
        Line = old_apps.get_model("supplies", "SupplyDocumentLine")
        Balance = old_apps.get_model("supplies", "SupplyStockBalance")
        Ledger = old_apps.get_model("supplies", "SupplyStockLedger")
        company = Company.objects.create(
            code="MIG-S15",
            normalized_code="mig-s15",
            name="Sprint 15 迁移公司",
            short_name="MIG-S15",
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
        )
        document = Document.objects.create(
            company=company,
            document_no="QC-2026-000001",
            document_type="opening",
            business_date="2026-08-26",
            target_warehouse=warehouse,
            status="posted",
            idempotency_key="migration-opening",
            posted_at="2026-08-26T00:00:00Z",
        )
        line = Line.objects.create(
            company=company,
            document=document,
            line_no=1,
            item=item,
            quantity=Decimal("2.0000"),
            entered_unit_cost=Decimal("10.000000"),
            posted_unit_cost=Decimal("10.000000"),
            posted_amount=Decimal("20.00"),
        )
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_supply_balance_mutation','on',true)"
                    )
            balance = Balance.objects.create(
                company=company,
                warehouse=warehouse,
                item=item,
                quantity_on_hand=Decimal("2.0000"),
                amount_on_hand=Decimal("20.00"),
                average_unit_cost=Decimal("10.000000"),
            )
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('eam_lite.controlled_supply_ledger_insert','on',true)"
                    )
            ledger = Ledger.objects.create(
                company=company,
                warehouse=warehouse,
                item=item,
                document=document,
                document_line=line,
                movement_type="opening_in",
                quantity_delta=Decimal("2.0000"),
                amount_delta=Decimal("20.00"),
                unit_cost=Decimal("10.000000"),
                quantity_before=Decimal("0.0000"),
                quantity_after=Decimal("2.0000"),
                amount_before=Decimal("0.00"),
                amount_after=Decimal("20.00"),
                average_unit_cost_before=Decimal("0.000000"),
                average_unit_cost_after=Decimal("10.000000"),
                occurred_at="2026-08-26T00:00:00Z",
            )
        ids = (company.pk, document.pk, line.pk, balance.pk, ledger.pk)

        new_target = [("supplies", "0006_sprint15_postgresql_guards")]
        executor = MigrationExecutor(connection)
        executor.migrate(new_target)
        new_apps = executor.loader.project_state(new_target).apps
        NewDocument = new_apps.get_model("supplies", "SupplyDocument")
        NewLine = new_apps.get_model("supplies", "SupplyDocumentLine")
        NewBalance = new_apps.get_model("supplies", "SupplyStockBalance")
        NewLedger = new_apps.get_model("supplies", "SupplyStockLedger")
        Custody = new_apps.get_model("supplies", "SupplyCustody")
        Movement = new_apps.get_model("supplies", "SupplyCustodyMovement")
        assert NewDocument.objects.get(pk=ids[1]).company_id == ids[0]
        assert NewLine.objects.get(pk=ids[2]).source_custody_id is None
        assert NewBalance.objects.get(pk=ids[3]).amount_on_hand == Decimal("20.00")
        assert NewLedger.objects.get(pk=ids[4]).amount_delta == Decimal("20.00")
        assert Custody._meta.get_field("origin_issue_line").unique
        assert Movement._meta.get_field("reverses_movement").unique
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)
