import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.masterdata.models import Company


@pytest.mark.django_db(transaction=True)
def test_sprint13_migrates_from_previous_baseline_and_preserves_company():
    company = Company.objects.create(
        code="MIG-S13",
        normalized_code="mig-s13",
        name="迁移验证公司",
        short_name="迁移验证",
    )
    company_id = company.pk
    target_old = ("masterdata", "0009_sprint5_asset_initialization_import")
    target_new = ("supplies", "0002_postgresql_integrity_guards")
    try:
        MigrationExecutor(connection).migrate([target_old])
        executor = MigrationExecutor(connection)
        executor.migrate([target_new])
        apps = executor.loader.project_state([target_new]).apps
        HistoricalCompany = apps.get_model("masterdata", "Company")
        HistoricalCategory = apps.get_model("supplies", "SupplyCategory")
        HistoricalWarehouse = apps.get_model("supplies", "SupplyWarehouse")
        HistoricalItem = apps.get_model("supplies", "SupplyItem")

        owner = HistoricalCompany.objects.get(pk=company_id)
        category = HistoricalCategory.objects.create(
            company=owner,
            code="MIG-CAT",
            normalized_code="mig-cat",
            name="迁移分类",
        )
        warehouse = HistoricalWarehouse.objects.create(
            company=owner,
            code="MIG-WH",
            normalized_code="mig-wh",
            name="迁移仓库",
        )
        item = HistoricalItem.objects.create(
            company=owner,
            item_code="MIG-ITEM",
            normalized_item_code="mig-item",
            name="迁移物品",
            category=category,
            item_type="consumable",
            unit="个",
            default_warehouse=warehouse,
        )
        assert str(item.pk)
        import_type_field = apps.get_model("masterdata", "ImportBatch")._meta.get_field(
            "import_type"
        )
        assert ("item_master", "低值物品档案") in import_type_field.choices
    finally:
        MigrationExecutor(connection).migrate([target_new])
