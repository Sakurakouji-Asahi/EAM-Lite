import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_sprint14_migrates_from_sprint13_and_preserves_supply_master_data():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes()
    old_targets = [
        ("masterdata", "0010_sprint13_supply_item_import"),
        ("supplies", "0002_postgresql_integrity_guards"),
    ]
    try:
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        Company = old_apps.get_model("masterdata", "Company")
        Category = old_apps.get_model("supplies", "SupplyCategory")
        Warehouse = old_apps.get_model("supplies", "SupplyWarehouse")
        Item = old_apps.get_model("supplies", "SupplyItem")
        company = Company.objects.create(
            code="MIG-S14",
            normalized_code="mig-s14",
            name="Sprint 14 迁移公司",
            short_name="MIG-S14",
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
        company_id = company.pk
        item_id = item.pk

        executor = MigrationExecutor(connection)
        new_target = [("supplies", "0004_sprint14_postgresql_guards")]
        executor.migrate(new_target)
        new_apps = executor.loader.project_state(new_target).apps
        NewItem = new_apps.get_model("supplies", "SupplyItem")
        Document = new_apps.get_model("supplies", "SupplyDocument")
        Balance = new_apps.get_model("supplies", "SupplyStockBalance")
        ImportBatch = new_apps.get_model("masterdata", "ImportBatch")
        assert NewItem.objects.get(pk=item_id).company_id == company_id
        assert Document._meta.get_field("document_no") is not None
        assert Balance._meta.get_field("quantity_on_hand") is not None
        assert ("opening_stock", "低值物品期初库存") in ImportBatch._meta.get_field(
            "import_type"
        ).choices
    finally:
        MigrationExecutor(connection).migrate(leaf_nodes)
