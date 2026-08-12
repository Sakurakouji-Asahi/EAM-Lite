import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections
from openpyxl import load_workbook

from apps.imports.services import build_template_workbook, upload_and_validate_import
from apps.masterdata.models import Company, ImportBatch


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def isolated_import_media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture(autouse=True)
def require_postgresql(django_db_blocker):
    from django.db import connection

    with django_db_blocker.unblock():
        if connection.vendor != "postgresql":
            pytest.skip("Sprint 1 import concurrency requires PostgreSQL")


def _department_workbook():
    book = load_workbook(io.BytesIO(build_template_workbook("department")))
    book["部门导入"].append(["D1", "并发部门", "", "", "是"])
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def _upload(*, company_id, user_id, content, idempotency_key, barrier):
    close_old_connections()
    try:
        barrier.wait(timeout=10)
        return (
            "ok",
            upload_and_validate_import(
                actor=get_user_model().objects.get(pk=user_id),
                company=Company.objects.get(pk=company_id),
                import_type="department",
                uploaded_file=SimpleUploadedFile(
                    "department.xlsx",
                    content,
                    content_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                ),
                idempotency_key=idempotency_key,
            ).pk,
        )
    except ValidationError as exc:
        return "validation", "; ".join(exc.messages)
    finally:
        close_old_connections()


def _setup():
    company = Company.objects.create(
        code="C1", normalized_code="c1", name="测试公司", short_name="测试"
    )
    user = get_user_model().objects.create_user(
        username="admin",
        password="Valid-Password-2026!",
        display_name="管理员",
    )
    group, _ = Group.objects.get_or_create(name="system_admin")
    user.groups.add(group)
    return company, user


def test_concurrent_same_idempotency_key_returns_one_batch():
    company, user = _setup()
    content = _department_workbook()
    barrier = threading.Barrier(2)
    key = "concurrent-same-idempotency-key"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: _upload(
                    company_id=company.pk,
                    user_id=user.pk,
                    content=content,
                    idempotency_key=key,
                    barrier=barrier,
                ),
                range(2),
            )
        )

    assert results[0] == results[1]
    assert results[0][0] == "ok"
    assert ImportBatch.objects.filter(company=company).count() == 1


def test_concurrent_same_digest_with_different_keys_rejects_duplicate():
    company, user = _setup()
    content = _department_workbook()
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _upload,
                company_id=company.pk,
                user_id=user.pk,
                content=content,
                idempotency_key=f"concurrent-digest-{index}",
                barrier=barrier,
            )
            for index in range(2)
        ]
        results = [future.result(timeout=30) for future in futures]

    assert sorted(result[0] for result in results) == ["ok", "validation"]
    assert "已上传为批次" in next(
        result[1] for result in results if result[0] == "validation"
    )
    assert ImportBatch.objects.filter(company=company).count() == 1
