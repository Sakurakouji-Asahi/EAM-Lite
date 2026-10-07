from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.masterdata.models import Company
from apps.supplies.models import SupplyStockLedger
from apps.supplies.services import add_supply_count_item, record_supply_count
from tests.test_count_role_scope import count_role_scope
from tests.test_supply_count_entry_assistant import count_entry
from tests.test_sprint15_support import make_company, make_supply_item, make_supply_warehouse, make_user
from tests.test_sprint17_services import make_count


pytestmark = pytest.mark.django_db


def test_print_includes_all_matching_pages_preserves_return_and_exact_zero_without_writes(client, count_entry):
    task = count_entry
    actor = task.created_by
    original = task.lines.get(expected_quantity=Decimal("5.1234"))
    for index in range(26):
        item = make_supply_item(task.company, original.item.category, f"PRINT-{index:02d}")
        add_supply_count_item(actor=actor, task=task, item=item)
    record_supply_count(actor=actor, line=original, counted_quantity=Decimal("0"), remark="现场无实物")
    counts = (AuditLog.objects.count(), SupplyStockLedger.objects.count())
    query = {"q": "", "row_view": "", "page_size": "25", "page": "2"}
    detail = client.get(reverse("supplies:count-task-detail", args=[task.pk]), query)
    assert len(detail.context["page_obj"]) == 3
    printed = client.get(reverse("supplies:count-task-print", args=[task.pk]), query)
    assert printed.status_code == 200 and len(printed.context["print_lines"]) == 28
    assert printed.context["count_detail_url"].endswith("?q=&row_view=&page_size=25&page=2")
    html = printed.content.decode()
    assert f"0.0000 {original.item.unit}" in html and "未录入" in html and "包含该查询全部匹配页" in html
    assert "现场实盘" in html and "现场盘点人" in html
    assert "应盘金额" not in html and "单位成本快照" not in html
    filtered = client.get(reverse("supplies:count-task-print", args=[task.pk]), {"q": "PRINT-", "row_view": "unrecorded"})
    assert len(filtered.context["print_lines"]) == 26
    assert all(line.item_code_snapshot.startswith("PRINT-") for line in filtered.context["print_lines"])
    assert (AuditLog.objects.count(), SupplyStockLedger.objects.count()) == counts


def test_print_personal_and_managed_scope_never_includes_other_custodies_or_cost(client, count_role_scope):
    scope = count_role_scope
    client.force_login(scope.actor)
    printed = client.get(reverse("supplies:count-task-print", args=[scope.task_b.pk]))
    assert printed.status_code == 200
    assert {line.pk for line in printed.context["print_lines"]} == {scope.own_line.pk}
    html = printed.content.decode()
    assert scope.mine.name in html and scope.other.name not in html
    assert "数量盘点纸面核对表" in html and "责任部门 / 员工" in html
    assert "单位成本" not in html and "应盘金额" not in html
    managed = client.get(reverse("supplies:count-task-print", args=[scope.task_a.pk]))
    assert managed.status_code == 200 and len(managed.context["print_lines"]) == 2
    disallowed = client.get(reverse("supplies:count-task-print", args=[scope.task_b.pk]), {"row_view": "needs_cost"})
    assert disallowed.status_code == 400 and not disallowed.context["print_lines"]


def test_print_rejects_company_role_and_invalid_queries_and_is_read_only(client, count_entry):
    task = count_entry
    other = make_company("PRINT-OTHER", active=False)
    Company.objects.filter(pk=task.company_id).update(is_active=False)
    Company.objects.filter(pk=other.pk).update(is_active=True)
    try:
        foreign = make_count(actor=task.created_by, company=other, domain="warehouse_stock",
            warehouse=make_supply_warehouse(other, "PRINT-OTHER-WH"), key="print-foreign-task")
    finally:
        Company.objects.filter(pk=other.pk).update(is_active=False)
        Company.objects.filter(pk=task.company_id).update(is_active=True)
    assert client.get(reverse("supplies:count-task-print", args=[foreign.pk])).status_code == 404
    url = reverse("supplies:count-task-print", args=[task.pk])
    invalid = client.get(url, {"page_size": "99999"})
    assert invalid.status_code == 400 and not invalid.context["print_lines"]
    empty = client.get(url, {"q": "NO-COUNT-MATCH"})
    assert empty.status_code == 200 and "当前查询没有可打印的明细" in empty.content.decode()
    assert client.post(url, {"counted_quantity": "0"}).status_code == 405
    client.force_login(make_user("print-count-hr", "hr"))
    assert client.get(url).status_code == 404
