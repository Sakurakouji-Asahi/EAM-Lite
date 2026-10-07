from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.supplies.models import SupplyCustody, SupplyCustodyMovement, SupplyStockBalance, SupplyStockLedger
from apps.supplies.services import post_supply_document, return_custody_to_warehouse, reverse_supply_document, transfer_custody
from tests.test_sprint16_services import issued_custody
from tests.test_sprint15_support import make_department, make_employee, make_user


pytestmark = pytest.mark.django_db


@pytest.fixture
def timeline_context(client):
    company, actor, department, employee, warehouse, return_wh, chair, original, custody = issued_custody(quantity="3.4567", unit_cost="10")
    other_department = make_department(company, "OTHER-SECRET-DEPT")
    other_employee = make_employee(company, other_department, "OTHER-SECRET-EMP")
    target = transfer_custody(custody=custody, quantity=Decimal("1.1111"), target_department=other_department,
        target_employee=other_employee, business_date=date(2026, 8, 27), reason="换岗交接记录",
        actor=actor, idempotency_key="timeline-transfer")
    returned = return_custody_to_warehouse(custody=custody, target_warehouse=return_wh, quantity=Decimal("0.2345"),
        business_date=date(2026, 8, 28), reason="归还现场核对", actor=actor, idempotency_key="timeline-return")
    post_supply_document(actor=actor, document=returned)
    reverse_supply_document(document=returned, actor=actor, reason="归还仓库选择错误", idempotency_key="timeline-return-reversal")
    client.force_login(actor)
    return {"company":company, "actor":actor, "department":department, "employee":employee,
        "other_department":other_department, "other_employee":other_employee, "custody":custody, "target":target}


def test_timeline_explains_business_date_exact_transfer_parties_and_paired_reversal_without_summing(client, timeline_context):
    custody = timeline_context["custody"]
    origin = reverse("supplies:custody-list") + "?item=CHAIR&status=open&page=2"
    page = client.get(reverse("supplies:custody-detail", args=[custody.pk]), {"return_to": origin})
    assert page.status_code == 200
    movements = page.context["movements"]
    assert [movement.action for movement in movements] == ["issue", "transfer", "return", "reversal"]
    transfer = movements[1]
    assert transfer.quantity == Decimal("1.1111") and transfer.history_from["current"]
    assert str(timeline_context["other_department"]) in transfer.history_to["label"]
    assert str(timeline_context["other_employee"]) in transfer.history_to["label"]
    assert urlsplit(transfer.history_to["url"]).path == reverse("supplies:custody-detail", args=[timeline_context["target"].pk])
    assert parse_qs(urlsplit(transfer.history_to["url"]).query) == {"return_to": [origin]}
    assert movements[3].history_original["url"] == "#" + movements[2].history_anchor
    assert movements[2].history_reversal["url"] == "#" + movements[3].history_anchor
    html = page.content.decode()
    assert "业务日期 2026-08-27" in html and "记录时间" in html
    assert "1.1111 把" in html and "0.2345 把" in html
    assert "该动作已冲销" in html and "本次冲销对应" in html
    assert "查看完整流水表与成本记录" in html and "<details" in html
    assert page.context["custody_list_url"] == origin


def test_timeline_redacts_related_employee_department_and_record_links_outside_self_scope(client, timeline_context):
    user = make_user("timeline-original-person", "employee")
    employee = timeline_context["employee"]
    employee.user = user
    employee.save(update_fields=["user"])
    client.force_login(user)
    page = client.get(reverse("supplies:custody-detail", args=[timeline_context["custody"].pk]))
    html = page.content.decode()
    transfer = next(movement for movement in page.context["movements"] if movement.action == "transfer")
    assert transfer.history_to == {"label":"范围外保管（不可查看）", "url":""}
    assert "OTHER-SECRET" not in html
    assert not page.context["show_cost"] and "单位成本" not in html and "当前金额" not in html
    assert client.get(reverse("supplies:custody-detail", args=[timeline_context["target"].pk])).status_code == 404
    other_user = make_user("timeline-target-person", "employee")
    other_employee = timeline_context["other_employee"]
    other_employee.user = other_user
    other_employee.save(update_fields=["user"])
    client.force_login(other_user)
    target_page = client.get(reverse("supplies:custody-detail", args=[timeline_context["target"].pk]))
    assert target_page.status_code == 200 and not target_page.context["direct_parent_visible"]
    assert target_page.context["movements"][0].history_from == {"label":"范围外保管（不可查看）", "url":""}
    assert not target_page.context["ancestor_chain"]
    assert str(timeline_context["department"]) not in target_page.content.decode()
    assert str(timeline_context["employee"]) not in target_page.content.decode()


def test_timeline_and_child_navigation_are_read_only_keep_query_and_existing_role_rejection(client, timeline_context):
    before = (list(SupplyCustody.objects.order_by("pk").values()), list(SupplyCustodyMovement.objects.order_by("pk").values()),
        list(SupplyStockBalance.objects.order_by("pk").values()), list(SupplyStockLedger.objects.order_by("pk").values()), AuditLog.objects.count())
    origin = reverse("supplies:my-custodies") + "?q=CHAIR&status=all&page=2"
    page = client.get(reverse("supplies:custody-detail", args=[timeline_context["custody"].pk]), {"return_to":origin})
    target_url = next(movement for movement in page.context["movements"] if movement.action == "transfer").history_to["url"]
    target_page = client.get(target_url)
    assert target_page.context["custody_list_url"] == origin
    assert target_page.context["movements"][0].history_to["current"]
    parent_url = target_page.context["movements"][0].history_from["url"]
    assert client.get(parent_url).context["custody_list_url"] == origin
    assert before == (list(SupplyCustody.objects.order_by("pk").values()), list(SupplyCustodyMovement.objects.order_by("pk").values()),
        list(SupplyStockBalance.objects.order_by("pk").values()), list(SupplyStockLedger.objects.order_by("pk").values()), AuditLog.objects.count())
    client.force_login(make_user("timeline-hr", "hr"))
    assert client.get(reverse("supplies:custody-detail", args=[timeline_context["custody"].pk])).status_code == 403
