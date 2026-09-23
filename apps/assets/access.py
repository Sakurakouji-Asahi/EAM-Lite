"""Asset page access: initialization gate, role scope and object lookup."""
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404

from apps.assets.models import Asset
from apps.assets.permissions import scoped_assets
from apps.masterdata.models import InitializationSetting
from apps.masterdata.permissions import current_company


def asset_company_for_request():
    company = current_company()
    if company is None:
        raise Http404("尚未配置启用公司。")
    if not InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists():
        raise PermissionDenied("系统初始化尚未完成，资产建账入口暂不可用。")
    return company


def asset_queryset_for_request(user, company):
    return scoped_assets(
        user,
        company,
        Asset.objects.select_related(
            "category",
            "department",
            "responsible_employee",
            "location",
            "requested_coding_scheme",
            "created_by",
            "submitted_by",
            "finance",
            "registration",
            "current_issued_code__identity",
        ),
    )


def asset_or_404(user, company, pk):
    return get_object_or_404(asset_queryset_for_request(user, company), pk=pk)
