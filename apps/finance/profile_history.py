"""Read-only display of stored depreciation parameters and adjacent versions."""

from datetime import date
from decimal import Decimal
from urllib.parse import urlencode, urlsplit

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.masterdata.permissions import current_company
from .models import AssetDepreciationProfile
from .permissions import require_view_finance, scoped_finance_assets


PARAMETERS = (
    ("depreciation_policy", "来源政策版本"), ("method", "折旧方法"),
    ("posting_period", "计提周期"), ("start_rule", "起算规则"), ("stop_rule", "停止规则"),
    ("start_date", "折旧起算日"), ("actual_continuation_date", "实际接续日"),
    ("actual_continuation_review_required", "实际接续日待复核"),
    ("useful_life_months", "使用寿命（月）"), ("salvage_mode", "残值方式"),
    ("salvage_rate", "残值率（小数）"), ("salvage_amount", "残值金额"),
    ("annual_posting_month", "年度计提月"), ("expected_total_units", "预计总工作量"),
    ("work_unit", "工作量单位"), ("opening_book_value", "期初账面净值"),
    ("opening_actual_accumulated_depreciation", "期初实际累计折旧"),
)


def _stored_value(profile, key):
    if key == "depreciation_policy":
        return profile.depreciation_policy_id
    return getattr(profile, key)


def _display(profile, key):
    value = _stored_value(profile, key)
    if key == "depreciation_policy":
        policy = profile.depreciation_policy
        if policy.company_id != profile.company_id:
            return "当前公司内不可查的政策"
        return f"{policy.policy_key} · {policy.name} v{policy.version}"
    if value is None or value == "":
        return "未设置／不适用"
    display = getattr(profile, f"get_{key}_display", None)
    if display is not None:
        return display()
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def stored_parameter_rows(profile, previous=None):
    return [{
        "key": key, "label": label, "value": _display(profile, key),
        "previous": _display(previous, key) if previous is not None else None,
        "changed": previous is not None and _stored_value(profile, key) != _stored_value(previous, key),
    } for key, label in PARAMETERS]


def profile_detail_url(profile, origin):
    return reverse("finance:profile-detail", args=[profile.pk]) + "?" + urlencode({"return_to": origin})


def profile_history_links(request, profiles):
    for profile in profiles:
        profile.ui_detail_url = profile_detail_url(profile, request.get_full_path())


def _return_path(request, asset_id):
    fallback = reverse("finance:asset-finance-detail", args=[asset_id])
    value = request.GET.get("return_to", "")
    if value and len(value) <= 6000 and "\\" not in value and not any(ord(char) < 32 for char in value):
        try:
            parts = urlsplit(value)
            if (not parts.scheme and not parts.netloc and not parts.fragment
                    and not value.startswith("//") and parts.path == fallback):
                return value
        except ValueError:
            pass
    return fallback


@login_required
@never_cache
@require_GET
def depreciation_profile_detail(request, profile_pk):
    require_view_finance(request.user)
    company = current_company()
    profiles = AssetDepreciationProfile.objects.filter(
        company=company, asset__company=company, asset__in=scoped_finance_assets(request.user, company),
        asset__finance__finance_confirmed_at__isnull=False,
        asset__finance__accounting_treatment__isnull=False, asset__finance__original_cost__isnull=False,
    ).select_related("asset", "depreciation_policy", "created_by")
    profile = get_object_or_404(profiles, pk=profile_pk)
    same_asset = profiles.filter(asset=profile.asset)
    previous = same_asset.filter(version__lt=profile.version).order_by("-version").first()
    following = same_asset.filter(version__gt=profile.version).order_by("version").first()
    rows = stored_parameter_rows(profile, previous)
    origin = _return_path(request, profile.asset_id)
    return render(request, "finance/profile_detail.html", {
        "profile": profile, "asset": profile.asset, "previous_profile": previous, "next_profile": following,
        "parameter_rows": rows, "changed_rows": [row for row in rows if row["changed"]],
        "unchanged_rows": [row for row in rows if not row["changed"]],
        "profile_return_url": origin + "#depreciation-profiles",
        "previous_profile_url": profile_detail_url(previous, origin) if previous else "",
        "next_profile_url": profile_detail_url(following, origin) if following else "",
    })
