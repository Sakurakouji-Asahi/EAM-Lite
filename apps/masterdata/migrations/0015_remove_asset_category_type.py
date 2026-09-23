"""Remove the redundant discriminator; snapshots remain only in migration audit."""
import uuid

from django.db import migrations, models

ARCHIVE_ACTION = "asset_category_type_removed"
OLD_CHOICES = (
    ("equipment", "设备"), ("mold", "模具"), ("tool", "工具"),
    ("inspection_tool", "检具"), ("office_equipment", "办公设备"), ("other", "其他"),
)


def archive_removed_values(apps, schema_editor):
    alias = schema_editor.connection.alias
    ExportLog = apps.get_model("reports", "ExportLog")
    if ExportLog.objects.using(alias).filter(
        export_type__in=("equipment_list", "mold_tool_inspection_list"), status="pending"
    ).exists():
        raise RuntimeError("仍有旧分组清单正在导出，请先处理完该导出后再移除实物类型。")
    Category = apps.get_model("masterdata", "AssetCategory")
    AuditLog = apps.get_model("audit", "AuditLog")
    correlation = uuid.uuid4()
    for category in Category.objects.using(alias).order_by("pk").iterator():
        AuditLog.objects.using(alias).create(
            company_id=category.company_id, user_id=None, action=ARCHIVE_ACTION,
            object_type="AssetCategory", object_id=str(category.pk), correlation_id=correlation,
            old_data_json={"code": category.code, "name": category.name, "category_type": category.category_type},
            new_data_json={"removed_field": "category_type", "migration": "masterdata.0015", "reason": "用户要求直接移除实物类型；此记录仅保留变更前值，不参与业务筛选。"},
        )


def restore_removed_values(apps, schema_editor):
    alias = schema_editor.connection.alias
    Category = apps.get_model("masterdata", "AssetCategory")
    AuditLog = apps.get_model("audit", "AuditLog")
    archived = {}
    for row in AuditLog.objects.using(alias).filter(action=ARCHIVE_ACTION, object_type="AssetCategory").order_by("created_at", "pk").iterator():
        archived[(row.company_id, row.object_id)] = row.old_data_json.get("category_type")
    allowed = {key for key, _ in OLD_CHOICES}
    correlation = uuid.uuid4()
    for category in Category.objects.using(alias).order_by("pk").iterator():
        previous = archived.get((category.company_id, str(category.pk)))
        if previous is not None and previous not in allowed:
            raise RuntimeError("迁移审计中的旧实物类型无效，不能猜测或覆盖原值。")
        restored = previous if previous is not None else "other"
        Category.objects.using(alias).filter(pk=category.pk).update(category_type=restored)
        if previous is None:
            AuditLog.objects.using(alias).create(
                company_id=category.company_id, user_id=None, action="asset_category_type_restore_default",
                object_type="AssetCategory", object_id=str(category.pk), correlation_id=correlation,
                old_data_json={"category_type": None},
                new_data_json={"category_type": "other", "reason": "回退旧结构：该分类在字段移除后创建，没有旧分组，采用其他，不推断具体类型。"},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("masterdata", "0014_physical_setup_before_finance"),
        ("reports", "0002_sprint18_supply_report_types"),
        ("audit", "0002_auditlog_company"),
    ]
    operations = [
        migrations.RunPython(archive_removed_values, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="assetcategory", name="ck_category_type_valid"),
        # A reverse ADD COLUMN needs a safe value before restoring audited values.
        migrations.AlterField(model_name="assetcategory", name="category_type", field=models.CharField(
            verbose_name="实物类型", max_length=32, choices=OLD_CHOICES, default="other"
        )),
        migrations.RunPython(migrations.RunPython.noop, restore_removed_values),
        migrations.RemoveField(model_name="assetcategory", name="category_type"),
    ]
