from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0004_sprint4_formal_asset_guards")]

    operations = [
        migrations.AlterField(
            model_name="asset",
            name="initialization_source",
            field=models.CharField(
                choices=[
                    ("manual", "手工录入"),
                    ("excel_import", "受控 Excel 导入"),
                ],
                default="manual",
                max_length=32,
                verbose_name="初始化来源",
            ),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    initialization_source__in=("manual", "excel_import")
                ),
                name="ck_asset_initialization_source",
            ),
        ),
    ]
