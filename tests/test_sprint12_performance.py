from __future__ import annotations

import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.template.loader import render_to_string
from django.urls import reverse

from apps.accounts.models import User
from apps.assets.models import Asset
from apps.assets.qr_services import render_qr_svg
from tests.test_sprint4_acceptance import _base_context
from tests.test_sprint7_support import active_asset_context


pytestmark = pytest.mark.django_db(transaction=True)


def _p95(values):
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


def test_postgresql_5000_asset_authenticated_read_p95_limits():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 12 capacity acceptance requires PostgreSQL")
    context = _base_context("S12PERF")
    drafts = [
        Asset(
            company=context["company"],
            asset_name=f"容量资产 {index:05d}",
            category=context["category"],
            department=context["department"],
            responsible_employee=context["employee"],
            location=context["location"],
            created_by=context["equipment"],
            updated_by=context["equipment"],
            initialized_by=context["equipment"],
        )
        for index in range(5000)
    ]
    Asset._base_manager.bulk_create(drafts, batch_size=500)
    User._base_manager.bulk_create(
        [
            User(
                username=f"s12-perf-user-{index:03d}",
                display_name=f"容量用户 {index:03d}",
                password="!",
                is_active=True,
            )
            for index in range(100)
        ],
        batch_size=100,
    )
    actor_id = context["equipment"].pk

    def measured_get(url):
        close_old_connections()
        try:
            from apps.accounts.models import User

            actor = User.objects.get(pk=actor_id)
            client = Client()
            client.force_login(actor)
            started = time.perf_counter()
            response = client.get(url)
            elapsed = time.perf_counter() - started
            assert response.status_code == 200
            return elapsed
        finally:
            close_old_connections()

    asset_url = reverse("assets:asset-list") + "?q=容量资产&page=1"
    dashboard_url = reverse("home")
    measured_get(asset_url)
    measured_get(dashboard_url)
    with ThreadPoolExecutor(max_workers=10) as pool:
        asset_samples = list(pool.map(measured_get, [asset_url] * 20))
        dashboard_samples = list(pool.map(measured_get, [dashboard_url] * 20))
    print(
        {
            "asset_list_p95_seconds": round(_p95(asset_samples), 4),
            "dashboard_p95_seconds": round(_p95(dashboard_samples), 4),
        }
    )
    assert _p95(asset_samples) <= 3.0, statistics.fmean(asset_samples)
    assert _p95(dashboard_samples) <= 5.0, statistics.fmean(dashboard_samples)


def test_postgresql_asset_detail_p95_limit_with_permission_checks():
    if connection.vendor != "postgresql":
        pytest.skip("Sprint 12 capacity acceptance requires PostgreSQL")
    context, asset, _qr = active_asset_context("S12DETAILPERF")
    actor = context["equipment"]
    client = Client()
    client.force_login(actor)
    url = reverse("assets:asset-detail", args=[asset.pk])
    assert client.get(url).status_code == 200
    samples = []
    for _ in range(20):
        started = time.perf_counter()
        response = client.get(url)
        samples.append(time.perf_counter() - started)
        assert response.status_code == 200
    print({"asset_detail_p95_seconds": round(_p95(samples), 4)})
    assert _p95(samples) <= 3.0


def test_500_label_a4_preview_and_qr_render_under_60_seconds():
    batch = SimpleNamespace(
        pk=uuid.uuid4(),
        batch_code="S12-500-LABELS",
        include_responsible_employee=True,
        include_location=True,
        include_model=True,
    )
    items = []
    for index in range(500):
        items.append(
            SimpleNamespace(
                pk=uuid.uuid4(),
                page_no=(index // 24) + 1,
                label_snapshot_json={
                    "company_short_name": "验收公司",
                    "asset_name": f"容量标签资产 {index:04d}",
                    "asset_code": f"S12-{index:05d}",
                    "department": "生产设备部",
                    "responsible_employee": "容量责任人",
                    "location": "厂区 / 车间 / 具体位置",
                    "model": "MODEL-500",
                },
            )
        )
    started = time.perf_counter()
    html = render_to_string("assets/label_print.html", {"batch": batch, "items": items})
    for index in range(500):
        svg = render_qr_svg(
            SimpleNamespace(public_token=(f"S12{index:040d}"[-43:]))
        )
        assert "<svg" in svg
    elapsed = time.perf_counter() - started
    assert html.count('class="qr-label"') == 500
    print({"label_500_render_seconds": round(elapsed, 4)})
    assert elapsed <= 60
