from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import re
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import AssetQrIdentity, AttachmentLink
from apps.assets.services import submit_asset_for_finance
from apps.finance.forms import ConfirmFormalizationForm, DepreciationPolicyForm
from apps.finance.models import (
    AssetDepreciationProfile,
    AssetFinance,
    DepreciationBatch,
    DepreciationPolicy,
)
from apps.finance.services import (
    confirm_depreciation_batch,
    create_depreciation_policy,
    create_fixed_asset_category,
)
from apps.masterdata.models import IssuedCode, SequenceCounter
from tests.test_sprint3_support import (
    add_photo,
    complete_initialization,
    direct_draft,
    make_asset,
    make_category,
    make_company,
    make_department,
    make_employee,
    make_location_tree,
    make_user,
    PDF_BYTES,
)


pytestmark = pytest.mark.django_db


def _context():
    company = make_company()
    equipment = make_user("s4-equipment", "equipment")
    finance = make_user("s4-finance", "finance")
    management = make_user("s4-management", "management")
    admin = make_user("s4-admin", "system_admin")
    complete_initialization(company, admin)
    department = make_department(company)
    employee = make_employee(company, department)
    category = make_category(company)
    _site, _area, location = make_location_tree(company)
    asset = make_asset(
        actor=equipment,
        company=company,
        category=category,
        department=department,
        employee=employee,
        location=location,
    )
    add_photo(equipment, asset)
    asset = submit_asset_for_finance(actor=equipment, asset=asset)
    return {
        "company": company,
        "equipment": equipment,
        "finance": finance,
        "management": management,
        "admin": admin,
        "asset": asset,
    }


def _policy(company, actor, key="S4-HTTP"):
    return create_depreciation_policy(
        actor=actor,
        company=company,
        data={
            "policy_key": key,
            "name": "Sprint 4 HTTP 政策",
            "method": "straight_line",
            "posting_period": "monthly",
            "start_rule": "next_month",
            "stop_rule": "next_month",
            "default_useful_life_months": 60,
            "default_salvage_mode": "rate",
            "default_salvage_rate": Decimal("0.05000000"),
            "default_salvage_amount": None,
            "annual_posting_month": None,
            "work_unit": "",
            "effective_from": timezone.localdate(),
            "effective_to": None,
        },
    )


def _policy_form_data(**overrides):
    data = {
        "policy_key": "S4-FORM",
        "name": "Sprint 4 表单政策",
        "method": "straight_line",
        "posting_period": "monthly",
        "start_rule": "next_month",
        "stop_rule": "next_month",
        "default_useful_life_months": "60",
        "default_salvage_mode": "rate",
        "default_salvage_rate": "0.05",
        "default_salvage_amount": "",
        "annual_posting_month": "",
        "work_unit": "",
        "effective_from": "",
        "effective_to": "",
    }
    data.update(overrides)
    return data


def _finance_row(context, *, fixed=False):
    asset = context["asset"]
    values = {
        "company": context["company"],
        "asset": asset,
        "accounting_treatment": (
            "fixed_asset" if fixed else "controlled_non_fixed"
        ),
        "original_cost": Decimal("12345.67"),
        "recognition_threshold_snapshot": Decimal("5000.00"),
        "finance_confirmed_by": context["finance"],
        "finance_confirmed_at": timezone.now(),
    }
    if fixed:
        today = timezone.localdate()
        asset.commissioning_date = today
        asset.save(update_fields=["commissioning_date", "updated_at"])
        values.update(
            {
                "fixed_asset_category": create_fixed_asset_category(
                    actor=context["finance"],
                    company=context["company"],
                    data={
                        "code": "S4-HTTP-FA",
                        "name": "Sprint 4 HTTP 固定资产类别",
                        "useful_life_months_default": 60,
                    },
                ),
                "capitalization_date": today,
            }
        )
    else:
        values["accounting_treatment_reason"] = "高于提示阈值但明确认定为受控非固定资产"
    AssetFinance.objects.create(**values)
    return asset


@pytest.mark.parametrize(
    "role",
    ("system_admin", "equipment", "department_manager", "warehouse", "employee", "hr"),
)
def test_non_finance_roles_cannot_read_or_write_f1_pages(client, role):
    context = _context()
    user = make_user(f"s4-denied-{role}", role)
    client.force_login(user)
    urls = (
        reverse("finance:pending-list"),
        reverse("finance:finance-confirm", args=[context["asset"].pk]),
        reverse("finance:policy-list"),
        reverse("finance:fixed-category-list"),
        reverse("finance:settings"),
        reverse("finance:batch-list"),
    )

    for url in urls:
        assert client.get(url).status_code == 403
    response = client.post(
        reverse("finance:finance-confirm", args=[context["asset"].pk]),
        {"action": "save", "original_cost": "999999.99"},
    )
    assert response.status_code == 403
    assert not AssetFinance.objects.filter(asset=context["asset"]).exists()


def test_management_sees_pending_finance_summary_without_mutating_confirmation_button(client):
    context = _context()
    confirm_url = reverse("finance:finance-confirm", args=[context["asset"].pk])

    client.force_login(context["management"])
    readonly = client.get(reverse("assets:asset-detail", args=[context["asset"].pk]))
    assert readonly.status_code == 200
    assert "财务资料尚未确认".encode() in readonly.content
    assert confirm_url.encode() not in readonly.content
    assert client.get(confirm_url).status_code == 403

    client.force_login(context["finance"])
    writable = client.get(reverse("assets:asset-detail", args=[context["asset"].pk]))
    assert writable.status_code == 200
    assert confirm_url.encode() in writable.content


def test_finance_can_write_and_management_is_strictly_read_only(client):
    context = _context()
    pending_url = reverse("finance:pending-list")
    confirm_url = reverse("finance:finance-confirm", args=[context["asset"].pk])
    client.force_login(context["finance"])

    assert client.get(pending_url).status_code == 200
    assert client.get(confirm_url).status_code == 200

    client.force_login(context["management"])
    pending = client.get(pending_url)
    assert pending.status_code == 200
    assert confirm_url not in pending.content.decode()
    assert client.get(confirm_url).status_code == 403
    assert client.post(reverse("finance:settings"), {"fixed_asset_warning_amount": "1"}).status_code == 403


def test_policy_page_links_to_fixed_asset_accounting_categories(client):
    context = _context()
    client.force_login(context["finance"])

    response = client.get(reverse("finance:policy-list"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "固定资产会计类别" in html
    assert reverse("finance:fixed-category-list") in html


def test_finance_confirmation_explains_missing_category_master_and_commissioning_date(
    client,
):
    context = _context()
    context["asset"].commissioning_date = None
    context["asset"].save(update_fields=["commissioning_date", "updated_at"])
    client.force_login(context["finance"])

    response = client.get(
        reverse("finance:finance-confirm", args=[context["asset"].pk])
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "尚未配置固定资产会计类别" in html
    assert reverse("finance:fixed-category-create") in html
    assert "尚未填写达到可使用状态日期" in html
    assert reverse("assets:asset-withdraw", args=[context["asset"].pk]) in html


def test_finance_confirmation_uses_local_accounting_treatment_script(client):
    context = _context()
    client.force_login(context["finance"])

    response = client.get(
        reverse("finance:finance-confirm", args=[context["asset"].pk])
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="finance-confirm-form"' in html
    assert "/static/js/finance-confirm-form.js" in html
    assert "<script>" not in html


def test_management_reads_confirmed_f1_but_never_receives_qr_token(client):
    context = _context()
    asset = _finance_row(context)
    client.force_login(context["management"])

    response = client.get(reverse("finance:asset-finance-detail", args=[asset.pk]))

    assert response.status_code == 200
    html = response.content.decode()
    assert "12345.67" in html
    assert "价值调整" not in html
    assert "public_token" not in html


def test_a1_attachment_http_is_finance_write_management_read_only(
    client, tmp_path
):
    context = _context()
    asset = context["asset"]
    upload_url = reverse("assets:attachment-upload", args=[asset.pk])
    client.force_login(context["finance"])
    with override_settings(MEDIA_ROOT=tmp_path):
        response = client.post(
            upload_url,
            {
                "role": "invoice",
                "security_class": "A1",
                "file": SimpleUploadedFile(
                    "S4-A1-INVOICE.pdf",
                    PDF_BYTES,
                    content_type="application/pdf",
                ),
            },
        )
        assert response.status_code == 302
        link = AttachmentLink.objects.get(
            asset=asset, security_class=AttachmentLink.SecurityClass.A1
        )
        download_url = reverse(
            "assets:attachment-download", args=[asset.pk, link.pk]
        )
        void_url = reverse("assets:attachment-void", args=[asset.pk, link.pk])

        client.force_login(context["management"])
        download = client.get(download_url)
        assert download.status_code == 200
        assert b"".join(download.streaming_content) == PDF_BYTES
        assert client.get(upload_url).status_code == 403
        assert client.post(void_url, {"reason": "越权作废"}).status_code == 403

        for role in (
            context["admin"],
            context["equipment"],
            make_user("s4-a1-manager", "department_manager"),
            make_user("s4-a1-warehouse", "warehouse"),
        ):
            client.force_login(role)
            assert client.get(download_url).status_code in {403, 404}
            detail = client.get(reverse("assets:asset-detail", args=[asset.pk]))
            if detail.status_code == 200:
                assert "S4-A1-INVOICE" not in detail.content.decode()

        client.force_login(context["finance"])
        assert client.post(void_url, {"reason": "发票上传错误"}).status_code == 302
        link.refresh_from_db()
        assert link.status == AttachmentLink.Status.VOIDED


def test_foreign_and_forged_uuid_objects_are_not_disclosed_or_mutated(client):
    context = _context()
    foreign_company = make_company("S4X", active=False)
    foreign_department = make_department(foreign_company, "X1")
    foreign_employee = make_employee(foreign_company, foreign_department, "XE1")
    foreign_category = make_category(foreign_company, "XCAT")
    _site, _area, foreign_location = make_location_tree(foreign_company, "XLOC")
    foreign = direct_draft(
        company=foreign_company,
        category=foreign_category,
        department=foreign_department,
        responsible_employee=foreign_employee,
        location=foreign_location,
    )
    client.force_login(context["finance"])

    for identifier in (foreign.pk, uuid.uuid4()):
        url = reverse("finance:finance-confirm", args=[identifier])
        assert client.get(url).status_code == 404
        assert client.post(url, {"action": "save", "original_cost": "100.00"}).status_code == 404
    assert not AssetFinance.objects.filter(asset=foreign).exists()


def test_preview_is_non_consuming_and_never_creates_official_qr_or_code(client):
    context = _context()
    fixed_category = create_fixed_asset_category(
        actor=context["finance"],
        company=context["company"],
        data={
            "code": "MACHINE",
            "name": "机器设备",
            "useful_life_months_default": 60,
            "note": "预览测试",
        },
    )
    DepreciationPolicy.objects.create(
        company=context["company"],
        policy_key="S4-PREVIEW",
        version=1,
        name="预览默认政策",
        method="straight_line",
        posting_period="monthly",
        start_rule="next_month",
        stop_rule="next_month",
        default_useful_life_months=60,
        default_salvage_mode="rate",
        default_salvage_rate=Decimal("0.05000000"),
        status="active",
        is_default=True,
        effective_from=timezone.localdate(),
        created_by=context["finance"],
    )
    client.force_login(context["finance"])
    before = (
        SequenceCounter.objects.count(),
        IssuedCode.objects.count(),
        AssetQrIdentity.objects.count(),
    )

    response = client.post(
        reverse("finance:finance-preview", args=[context["asset"].pk]),
        {
            "accounting_treatment": "fixed_asset",
            "original_cost": "10000.00",
            "fixed_asset_category": fixed_category.pk,
            "capitalization_date": context["asset"].commissioning_date.isoformat(),
            "code_effective_date": timezone.localdate().isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.context["preview"] is not None
    assert response.context["preview"].lines
    assert (
        SequenceCounter.objects.count(),
        IssuedCode.objects.count(),
        AssetQrIdentity.objects.count(),
    ) == before == (0, 0, 0)
    context["asset"].refresh_from_db()
    assert context["asset"].asset_status == "pending_finance"
    assert context["asset"].asset_code is None


def test_finance_forms_and_html_never_accept_or_render_protected_identifiers(client):
    context = _context()
    form = ConfirmFormalizationForm(
        actor=context["finance"], company=context["company"], asset=context["asset"]
    )
    protected = {
        "asset_code", "current_issued_code", "sequence_counter", "public_token",
        "qr_identity", "impairment_balance_cache", "finance_confirmed_at",
        "finance_confirmed_by", "book_value", "actual_accumulated_depreciation",
    }

    assert protected.isdisjoint(form.fields)
    client.force_login(context["finance"])
    html = client.get(
        reverse("finance:finance-confirm", args=[context["asset"].pk])
    ).content.decode()
    for field in protected:
        assert f'name="{field}"' not in html
    assert "public_token" not in html


def test_sensitive_finance_post_requires_reason_and_confirmation(client):
    context = _context()
    policy = _policy(context["company"], context["finance"])
    client.force_login(context["finance"])

    confirm = client.post(
        reverse("finance:finance-confirm", args=[context["asset"].pk]),
        {
            "action": "confirm",
            "accounting_treatment": "controlled_non_fixed",
            "original_cost": "0.00",
            "code_effective_date": timezone.localdate().isoformat(),
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    policy_action = client.post(
        reverse("finance:policy-action", args=[policy.pk, "activate"]),
        {},
    )

    assert confirm.status_code == 200
    assert "财务正式化原因" in confirm.content.decode()
    assert context["asset"].current_issued_code_id is None
    assert policy_action.status_code == 400
    policy.refresh_from_db()
    assert policy.status == "draft"


def test_policy_and_category_pages_use_explicit_confirmation_pages(client):
    context = _context()
    policy = _policy(context["company"], context["finance"])
    client.force_login(context["finance"])

    policy_html = client.get(
        reverse("finance:policy-detail", args=[policy.pk])
    ).content.decode()

    assert 'name="reason"' in policy_html
    assert 'name="confirm"' in policy_html
    assert "二次确认" in policy_html


@pytest.mark.parametrize(
    ("salvage_rate", "expected_message"),
    (
        ("", "残值率模式必须填写默认残值率"),
        ("5", "默认残值率必须在 0 至 1 之间"),
    ),
)
def test_policy_form_shows_field_error_instead_of_database_constraint(
    client, salvage_rate, expected_message
):
    context = _context()
    client.force_login(context["finance"])

    response = client.post(
        reverse("finance:policy-create"),
        _policy_form_data(default_salvage_rate=salvage_rate),
    )

    assert response.status_code == 200
    assert expected_message in response.content.decode()
    assert "ck_depr_policy_salvage_fields" not in response.content.decode()
    assert not DepreciationPolicy.objects.filter(
        company=context["company"], policy_key="S4-FORM"
    ).exists()


def test_policy_form_explains_yearly_period_fields_without_constraint_name(client):
    context = _context()
    client.force_login(context["finance"])

    response = client.post(
        reverse("finance:policy-create"),
        _policy_form_data(posting_period="yearly", annual_posting_month=""),
    )

    assert response.status_code == 200
    assert "年度计提必须填写 1 至 12 的计提月" in response.content.decode()
    assert "ck_depr_policy_period_fields" not in response.content.decode()


def test_policy_form_requires_start_date_when_end_date_is_filled(client):
    context = _context()
    client.force_login(context["finance"])

    response = client.post(
        reverse("finance:policy-create"),
        _policy_form_data(effective_to=timezone.localdate().isoformat()),
    )

    assert response.status_code == 200
    assert "填写生效结束日时必须同时填写开始日" in response.content.decode()
    assert "ck_depr_policy_effective_dates" not in response.content.decode()


@pytest.mark.parametrize(
    ("salvage_mode", "salvage_rate", "salvage_amount", "saved_rate", "saved_amount"),
    (
        ("rate", "0.05", "500.00", Decimal("0.05"), None),
        ("amount", "0.05", "500.00", None, Decimal("500.00")),
    ),
)
def test_policy_form_discards_fields_that_do_not_match_selected_modes(
    client, salvage_mode, salvage_rate, salvage_amount, saved_rate, saved_amount
):
    context = _context()
    client.force_login(context["finance"])

    response = client.post(
        reverse("finance:policy-create"),
        _policy_form_data(
            default_salvage_mode=salvage_mode,
            default_salvage_rate=salvage_rate,
            default_salvage_amount=salvage_amount,
            annual_posting_month="12",
        ),
    )

    assert response.status_code == 302
    policy = DepreciationPolicy.objects.get(
        company=context["company"], policy_key="S4-FORM"
    )
    assert policy.default_salvage_rate == saved_rate
    assert policy.default_salvage_amount == saved_amount
    assert policy.annual_posting_month is None


def test_policy_form_help_text_explains_percentage_input():
    context = _context()
    form = DepreciationPolicyForm(actor=context["finance"])

    assert "5% 填写 0.05" in form.fields["default_salvage_rate"].help_text


def test_batch_reverse_get_key_is_reused_by_post_retry(client):
    context = _context()
    month_start = timezone.localdate().replace(day=1)
    next_month_start = (month_start + timedelta(days=32)).replace(day=1)
    batch = DepreciationBatch.objects.create(
        company=context["company"],
        period_start=month_start,
        period_end=next_month_start,
        generation_no=1,
        batch_type="regular",
        status="draft",
        idempotency_key="source-batch-key",
        request_hash="source-batch-hash",
        generated_by=context["finance"],
        generated_at=timezone.now(),
    )
    batch = confirm_depreciation_batch(
        actor=context["finance"], batch=batch, reason="确认测试源批次"
    )
    client.force_login(context["finance"])
    url = reverse("finance:batch-reverse", args=[batch.pk])
    get_response = client.get(url)
    key = get_response.context["form"].initial["idempotency_key"]

    locations = []
    for _ in range(2):
        response = client.post(
            url,
            {
                "reason": "冲销错误计提",
                "confirm": "on",
                "idempotency_key": key,
            },
        )
        assert response.status_code == 302
        locations.append(response["Location"])

    reversal = DepreciationBatch.objects.get(reverses_batch=batch)
    assert reversal.status == "confirmed"
    assert reversal.idempotency_key == key
    assert locations == [
        reverse("finance:batch-detail", args=[reversal.pk]),
        reverse("finance:batch-detail", args=[reversal.pk]),
    ]


def test_profile_entry_view_resolves_the_unique_profile_effective_today(client):
    context = _context()
    asset = _finance_row(context, fixed=True)
    policy = _policy(context["company"], context["finance"])
    today = timezone.localdate()
    policy.status = "active"
    policy.effective_from = today - timedelta(days=60)
    policy.save(update_fields=["status", "effective_from", "updated_at"])
    old = AssetDepreciationProfile.objects.create(
        company=context["company"],
        asset=asset,
        depreciation_policy=policy,
        version=1,
        method="straight_line",
        posting_period="monthly",
        start_rule="specified_month",
        stop_rule="next_month",
        start_date=today - timedelta(days=60),
        useful_life_months=60,
        salvage_mode="rate",
        salvage_rate=Decimal("0.05000000"),
        opening_book_value=Decimal("12345.67"),
        opening_actual_accumulated_depreciation=Decimal("0.00"),
        effective_from=today - timedelta(days=60),
        effective_to=today + timedelta(days=10),
        status="completed",
        created_by=context["finance"],
    )
    AssetDepreciationProfile.objects.create(
        company=context["company"],
        asset=asset,
        depreciation_policy=policy,
        version=2,
        method="straight_line",
        posting_period="monthly",
        start_rule="specified_month",
        stop_rule="next_month",
        start_date=today + timedelta(days=11),
        useful_life_months=48,
        salvage_mode="rate",
        salvage_rate=Decimal("0.05000000"),
        opening_book_value=Decimal("12345.67"),
        opening_actual_accumulated_depreciation=Decimal("0.00"),
        effective_from=today + timedelta(days=11),
        status="active",
        change_reason="未来月份估计变更",
        created_by=context["finance"],
    )
    client.force_login(context["finance"])

    response = client.get(reverse("finance:work-usage", args=[asset.pk]))

    assert response.status_code == 200
    assert response.context["form"].initial["work_unit"] == old.work_unit


def test_setup_step_seven_is_finance_write_and_step_nine_is_admin_write(client):
    context = _context()
    step7 = reverse("masterdata:setup-step", args=[7])
    step9 = reverse("masterdata:setup-step", args=[9])

    client.force_login(context["finance"])
    assert client.get(step7).status_code == 200
    assert client.post(step7).status_code == 302
    assert client.post(step9).status_code == 403

    client.force_login(context["admin"])
    assert client.get(step7).status_code == 200
    assert client.post(step7).status_code == 403
    assert client.get(step9).status_code == 200
    assert client.post(step9).status_code == 302


def test_finance_pages_have_no_external_runtime_resource_requests(client):
    context = _context()
    client.force_login(context["finance"])
    urls = (
        reverse("finance:pending-list"),
        reverse("finance:finance-confirm", args=[context["asset"].pk]),
        reverse("finance:policy-list"),
        reverse("finance:fixed-category-list"),
        reverse("finance:batch-list"),
    )

    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        html = response.content.decode()
        assert not re.search(r'''(?:src|href)=["'](?:https?:)?//''', html, re.I)
        assert "cdn" not in html.casefold()
