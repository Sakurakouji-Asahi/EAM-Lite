from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("masterdata", "0011_sprint14_opening_stock_import")]

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
                    ("opening_custody", "耐用品期初保管"),
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
                        "opening_custody",
                    )
                ),
                name="ck_import_batch_type_valid",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="importbatch",
            name="ck_import_batch_status_fields",
        ),
        migrations.RemoveConstraint(
            model_name="importbatch",
            name="ck_import_batch_status_valid",
        ),
        migrations.AlterField(
            model_name="importbatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("uploaded", "已上传"),
                    ("validated", "校验通过"),
                    ("invalid", "校验不通过"),
                    ("confirmed", "已确认"),
                    ("failed", "处理失败"),
                    ("cancelled", "已取消"),
                ],
                default="uploaded",
                max_length=16,
                verbose_name="批次状态",
            ),
        ),
        migrations.AddConstraint(
            model_name="importbatch",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        status="uploaded",
                        validated_at__isnull=True,
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                    | models.Q(
                        status="validated",
                        validated_at__isnull=False,
                        error_rows=0,
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                    | models.Q(
                        status="invalid",
                        validated_at__isnull=False,
                        error_rows__gt=0,
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                    | models.Q(
                        status="confirmed",
                        validated_at__isnull=False,
                        error_rows=0,
                        confirmed_by__isnull=False,
                        confirmed_at__isnull=False,
                    )
                    | models.Q(
                        status="failed",
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                    | models.Q(
                        status="cancelled",
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                ),
                name="ck_import_batch_status_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="importbatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    status__in=(
                        "uploaded",
                        "validated",
                        "invalid",
                        "confirmed",
                        "failed",
                        "cancelled",
                    )
                ),
                name="ck_import_batch_status_valid",
            ),
        ),
    ]
