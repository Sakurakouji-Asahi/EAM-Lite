"""Production-path factories for Sprint 10 offboarding acceptance tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.assets.lifecycle_services import loan_asset
from apps.assets.models import AssetQrIdentity
from apps.assets.qr_services import (
    confirm_label_attachment,
    confirm_print_batch,
    generate_print_batch,
)
from apps.assets.services import submit_asset_for_finance
from tests.test_sprint3_support import (
    add_photo,
    make_asset,
    make_employee,
    make_user,
)
from tests.test_sprint4_acceptance import _base_context, _confirm_nonfixed


def offboarding_context(prefix: str) -> dict:
    context = _base_context(prefix)
    context["hr"] = make_user(f"{prefix.lower()}-hr", "hr")
    context["management"] = make_user(
        f"{prefix.lower()}-management", "management"
    )
    context["warehouse"] = make_user(
        f"{prefix.lower()}-warehouse", "warehouse"
    )
    context["employee_user"] = make_user(
        f"{prefix.lower()}-employee", "employee"
    )
    employee = context["employee"]
    employee.user = context["employee_user"]
    employee.hire_date = timezone.localdate() - timedelta(days=90)
    employee.save(update_fields=["user", "hire_date", "updated_at"])
    return context


def additional_employee(context: dict, suffix: str, *, user=None):
    employee = make_employee(
        context["company"],
        context["department"],
        f"{suffix}-E",
        user=user,
    )
    employee.hire_date = timezone.localdate() - timedelta(days=180)
    employee.save(update_fields=["hire_date", "updated_at"])
    return employee


def formal_asset(
    context: dict,
    suffix: str,
    *,
    employee=None,
    activate: bool = True,
    status: str = "in_use",
    cost=Decimal("987654.32"),
):
    """Create a real formal asset, optionally leaving it at pending_label."""

    employee = employee or context["employee"]
    asset = make_asset(
        actor=context["equipment"],
        company=context["company"],
        category=context["category"],
        department=employee.department,
        employee=employee,
        location=context["location"],
        asset_name=f"{suffix} 清退测试资产",
        serial_number=f"SN-{suffix}",
        factory_number=f"FN-{suffix}",
        commissioning_date=timezone.localdate(),
    )
    add_photo(context["equipment"], asset)
    asset = submit_asset_for_finance(actor=context["equipment"], asset=asset)
    asset = _confirm_nonfixed(
        context,
        asset,
        cost=cost,
        key=f"{suffix}-formalize",
        treatment_reason="Sprint 10 专项验收用受控非固定资产",
        actor=context["finance"],
    )
    qr = AssetQrIdentity.objects.get(asset=asset, status="active")
    if activate:
        batch = generate_print_batch(
            actor=context["finance"],
            assets=[asset],
            idempotency_key=f"{suffix}-print",
        )
        confirm_print_batch(actor=context["finance"], batch=batch)
        confirm_label_attachment(
            actor=context["finance"],
            asset=asset,
            scanned_token=qr.public_token,
            target_status=status,
            idempotency_key=f"{suffix}-attach",
        )
        asset.refresh_from_db()
        qr.refresh_from_db()
    return asset, qr


def active_internal_loan(context: dict, asset, borrower, suffix: str):
    return loan_asset(
        actor=context["equipment"],
        asset=asset,
        borrower_type="internal_employee",
        borrower_employee=borrower,
        borrower_name="",
        borrower_organization="",
        loan_date=timezone.localdate(),
        expected_return_date=timezone.localdate() + timedelta(days=7),
        handled_by=context["equipment"],
        reason="Sprint 10 清退借用快照",
        idempotency_key=f"{suffix}-loan",
        expected_status=asset.asset_status,
    )
