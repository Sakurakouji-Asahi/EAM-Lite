from __future__ import annotations

import importlib
from types import SimpleNamespace

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
import pytest

from apps.assets.models import AssetCodeHistory, AssetQrIdentity
from apps.finance.models import AssetFinance, FinanceFormalizationRequest
from apps.finance.services import confirm_asset_finance
from apps.masterdata.models import (
    Company,
    Department,
    Employee,
    IssuedCode,
    Location,
    SequenceCounter,
    UserDepartmentScope,
)
from apps.masterdata.services import create_employee, create_location
from tests.test_sprint4_acceptance import _base_context, _pending_asset
from tests.test_sprint3_support import make_user
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db


def test_create_employee_rejects_user_with_another_company_scope():
    active_company = Company.objects.create(
        code="ACTIVE",
        normalized_code="active",
        name="当前公司",
        short_name="当前",
    )
    other_company = Company.objects.create(
        code="OTHER",
        normalized_code="other",
        name="历史公司",
        short_name="历史",
        is_active=False,
    )
    active_department = Department.objects.create(
        company=active_company,
        code="ACTIVE-D",
        normalized_code="active-d",
        name="当前部门",
    )
    other_department = Department.objects.create(
        company=other_company,
        code="OTHER-D",
        normalized_code="other-d",
        name="历史部门",
    )
    actor = make_user("cross-company-hr-admin", "hr")
    actor.groups.add(Group.objects.get_or_create(name="system_admin")[0])
    target = make_user("cross-company-target")
    UserDepartmentScope.objects.create(
        company=other_company,
        user=target,
        department=other_department,
    )

    with pytest.raises(ValidationError, match="跨公司"):
        create_employee(
            actor=actor,
            company=active_company,
            data={
                "employee_no": "E-CROSS",
                "name": "跨公司账号",
                "department": active_department,
                "user": target,
            },
        )

    assert not Employee.objects.filter(user=target).exists()


def test_formalization_code_collision_is_business_error_and_rolls_back():
    context = _base_context("HARDENCODE")
    asset = _pending_asset(context, "collision")
    existing = IssuedCode.objects.create(
        company=context["company"],
        coding_scheme=context["scheme"],
        scope_key="historical-reservation",
        sequence_value=999,
        display_code="0001",
        normalized_code="0001",
        effective_date=asset.commissioning_date,
        status="active",
        idempotency_key="historical-code-reservation",
        issued_by=context["admin"],
    )
    before = {
        "finance": AssetFinance.objects.count(),
        "counter": SequenceCounter.objects.count(),
        "issued": IssuedCode.objects.count(),
        "history": AssetCodeHistory.objects.count(),
        "qr": AssetQrIdentity.objects.count(),
        "request": FinanceFormalizationRequest.objects.count(),
    }

    with pytest.raises(ValidationError, match="正式编号已被永久占用"):
        confirm_asset_finance(
            actor=context["finance"],
            asset=asset,
            finance_data={
                "accounting_treatment": "controlled_non_fixed",
                "original_cost": "100.00",
            },
            code_effective_date=asset.commissioning_date,
            idempotency_key="formalize-colliding-code",
            reason="验证永久占号冲突回滚",
        )

    asset.refresh_from_db()
    assert asset.asset_status == "pending_finance"
    assert asset.asset_code is None
    assert IssuedCode.objects.get(pk=existing.pk).display_code == "0001"
    assert {
        "finance": AssetFinance.objects.count(),
        "counter": SequenceCounter.objects.count(),
        "issued": IssuedCode.objects.count(),
        "history": AssetCodeHistory.objects.count(),
        "qr": AssetQrIdentity.objects.count(),
        "request": FinanceFormalizationRequest.objects.count(),
    } == before


def test_current_formal_asset_location_cannot_become_a_parent():
    context, asset, _qr = active_asset_context("HARDLEAF")

    with pytest.raises(ValidationError, match="当前正式资产.*不能.*新增"):
        create_location(
            actor=context["admin"],
            company=context["company"],
            data={
                "code": "HARDLEAF-CHILD",
                "name": "不应创建的下级位置",
                "parent": asset.location,
                "location_type": "position",
            },
        )

    assert not Location.objects.filter(
        company=context["company"], parent=asset.location
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_postgresql_rejects_raw_child_under_current_asset_location():
    if connection.vendor != "postgresql":
        pytest.skip("当前资产位置叶级并发约束需要 PostgreSQL 18.6")
    context, asset, _qr = active_asset_context("HARDLEAFDB")

    with pytest.raises(IntegrityError, match="must remain a leaf"), transaction.atomic():
        Location.objects.create(
            company=context["company"],
            code="HARDLEAFDB-CHILD",
            normalized_code="hardleafdb-child",
            name="绕过服务的下级位置",
            parent=asset.location,
            location_type="position",
        )


def test_location_leaf_migration_rejects_preexisting_invalid_assets():
    if connection.vendor == "postgresql":
        pytest.skip("PostgreSQL 当前触发器已阻止构造升级前违规夹具。")
    context, asset, _qr = active_asset_context("HARDLEAFUPGRADE")
    Location.objects.create(
        company=context["company"],
        code="HARDLEAFUPGRADE-CHILD",
        normalized_code="hardleafupgrade-child",
        name="升级前遗留下级位置",
        parent=asset.location,
        location_type="position",
    )
    migration = importlib.import_module(
        "apps.assets.migrations.0014_production_location_leaf_guard"
    )

    with pytest.raises(RuntimeError, match="1 项当前正式资产.*1 个非叶级位置"):
        migration.ensure_existing_locations_are_valid(
            None,
            SimpleNamespace(connection=connection),
        )
