"""A hidden submission error remains visible without causing a finance action."""
from datetime import date

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.finance.models import DepreciationBatch, DepreciationEntry
from tests.test_sprint3_support import complete_initialization, make_company, make_user

pytestmark=pytest.mark.django_db


def test_missing_hidden_request_key_is_explained_and_batch_remains_unchanged(client):
    company=make_company("FINANCE-FORM-ERROR")
    finance=make_user("finance-form-error", "finance")
    complete_initialization(company,finance)
    batch=DepreciationBatch.objects.create(company=company,period_start=date(2026,9,1),
        period_end=date(2026,10,1),batch_type="regular",idempotency_key="finance-form-error",
        request_hash="0"*64,generated_by=finance,generated_at=timezone.now())
    before=DepreciationBatch.objects.filter(pk=batch.pk).values().get()
    client.force_login(finance)
    audit_before=AuditLog.objects.count()
    query="period=2026-09&status=draft&page=2"
    response=client.post(reverse("finance:batch-reverse",args=[batch.pk]),
        {"reason":"保留当前原因", "confirm":"on", "batch_query":query})
    assert response.status_code==200
    form=response.context["form"]
    assert "idempotency_key" in form.errors
    html=response.content.decode()
    assert "data-finance-form-errors" in html and "提交校验信息" in html
    assert "请刷新页面后重新核对并提交" in html
    assert form["reason"].value()=="保留当前原因" and form["confirm"].value()
    assert response.context["batch_query"]==query
    assert DepreciationBatch.objects.filter(pk=batch.pk).values().get()==before
    assert DepreciationBatch.objects.count()==1 and not DepreciationEntry.objects.exists()
    assert AuditLog.objects.count()==audit_before
    readonly=make_user("finance-form-readonly", "management")
    client.force_login(readonly)
    assert client.get(reverse("finance:policy-create")).status_code==403
