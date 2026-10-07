from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.urls import reverse

from apps.assets.models import AssetLabelPrintBatch, AssetLabelPrintItem, AssetMovement, AssetQrIdentity
from apps.assets.qr_services import generate_print_batch, rotate_qr_identity
from apps.audit.models import AuditLog
from tests.test_label_batch_followup import _attach
from tests.test_label_queue_usability import make_asset
from tests.test_sprint3_support import make_company, make_user
from tests.test_sprint6_qr_services import _legacy_generated
from tests.test_unified_asset_identity import context


pytestmark = pytest.mark.django_db


def test_list_progress_matches_whole_batch_and_current_identity_without_writes(context, client):
    pending, attached, inactive, waiting, complete = [make_asset(context, i) for i in range(1, 6)]
    mixed = generate_print_batch(actor=context["finance"], assets=[pending, attached, inactive], idempotency_key="progress-mixed")
    _attach(context, attached, "progress-attached")
    _attach(context, inactive, "progress-inactive")
    rotate_qr_identity(actor=context["finance"], asset=inactive, reason="标签损坏")
    legacy = _legacy_generated(context, waiting, "progress-waiting")
    done = generate_print_batch(actor=context["finance"], assets=[complete], idempotency_key="progress-complete")
    _attach(context, complete, "progress-completed")
    client.force_login(context["finance"])
    models = (AssetQrIdentity, AssetLabelPrintBatch, AssetLabelPrintItem, AssetMovement, AuditLog)
    before = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
    url = reverse("assets:label-batch-list")
    page = client.get(url)
    assert page.status_code == 200
    rows = {row.pk: row for row in page.context["batches"]}
    assert (rows[mixed.pk].item_count, rows[mixed.pk].pending_count, rows[mixed.pk].attached_count,
            rows[mixed.pk].inactive_count, rows[mixed.pk].waiting_count) == (3, 1, 1, 1, 0)
    assert rows[legacy.pk].waiting_count == 1
    assert rows[done.pk].attached_count == 1
    for progress, expected in (("pending", mixed.pk), ("attached", done.pk), ("inactive", mixed.pk)):
        response = client.get(url, {"progress": progress})
        assert [row.pk for row in response.context["batches"]] == [expected]
    searched = client.get(url, {"q": pending.equipment_number})
    assert [(row.pk, row.item_count, row.attached_count) for row in searched.context["batches"]] == [(mixed.pk, 3, 1)]
    assert client.get(url, {"status": "generated", "progress": "pending"}).context["page_obj"].paginator.count == 0
    assert {model.__name__: list(model.objects.order_by("pk").values()) for model in models} == before


def test_second_page_context_survives_detail_query_and_web_attachment(context, client):
    asset = make_asset(context, 1)
    for index in range(26):
        generate_print_batch(actor=context["finance"], assets=[asset], idempotency_key=f"progress-page-{index}", explicit_reprint=bool(index))
    client.force_login(context["finance"])
    query = {"q": asset.equipment_number, "status": "printed", "progress": "pending", "page": "2"}
    response = client.get(reverse("assets:label-batch-list"), query)
    assert response.context["page_obj"].number == 2
    assert len(response.context["batches"]) == 1
    batch = response.context["batches"][0]
    detail = client.get(batch.pending_url)
    assert parse_qs(urlsplit(detail.context["label_list_url"]).query) == {k: [v] for k, v in query.items()}
    list_query = detail.context["label_list_query"]
    assert detail.context["filter_hidden_fields"] == {"list_query": list_query}
    queried = client.get(reverse("assets:label-batch-detail", args=[batch.pk]), {"list_query": list_query, "q": asset.equipment_number, "work": "pending"})
    assert queried.context["label_list_url"] == detail.context["label_list_url"]
    attach_url = reverse("assets:qr-web-attach", args=[asset.pk])
    form_page = client.get(attach_url, {"return_batch": batch.pk, "list_query": list_query})
    payload = {"return_batch": str(batch.pk), "list_query": list_query, "qr_identity_id": str(asset.qr_identities.get(status="active").pk),
               "idempotency_key": form_page.context["form"]["idempotency_key"].value(), "target_status": "in_use"}
    invalid = client.post(attach_url, payload)
    assert invalid.status_code == 400
    assert invalid.context["label_list_query"] == list_query
    saved = client.post(attach_url, {**payload, "label_attached": "on", "responsibility_confirmed": "on"})
    assert saved.status_code == 302
    assert parse_qs(urlsplit(saved.url).query) == {"work": ["pending"], "list_query": [list_query]}
    assert client.get(saved.url).context["label_counts"]["pending"] == 0
    assert client.get(reverse("assets:label-batch-list"), {"progress": "attached"}).context["page_obj"].paginator.count == 26


def test_progress_filters_validate_and_list_return_stays_in_authorized_route(context, client):
    asset = make_asset(context, 1)
    batch = generate_print_batch(actor=context["finance"], assets=[asset], idempotency_key="progress-access")
    foreign = AssetLabelPrintBatch.objects.create(company=make_company("PROGRESS-OTHER", active=False), batch_code="foreign-batch",
        template_version="a4-v1", status="generated", created_by=context["finance"], idempotency_key="foreign-batch")
    client.force_login(context["finance"])
    url = reverse("assets:label-batch-list")
    assert foreign.pk not in [row.pk for row in client.get(url).context["batches"]]
    assert client.get(url, {"progress": "unknown"}).status_code == 400
    assert client.get(url, {"status": "unknown"}).status_code == 400
    malicious = urlencode({"next": "https://outside.example/", "page": "-2", "progress": "pending", "status": "printed"})
    detail = client.get(reverse("assets:label-batch-detail", args=[batch.pk]), {"list_query": malicious})
    assert detail.context["label_list_url"] == url + "?status=printed&progress=pending"
    client.force_login(make_user("progress-hr", "hr"))
    assert client.get(url).status_code == 403
    assert client.get(reverse("assets:label-batch-detail", args=[batch.pk])).status_code == 403
