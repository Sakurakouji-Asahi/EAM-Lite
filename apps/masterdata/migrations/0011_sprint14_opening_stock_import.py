from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("masterdata", "0010_sprint13_supply_item_import"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="importbatch",
            name="ck_import_batch_type_valid",
        ),
        migrations.AlterField(
            model_name="importbatch",
            name="import_type",
            field=models.CharField(
                choices=[
                    ("department", "部门"),
                    ("employee", "人员"),
                    ("asset_initialization", "资产初始化"),
                    ("item_master", "低值物品档案"),
                    ("opening_stock", "低值物品期初库存"),
                ],
                max_length=32,
                verbose_name="导入类型",
            ),
        ),
        migrations.AddConstraint(
            model_name="importbatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    import_type__in=(
                        "department",
                        "employee",
                        "asset_initialization",
                        "item_master",
                        "opening_stock",
                    )
                ),
                name="ck_import_batch_type_valid",
            ),
        ),
    ]
