from types import MappingProxyType
import hashlib

import pytest
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.masterdata.models import AssetCategory
from apps.masterdata.services import create_asset_category, update_asset_category
from apps.reports import services as export_services
from apps.reports.models import ExportLog
from apps.reports.queries import ReportDataset, ReportValidationError, build_report_dataset
from apps.reports.schemas import REPORT_REGISTRY, RETIRED_REPORT_KEYS, get_report_definition, report_choices
from tests.test_unified_asset_identity import context

pytestmark = pytest.mark.django_db(transaction=True)


def test_type_is_removed_from_model_database_and_category_workflow(context, client):
    with pytest.raises(FieldDoesNotExist):
        AssetCategory._meta.get_field("category_type")
    assert not hasattr(AssetCategory, "CategoryType")
    with connection.cursor() as cursor:
        assert "category_type" not in {column.name for column in connection.introspection.get_table_description(cursor, AssetCategory._meta.db_table)}
    client.force_login(context["admin"])
    for path in (reverse("masterdata:category-list"), reverse("masterdata:category-create"),
                 reverse("masterdata:category-edit", args=[context["category"].pk]),
                 reverse("masterdata:category-detail", args=[context["category"].pk])):
        response = client.get(path)
        assert response.status_code == 200
        assert "实物类型" not in response.content.decode() and 'name="category_type"' not in response.content.decode()
    response = client.post(reverse("masterdata:category-create"), {"code": "0201", "name": "模压设备", "parent": context["category"].pk})
    assert response.status_code == 302
    category = AssetCategory.objects.get(code="0201")
    assert category.parent_id == context["category"].pk
    assert client.post(reverse("masterdata:category-edit", args=[category.pk]), {"code": "0201", "name": "模压设备组", "parent": context["category"].pk}).status_code == 302
    category.refresh_from_db()
    assert category.name == "模压设备组"


def test_services_reject_removed_type_instead_of_storing_an_invisible_property(context):
    with pytest.raises(ValidationError, match="已移除"):
        create_asset_category(actor=context["admin"], company=context["company"], data={"code": "BAD", "name": "不应创建", "category_type": "equipment"})
    with pytest.raises(ValidationError, match="已移除"):
        update_asset_category(actor=context["admin"], category=context["category"], data={"category_type": "mold"})
    assert not AssetCategory.objects.filter(code="BAD").exists()


@pytest.mark.parametrize("key", sorted(RETIRED_REPORT_KEYS))
def test_old_group_reports_are_not_selectable_or_newly_generated(context, client, key):
    assert key not in REPORT_REGISTRY and key not in dict(report_choices())
    with pytest.raises(ReportValidationError, match="旧分组清单已取消"):
        build_report_dataset(actor=context["finance"], company=context["company"], report_key=key)
    with pytest.raises(ValidationError, match="旧分组清单已取消"):
        export_services.generate_report_export(actor=context["finance"], company=context["company"], report_key=key, idempotency_key="retired-new")
    assert not ExportLog.objects.exists()
    client.force_login(context["finance"])
    response = client.get(reverse("reports:report-center"), {"report_type": key, "category": context["category"].pk}, follow=True)
    assert response.status_code == 200 and response.context["dataset"].definition.key == "asset_ledger"
    assert "旧分组清单已取消" in response.content.decode()


@pytest.mark.parametrize("key", sorted(RETIRED_REPORT_KEYS))
def test_old_completed_exports_remain_readable_downloadable_and_idempotent(context, client, settings, tmp_path, key):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.IMPORT_TEMP_ROOT = tmp_path / "tmp"
    settings.IMPORT_TEMP_ROOT.mkdir()
    filters = export_services._stored_export_filters(actor=context["finance"], company=context["company"], report_key=key, filters={})
    digest = export_services._request_hash({"export_type": key, "filters": filters})
    log, created = export_services._create_export_request(actor=context["finance"], company=context["company"], export_type=key,
        filters=filters, idempotency_key="historical-file", request_hash=digest, request=None)
    assert created
    dataset = ReportDataset(definition=get_report_definition(key), rows=(MappingProxyType({"asset_code": "OLD-001", "asset_name": "历史导出设备"}),),
        filters=MappingProxyType(filters), data_snapshot_at=timezone.now())
    log = export_services._write_and_publish(export_log=log, actor=context["finance"], company=context["company"], dataset=dataset, request=None)
    client.force_login(context["finance"])
    assert client.get(reverse("reports:export-detail", args=[log.pk])).status_code == 200
    response = client.get(reverse("reports:export-download", args=[log.pk]))
    assert response.status_code == 200
    assert hashlib.sha256(b"".join(response.streaming_content)).hexdigest() == log.output_sha256
    replay = export_services.generate_report_export(actor=context["finance"], company=context["company"], report_key=key, idempotency_key="historical-file")
    assert replay.pk == log.pk and ExportLog.objects.count() == 1


def test_migration_archives_old_values_removes_column_and_reverses_without_changing_categories():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old = ("masterdata", "0014_physical_setup_before_finance")
    try:
        executor.migrate([old])
        historical = MigrationExecutor(connection).loader.project_state([old]).apps
        Company = historical.get_model("masterdata", "Company")
        Category = historical.get_model("masterdata", "AssetCategory")
        company = Company.objects.create(code="REMOVAL", normalized_code="removal", name="迁移核对公司", short_name="核对", currency="CNY", timezone="Asia/Shanghai", is_active=True)
        root = Category.objects.create(company=company, code="02", normalized_code="02", name="机器设备", category_type="equipment", category_level=1, is_active=True)
        child = Category.objects.create(company=company, parent=root, code="0201", normalized_code="0201", name="模具", category_type="mold", category_level=2, is_active=True)
        before = list(Category.objects.order_by("pk").values())
        MigrationExecutor(connection).migrate(latest)
        assert list(AssetCategory.objects.order_by("pk").values()) == [{key:value for key,value in row.items() if key != "category_type"} for row in before]
        archives = AuditLog.objects.filter(action="asset_category_type_removed").order_by("object_id")
        assert set(archives.values_list("object_id", flat=True)) == {str(root.pk),str(child.pk)}
        assert {row.old_data_json["category_type"] for row in archives} == {"equipment","mold"}
        new = AssetCategory.objects.create(company_id=company.pk, code="99", normalized_code="99", name="新建分类", is_active=True)
        MigrationExecutor(connection).migrate([old])
        restored = MigrationExecutor(connection).loader.project_state([old]).apps.get_model("masterdata", "AssetCategory")
        assert list(restored.objects.filter(pk__in=[root.pk,child.pk]).order_by("pk").values()) == before
        assert restored.objects.get(pk=new.pk).category_type == "other"
        assert AuditLog.objects.filter(action="asset_category_type_restore_default", object_id=str(new.pk)).exists()
    finally:
        MigrationExecutor(connection).migrate(latest)
