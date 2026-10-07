"""Paper records keep their original completion, object scope and evidence visibility."""
from urllib.parse import urlencode

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.assets.models import Asset, AttachmentLink
from apps.audit.models import AuditLog
from apps.maintenance.models import MaintenancePlan, MaintenanceRecord, MaintenanceProblem
from apps.maintenance.services import close_maintenance_problem, upload_maintenance_attachment, void_maintenance_attachment, void_maintenance_record
from tests.test_sprint3_support import JPEG_BYTES, make_user
from tests.test_sprint9_services import _complete
from tests.test_sprint9_support import maintenance_context

pytestmark = pytest.mark.django_db


def state(context):
    return [list(model._base_manager.filter(company=context["company"]).order_by("pk").values())
        for model in (Asset,MaintenancePlan,MaintenanceRecord,MaintenanceProblem,AttachmentLink,AuditLog)]


def test_paper_prints_saved_completion_and_current_reference_with_safe_detail_return(client):
    context = maintenance_context("MAINTPAPER")
    record = _complete(context,"maint-paper-confirmed",actual_content="实盘完成 <绝缘检测>\nPAPER-LAST-CONTENT",
        result="problem_found",problem_description="锁扣松动",remark="保留现场备注")
    close_maintenance_problem(actor=context["equipment"],problem=record.problem,closure_note="紧固后复查通过",idempotency_key="maint-paper-close")
    client.force_login(context["equipment"])
    origin = reverse("maintenance:record-list") + "?" + urlencode({"q":"MAINTPAPER","page":2})
    query = {"return_to":origin,"assignment_page":2}
    before = state(context)
    detail = client.get(reverse("maintenance:record-detail",args=[record.pk]),query)
    printed = client.get(detail.context["record_print_url"])
    assert printed.status_code == 200
    body = printed.content.decode()
    assert "打印保养记录" in detail.content.decode()
    assert "实盘完成 &lt;绝缘检测&gt;" in body and "PAPER-LAST-CONTENT" in body
    assert "紧固后复查通过" in body and "已关闭" in body
    assert "计划当前标准 · 交接参考" in body
    assert context["plan"].standard_content in body
    assert context["responsible"].employee_no in body
    assert printed.context["detail_url"] == reverse("maintenance:record-detail",args=[record.pk]) + "?" + urlencode(query)
    assert "no-store" in printed["Cache-Control"] and printed["Referrer-Policy"] == "no-referrer"
    assert "csrfmiddlewaretoken" not in body and "原值" not in body and "1234.56" not in body
    assert state(context) == before
    assert client.post(reverse("maintenance:record-print",args=[record.pk])).status_code == 405


def test_paper_evidence_uses_detail_role_rules_and_excludes_voided_links(client,tmp_path):
    context = maintenance_context("MAINTPAPEREVIDENCE")
    record = _complete(context,"maint-paper-issue",result="problem_found",problem_description="等待复查")
    def photo(name):
        return SimpleUploadedFile(name,JPEG_BYTES,content_type="image/jpeg")
    with override_settings(MEDIA_ROOT=tmp_path):
        a0 = upload_maintenance_attachment(actor=context["equipment"],target=record,uploaded_file=photo("ordinary-record.jpg"))
        issue = upload_maintenance_attachment(actor=context["equipment"],target=record.problem,uploaded_file=photo("ordinary-issue.jpg"))
        private = upload_maintenance_attachment(actor=context["finance"],target=record,uploaded_file=photo("financial-private.jpg"),security_class="A1")
        old = upload_maintenance_attachment(actor=context["equipment"],target=record,uploaded_file=photo("voided-evidence.jpg"))
        void_maintenance_attachment(actor=context["equipment"],link=old,reason="重复证据")
    unrelated = make_user("maintenance-paper-unrelated","employee")
    hr = make_user("maintenance-paper-hr","hr")
    url = reverse("maintenance:record-print",args=[record.pk])
    for actor,expected in ((context["equipment"],{a0.pk,issue.pk}),(context["responsible_user"],{a0.pk,issue.pk}),(context["finance"],{a0.pk,issue.pk,private.pk})):
        client.force_login(actor)
        before = state(context)
        printed = client.get(url)
        detail = client.get(reverse("maintenance:record-detail",args=[record.pk]))
        assert printed.status_code == 200
        assert {row["link"].pk for row in printed.context["attachments"]} == expected
        assert {row["link"].pk for row in detail.context["attachments"]} == expected
        body = printed.content.decode()
        assert "ordinary-record.jpg" in body and "ordinary-issue.jpg" in body
        assert "voided-evidence.jpg" not in body
        assert ("financial-private.jpg" in body) == (private.pk in expected)
        assert "完成记录证据" in body and "问题处理证据" in body
        assert state(context) == before
    for actor in (unrelated,hr):
        client.force_login(actor)
        before = state(context)
        assert client.get(url).status_code == 403
        assert state(context) == before


def test_voided_paper_clearly_marks_historical_issue_without_mutating_records(client):
    context = maintenance_context("MAINTPAPERVOID")
    record = _complete(context,"maint-paper-void-source",result="problem_found",problem_description="错误登记的异常")
    void_maintenance_record(actor=context["equipment"],record=record,reason="重复填写，保留原件",idempotency_key="maint-paper-void")
    client.force_login(context["equipment"])
    before = state(context)
    printed = client.get(reverse("maintenance:record-print",args=[record.pk]))
    assert printed.status_code == 200
    body = printed.content.decode()
    assert "已作废 · 历史记录" in body and "重复填写，保留原件" in body
    assert "来源记录已作废 · 仅保留历史" in body
    assert "confirm" not in body and "已保存的实际完成内容" in body
    assert state(context) == before
