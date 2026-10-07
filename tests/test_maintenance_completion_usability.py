"""The execution form keeps the plan in view and never replaces user input."""

from datetime import timedelta

import pytest
from django.urls import reverse

from apps.maintenance.domain import business_date
from apps.maintenance.services import update_maintenance_plan
from tests.test_sprint9_support import maintenance_context


pytestmark = pytest.mark.django_db


def test_completion_displays_standard_and_asset_and_defaults_today_without_auto_reporting(client):
    context = maintenance_context("MAINTUX")
    context["asset"].equipment_number = "MAINT-EQ-CONTEXT"
    context["asset"].save(update_fields=["equipment_number"])
    client.force_login(context["responsible_user"])
    page = client.get(reverse("maintenance:plan-complete", args=[context["plan"].pk]))
    assert page.status_code == 200
    html = page.content.decode()
    assert "MAINT-EQ-CONTEXT" in html
    assert context["asset"].asset_name in html
    assert context["responsible"].name in html
    assert context["plan"].standard_content in html
    assert "引用到实际完成内容" in html
    assert "maintenance-completion.js" in html
    assert "原值" not in html and "1234.56" not in html
    form = page.context["form"]
    assert form["actual_content"].value() is None
    assert form["completed_date"].value() == business_date()
    assert form.fields["completed_date"].widget.attrs["max"] == business_date().isoformat()
    assert form.fields["scheduled_date"].disabled
    assert form["scheduled_date"].value() == context["plan"].next_maintenance_date


def test_completion_errors_preserve_the_actual_report_and_selected_date(client):
    context = maintenance_context("MAINTUXINPUT")
    client.force_login(context["responsible_user"])
    url = reverse("maintenance:plan-complete", args=[context["plan"].pk])
    opened = client.get(url)
    form = opened.context["form"]
    chosen_date = (business_date() - timedelta(days=1)).isoformat()
    payload = {
        "idempotency_key": form["idempotency_key"].value(),
        "completion_instance": form["completion_instance"].value(),
        "completed_date": chosen_date, "actual_content": "已经完成紧固和润滑，测量温度 36 ℃",
        "result": "problem_found", "problem_description": "", "remark": "用户已写好的备注",
    }
    failed = client.post(url, payload)
    assert failed.status_code == 200
    bound = failed.context["form"]
    assert "发现问题时必须填写问题说明。" in failed.content.decode()
    assert bound["actual_content"].value() == payload["actual_content"]
    assert bound["completed_date"].value() == chosen_date
    assert bound["remark"].value() == payload["remark"]
    assert bound["completion_instance"].value() == payload["completion_instance"]
    payload["completed_date"] = ""
    blank_date = client.post(url, payload)
    assert blank_date.context["form"]["completed_date"].value() == ""
    assert "completed_date" in blank_date.context["form"].errors
    assert context["plan"].records.count() == 0


def test_standard_content_is_escaped_in_the_reference_and_copy_source(client):
    context = maintenance_context("MAINTUXESCAPE")
    plan = context["plan"]
    standard = '</script><script>alert("标准内容")</script>\n检查 & 记录'
    update_maintenance_plan(
        actor=context["equipment"], plan=plan, name=plan.name,
        cycle_value=plan.cycle_value, cycle_unit=plan.cycle_unit,
        responsible_employee=plan.responsible_employee,
        advance_notice_days=plan.advance_notice_days, standard_content=standard,
        first_due_date=plan.first_due_date,
    )
    client.force_login(context["responsible_user"])
    page = client.get(reverse("maintenance:plan-complete", args=[plan.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    assert standard not in html
    assert "&lt;script&gt;" in html
    assert "\\u003C/script\\u003E" in html
