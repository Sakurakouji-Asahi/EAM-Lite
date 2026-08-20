from datetime import date

import pytest
from django.urls import reverse

from tests.test_correction_finance_services import _custom_profile_context


pytestmark = pytest.mark.django_db


def test_pending_continuation_review_page_is_finance_only_and_actionable(client):
    _company, finance, management, _admin, _asset, _finance, profile = (
        _custom_profile_context(method="straight_line", review_required=True)
    )
    url = reverse("finance:profile-continuation-review", args=[profile.pk])

    client.force_login(management)
    assert client.get(url).status_code == 403

    client.force_login(finance)
    detail = client.get(
        reverse("finance:asset-finance-detail", args=[profile.asset_id])
    )
    assert detail.status_code == 200
    assert "立即复核" in detail.content.decode()
    assert url in detail.content.decode()
    response = client.get(url)
    assert response.status_code == 200
    assert "实际接续日" in response.content.decode()
    response = client.post(
        url,
        {
            "actual_continuation_date": "2024-01-16",
            "reason": "核对原系统折旧承接台账",
            "confirm": "on",
        },
    )
    assert response.status_code == 302
    profile.refresh_from_db()
    assert profile.actual_continuation_date == date(2024, 1, 16)
    assert profile.actual_continuation_review_required is False
