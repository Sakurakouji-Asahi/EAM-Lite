from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.inventory.forms import InventoryScanForm, InventoryTaskForm
from apps.inventory.services import publish_inventory_task
from tests.test_sprint3_support import make_user
from tests.test_sprint4_acceptance import _base_context
from tests.test_sprint8_services import _draft


pytestmark = pytest.mark.django_db


def test_task_form_rejects_result_finance_and_wrong_scope_fields():
    context = _base_context("S8FORMTASK")
    form = InventoryTaskForm(
        data={
            "name": "全盘",
            "inventory_type": "full",
            "scope_type": "company",
            "scope_department": context["department"].pk,
            "planned_start": timezone.localdate(),
            "planned_end": timezone.localdate() + timedelta(days=1),
            "result": "normal",
            "original_cost": "999999.99",
        },
        actor=context["finance"],
        company=context["company"],
    )
    assert not form.is_valid()
    assert "全公司范围不得同时提交其他范围字段" in str(form.errors)
    assert "result" not in form.fields
    assert "original_cost" not in form.fields


def test_scan_form_never_accepts_client_result_or_financial_fields():
    context = _base_context("S8FORMSCAN")
    assignee = make_user("s8-form-assignee", "employee")
    task = publish_inventory_task(
        actor=context["finance"],
        task=_draft(context, "S8FORMSCAN-T", assignees=[assignee]),
    )
    form = InventoryScanForm(
        data={
            "idempotency_key": "S8FORMSCAN-scan",
            "actual_location": "",
            "actual_employee": "",
            "actual_status": "in_use",
            "result": "normal",
            "original_cost": "888.88",
            "other_mismatch": "",
            "note": "",
        },
        actor=assignee,
        task=task,
    )
    assert form.is_valid(), form.errors
    assert "result" not in form.fields
    assert "original_cost" not in form.fields

    outsider = make_user("s8-form-outsider", "employee")
    with pytest.raises(PermissionDenied):
        InventoryScanForm(actor=outsider, task=task)
