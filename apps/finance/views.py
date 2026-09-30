"""Server-rendered Sprint 4 finance and depreciation views."""

from __future__ import annotations

import uuid
from calendar import monthrange
from urllib.parse import urlencode
from decimal import Decimal
from django import forms

from django.contrib import messages
from apps.finance.history_workspace import entry_history_context
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset
from apps.finance.workspaces import batch_review_context, ManualAmountForm, TheoreticalBusinessForm
from apps.finance.readiness import pending_finance_assets, missing_finance_base_fields, filter_pending_finance_assets
from apps.finance.confirmation_initial import finance_confirmation_initial as _finance_initial
from apps.finance.forms import (
    PendingFinanceFilterForm,
    AssetCategoryPolicyForm,
    ConfirmFormalizationForm,
    DepreciationBatchGenerateForm,
    DepreciationBatchFilterForm,
    DepreciationPolicyForm,
    DangerousActionForm,
    FinanceDraftForm,
    FixedAssetCategoryForm,
    FixedAssetWarningAmountForm,
    IdempotentReasonForm,
    PolicyActionForm,
    ProfileContinuationReviewForm,
    ProfileEventForm,
    ProfileVersionForm,
    ReasonForm,
    ValueAdjustmentForm,
    TheoreticalRunForm,
    WorkUsageForm,
)
from apps.finance.models import (
    AssetDepreciationProfile,
    AssetFinance,
    DepreciationBatch,
    DepreciationEntry,
    DepreciationPolicy,
)
from apps.finance.permissions import (
    can_manage_finance,
    require_manage_finance,
    require_view_finance,
    scoped_finance_assets,
)
from apps.finance.services import (
    activate_depreciation_policy,
    clone_depreciation_policy,
    confirm_asset_finance,
    confirm_depreciation_batch,
    create_depreciation_policy,
    create_fixed_asset_category,
    create_profile_event,
    create_value_adjustment,
    deactivate_fixed_asset_category,
    generate_depreciation_batch,
    get_asset_depreciation_status,
    ensure_asset_is_depreciable,
    preview_asset_depreciation,
    record_work_usage,
    review_profile_actual_continuation_date,
    retire_depreciation_policy,
    reverse_depreciation_batch,
    reverse_value_adjustment,
    clone_asset_depreciation_profile,
    run_theoretical_depreciation,
    save_asset_finance_draft,
    set_default_depreciation_policy,
    set_category_default_depreciation_policy,
    update_draft_depreciation_policy,
    update_fixed_asset_category,
)
from apps.masterdata.models import FixedAssetCategory
from apps.masterdata.permissions import current_company
from apps.masterdata.services import get_system_setting, set_system_setting


def _company():
    company = current_company()
    if company is None:
        raise Http404("尚未配置启用公司。")
    return company


def _require_depreciable_asset(asset):
    try:
        return ensure_asset_is_depreciable(asset)
    except ValidationError as exc:
        raise PermissionDenied("; ".join(exc.messages)) from exc


def _service_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for error in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, error)


def _pending_asset(company, pk):
    return get_object_or_404(
        pending_finance_assets(Asset.objects.filter(company=company)).select_related(
            "company", "category", "department", "responsible_employee", "location"
        ),
        pk=pk,
        company=company,
    )


@login_required
def pending_finance_list(request):
    require_view_finance(request.user)
    company = _company()
    form = PendingFinanceFilterForm(request.GET, company=company)
    assets = scoped_finance_assets(request.user, company).select_related(
        "category", "department", "responsible_employee", "finance", "registration"
    ).order_by("submitted_at", "created_at", "pk")
    query = {}
    if form.is_valid():
        data = form.cleaned_data
        assets = filter_pending_finance_assets(assets,data)
        query = {key:getattr(value,"pk",value) for key,value in data.items() if value}
    else:
        assets = assets.none()
    page = Paginator(assets, form.cleaned_data.get("page_size") or 25).get_page(request.GET.get("page"))
    for asset in page.object_list:
        asset.finance_missing_fields = missing_finance_base_fields(asset)
    return render(
        request,
        "finance/pending_list.html",
        {
            "assets": page.object_list,
            "page_obj": page,
            "can_manage": can_manage_finance(request.user),
            "filter_form": form,
            "pagination_query": urlencode(query),
            "selection_key": f"eam-bulk-finance:{company.pk}:{request.user.pk}:{query.get('import_batch','')}",
            "bulk_filters": query,
        },
    )


def _finance_form(request, *, asset, confirm=False):
    form_class = ConfirmFormalizationForm if confirm else FinanceDraftForm
    return form_class(
        request.POST or None,
        actor=request.user,
        company=asset.company,
        asset=asset,
        initial=_finance_initial(asset),
    )


def _finance_confirm_context(*, asset, form, preview=None, preview_only=False):
    sections = [
        ("会计认定与金额", "", ("accounting_treatment", "original_cost", "fixed_asset_category", "capitalization_date", "commissioning_date", "accounting_treatment_reason")),
        ("折旧规则", "留空项目使用适用政策的默认值。", ("depreciation_policy", "method", "useful_life_months", "posting_period", "salvage_mode", "salvage_rate", "salvage_amount", "start_rule", "specified_start_date", "stop_rule", "annual_posting_month", "expected_total_units", "work_unit")),
        ("期初余额与接续", "旧资产按期初余额接续；已提足资产保留余额，不重复计提。", ("opening_actual_accumulated_depreciation", "opening_impairment", "actual_continuation_date")),
        ("备注与说明", "", ("finance_remark", "action_reason", "code_effective_date", "code_effective_reason")),
    ]
    return {
        "asset": asset,
        "form": form,
        "preview": preview,
        "preview_only": preview_only,
        "field_sections": [{"title": title, "help": help_text, "fields": [form[name] for name in names if not form[name].is_hidden]}
                           for title, help_text, names in sections],
        "has_fixed_asset_categories": form.fields[
            "fixed_asset_category"
        ].queryset.exists(),
    }


@login_required
def finance_preview(request, pk):
    require_manage_finance(request.user)
    company = _company()
    asset = _pending_asset(company, pk)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    form = _finance_form(request, asset=asset)
    result = None
    if form.is_valid():
        if form.cleaned_data["accounting_treatment"] == "controlled_non_fixed":
            messages.info(request, "受控非固定资产不建立折旧 折旧参数 或折旧计划。")
        else:
            try:
                calculation = preview_asset_depreciation(
                    actor=request.user,
                    asset=asset,
                    finance_data=form.finance_data(),
                    profile_data=form.profile_data(),
                    commissioning_date=form.cleaned_data.get("commissioning_date"),
                )
                result = calculation["summary"] if isinstance(calculation, dict) else calculation[1]
            except (ValidationError, ValueError) as exc:
                _service_error(form, exc)
    return render(
        request,
        "finance/finance_confirm.html",
        _finance_confirm_context(
            asset=asset, form=form, preview=result, preview_only=True
        ),
    )


@login_required
def finance_confirm(request, pk):
    require_manage_finance(request.user)
    company = _company()
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    completed = scoped_finance_assets(request.user, company).filter(
        pk=pk, finance__finance_confirmed_at__isnull=False,
    ).first()
    if completed is not None:
        if request.method == "POST":
            messages.info(request, "该资产已完成财务确认，本次提交未修改数据；如需更正，请在财务资料页办理调整。")
        else:
            messages.info(request, "该资产已完成财务确认，已打开财务资料。")
        return redirect("finance:asset-finance-detail", pk=completed.pk)
    asset = _pending_asset(company, pk)
    action = request.POST.get("action") if request.method == "POST" else None
    form = _finance_form(request, asset=asset, confirm=action == "confirm")
    if request.method == "POST" and form.is_valid():
        try:
            if action == "save":
                save_asset_finance_draft(
                    actor=request.user,
                    asset=asset,
                    data=form.finance_data(),
                    profile_data=form.profile_data(),
                    commissioning_date=form.cleaned_data.get("commissioning_date"),
                    request=request,
                )
                messages.success(request, "财务资料已保存为草稿，填写的折旧参数一并保留，可稍后确认。")
            elif action == "confirm":
                asset = confirm_asset_finance(
                    actor=request.user,
                    asset=asset,
                    finance_data=form.finance_data(),
                    profile_data=form.profile_data(),
                    commissioning_date=form.cleaned_data.get("commissioning_date"),
                    code_effective_date=form.cleaned_data["code_effective_date"],
                    code_effective_reason=form.cleaned_data["code_effective_reason"],
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    reason=form.cleaned_data["action_reason"],
                    request=request,
                )
                messages.success(
                    request,
                    f"资产 {asset.asset_code} 的财务与折旧设置已确认，实物状态保持不变。",
                )
                return redirect("finance:asset-finance-detail", pk=asset.pk)
            else:
                raise ValidationError("未知的财务确认动作。")
        except (ValidationError, ValueError) as exc:
            _service_error(form, exc)
        else:
            return redirect("finance:finance-confirm", pk=asset.pk)
    return render(
        request,
        "finance/finance_confirm.html",
        _finance_confirm_context(asset=asset, form=form),
    )


@login_required
def asset_finance_detail(request, pk):
    require_view_finance(request.user)
    company = _company()
    asset = get_object_or_404(
        scoped_finance_assets(request.user, company).select_related("category"), pk=pk
    )
    finance = get_object_or_404(
        AssetFinance.objects.select_related("fixed_asset_category"),
        asset=asset,
        finance_confirmed_at__isnull=False,
        accounting_treatment__isnull=False,
        original_cost__isnull=False,
    )
    profiles = asset.depreciation_profiles.select_related(
        "depreciation_policy"
    ).order_by("version")
    entries = asset.depreciation_entries.order_by("period_start", "created_at")
    actual_ad = entries.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    book_value = finance.original_cost - finance.impairment_balance_cache - actual_ad
    today = timezone.localdate()
    current_profiles = [profile for profile in profiles if
                        profile.status in {"active", "suspended", "completed", "stopped"}
                        and profile.effective_from <= today
                        and (profile.effective_to is None or profile.effective_to >= today)]
    return render(
        request,
        "finance/asset_finance_detail.html",
        {
            "asset": asset,
            "finance": finance,
            "profiles": profiles,
            **entry_history_context(request, entries),
            "actual_ad": actual_ad,
            "book_value": book_value,
            "can_record_work_usage": can_manage_finance(request.user)
            and len(current_profiles) == 1 and current_profiles[0].method == "units_of_production",
            "depreciation_state": get_asset_depreciation_status(actor=request.user, asset=asset),
            "can_manage": can_manage_finance(request.user),
        },
    )


@login_required
def policy_list(request):
    require_view_finance(request.user)
    company = _company()
    return render(
        request,
        "finance/policy_list.html",
        {
            "policies": DepreciationPolicy.objects.filter(company=company).order_by(
                "policy_key", "-version"
            ),
            "can_manage": can_manage_finance(request.user),
        },
    )


@login_required
def policy_form(request, pk=None):
    require_manage_finance(request.user)
    company = _company()
    policy = (
        get_object_or_404(DepreciationPolicy, pk=pk, company=company)
        if pk is not None
        else None
    )
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = DepreciationPolicyForm(
        request.POST or None, actor=request.user, instance=policy
    )
    if request.method == "POST" and form.is_valid():
        try:
            saved = (
                update_draft_depreciation_policy(
                    actor=request.user,
                    policy=policy,
                    data=form.cleaned_data,
                    request=request,
                )
                if policy is not None
                else create_depreciation_policy(
                    actor=request.user,
                    company=company,
                    data=form.cleaned_data,
                    request=request,
                )
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "折旧政策草稿已保存。")
            return redirect("finance:policy-detail", pk=saved.pk)
    return render(request, "finance/form.html", {"form": form, "title": "折旧政策"})


@login_required
def policy_detail(request, pk):
    require_view_finance(request.user)
    policy = get_object_or_404(DepreciationPolicy, pk=pk, company=_company())
    return render(
        request,
        "finance/policy_detail.html",
        {
            "policy": policy,
            "can_manage": can_manage_finance(request.user),
            "action_form": (
                PolicyActionForm(actor=request.user)
                if can_manage_finance(request.user)
                else None
            ),
        },
    )


@login_required
def policy_action(request, pk, action):
    require_manage_finance(request.user)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    policy = get_object_or_404(DepreciationPolicy, pk=pk, company=_company())
    form = PolicyActionForm(request.POST, actor=request.user)
    if not form.is_valid():
        return render(
            request,
            "finance/policy_detail.html",
            {"policy": policy, "can_manage": True, "action_form": form},
            status=400,
        )
    reason = form.cleaned_data["reason"]
    try:
        if action == "activate":
            policy = activate_depreciation_policy(
                actor=request.user,
                policy=policy,
                make_default=form.cleaned_data["make_default"],
                reason=reason,
                request=request,
            )
        elif action == "default":
            policy = set_default_depreciation_policy(
                actor=request.user, policy=policy, reason=reason, request=request
            )
        elif action == "clone":
            policy = clone_depreciation_policy(
                actor=request.user, policy=policy, reason=reason, request=request
            )
            messages.success(request, "已克隆为新草稿版本。")
            return redirect("finance:policy-edit", pk=policy.pk)
        elif action == "retire":
            policy = retire_depreciation_policy(
                actor=request.user, policy=policy, reason=reason, request=request
            )
        else:
            raise Http404("未知政策动作")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "折旧政策状态已更新。")
    return redirect("finance:policy-detail", pk=policy.pk)


@login_required
def fixed_category_list(request):
    require_view_finance(request.user)
    return render(
        request,
        "finance/fixed_category_list.html",
        {
            "categories": FixedAssetCategory.objects.filter(company=_company()).order_by(
                "normalized_code"
            ),
            "can_manage": can_manage_finance(request.user),
        },
    )


@login_required
def category_policy_form(request):
    require_manage_finance(request.user)
    company = _company()
    form = AssetCategoryPolicyForm(
        request.POST or None, actor=request.user, company=company
    )
    if request.method == "POST" and form.is_valid():
        try:
            set_category_default_depreciation_policy(
                actor=request.user,
                category=form.cleaned_data["category"],
                policy=form.cleaned_data["policy"],
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "实物分类默认折旧政策已更新。")
            return redirect("finance:policy-list")
    return render(
        request,
        "finance/form.html",
        {"form": form, "title": "配置实物分类默认折旧政策"},
    )


@login_required
def fixed_category_form(request, pk=None):
    require_manage_finance(request.user)
    company = _company()
    category = (
        get_object_or_404(FixedAssetCategory, pk=pk, company=company)
        if pk is not None
        else None
    )
    form = FixedAssetCategoryForm(
        request.POST or None, actor=request.user, instance=category
    )
    if request.method == "POST" and form.is_valid():
        try:
            saved = (
                update_fixed_asset_category(
                    actor=request.user,
                    category=category,
                    data=form.cleaned_data,
                    request=request,
                )
                if category
                else create_fixed_asset_category(
                    actor=request.user,
                    company=company,
                    data=form.cleaned_data,
                    request=request,
                )
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "固定资产会计类别已保存。")
            return redirect("finance:fixed-category-list")
    return render(
        request, "finance/form.html", {"form": form, "title": "固定资产会计类别"}
    )


@login_required
def fixed_category_deactivate(request, pk):
    require_manage_finance(request.user)
    category = get_object_or_404(FixedAssetCategory, pk=pk, company=_company())
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = DangerousActionForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        deactivate_fixed_asset_category(
            actor=request.user,
            category=category,
            reason=form.cleaned_data["reason"],
            request=request,
        )
        messages.success(request, "固定资产会计类别已停用，历史引用保留。")
        return redirect("finance:fixed-category-list")
    return render(
        request,
        "finance/form.html",
        {"form": form, "title": f"停用会计类别：{category.name}"},
    )


@login_required
def finance_settings(request):
    require_view_finance(request.user)
    company = _company()
    initial = {
        "fixed_asset_warning_amount": get_system_setting(
            company=company, key="fixed_asset_warning_amount"
        )
    }
    form = None
    if can_manage_finance(request.user):
        form = FixedAssetWarningAmountForm(
            request.POST or None, actor=request.user, initial=initial
        )
        if request.method == "POST" and form.is_valid():
            try:
                set_system_setting(
                    actor=request.user,
                    company=company,
                    key="fixed_asset_warning_amount",
                    value=form.cleaned_data["fixed_asset_warning_amount"],
                    request=request,
                )
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                messages.success(request, "财务提示参数已保存。")
                return redirect("finance:settings")
    elif request.method == "POST":
        raise PermissionDenied("management 只有财务只读权限。")
    return render(
        request,
        "finance/settings.html",
        {"form": form, "values": initial, "can_manage": can_manage_finance(request.user)},
    )


@login_required
def batch_list(request):
    require_view_finance(request.user)
    queryset = DepreciationBatch.objects.filter(company=_company())
    form = DepreciationBatchFilterForm(request.GET)
    valid = form.is_valid()
    if valid:
        period = form.cleaned_data["period"]
        if period:
            month_end = period.replace(day=monthrange(period.year, period.month)[1])
            queryset = queryset.filter(period_start__lte=month_end, period_end__gt=period)
        if form.cleaned_data["status"]:
            queryset = queryset.filter(status=form.cleaned_data["status"])
    else:
        queryset = queryset.none()
    queryset = queryset.annotate(item_count=Count("items")).order_by("-period_start", "-generation_no", "pk")
    page_obj = Paginator(queryset, form.cleaned_data.get("page_size") or 25).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "finance/batch_list.html",
        {
            "batches": page_obj,
            "page_obj": page_obj,
            "pagination_query": query.urlencode(),
            "filter_form": form,
            "can_manage": can_manage_finance(request.user),
        },
        status=200 if valid else 400,
    )


@login_required
def batch_generate(request):
    require_manage_finance(request.user)
    company = _company()
    form = DepreciationBatchGenerateForm(request.POST or None, actor=request.user)
    form.fields["manual_inputs_json"].widget = forms.HiddenInput()
    valid = form.is_valid() if request.method == "POST" else False
    period = form.service_period_values() if valid else None
    manual_profiles = AssetDepreciationProfile.objects.filter(company=company, method="manual")
    has_manual_profiles = manual_profiles.exists()
    if period:
        manual_profiles = manual_profiles.filter(effective_from__lt=period["period_end"]).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=period["period_start"]))
    manual_assets = scoped_finance_assets(request.user, company).filter(pk__in=manual_profiles.values("asset_id"))
    manual_rows = [ManualAmountForm(request.POST if request.method == "POST" else None,
                   prefix=f"manual-{asset.pk}", asset=asset) for asset in manual_assets.distinct().order_by("asset_code")]
    rows_valid = all([row.is_valid() for row in manual_rows]) if request.method == "POST" else False
    if request.method == "POST" and valid and rows_valid and request.POST.get("action") != "refresh_manual":
        manual_inputs = form.cleaned_data["manual_inputs_json"]
        entered = {str(row.asset.pk): {"amount": str(row.cleaned_data["amount"]), "reason": row.cleaned_data["reason"]}
                   for row in manual_rows if row.cleaned_data.get("amount") is not None}
        if manual_inputs and entered:
            form.add_error(None, "本次存在两组手工金额，请仅使用页面明细填写。")
        else:
            try:
                batch = generate_depreciation_batch(actor=request.user, company=company, **period,
                    idempotency_key=form.cleaned_data["idempotency_key"], manual_inputs=entered or manual_inputs, request=request)
            except ValidationError as exc:
                _service_error(form, exc)
            else:
                return redirect("finance:batch-detail", pk=batch.pk)
    return render(request, "finance/batch_generate.html", {"form": form, "manual_rows": manual_rows, "has_manual_profiles":has_manual_profiles})


@login_required
def batch_detail(request, pk):
    require_view_finance(request.user)
    batch = get_object_or_404(DepreciationBatch, pk=pk, company=_company())
    context = batch_review_context(request, batch)
    return render(request, "finance/batch_detail.html", context, status=200 if context["filters_valid"] else 400)


@login_required
def batch_confirm(request, pk):
    require_manage_finance(request.user)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    batch = get_object_or_404(DepreciationBatch, pk=pk, company=_company())
    form = DangerousActionForm(request.POST, actor=request.user)
    if not form.is_valid():
        return render(request, "finance/batch_detail.html", batch_review_context(request, batch, confirm_form=form), status=400)
    try:
        confirm_depreciation_batch(actor=request.user, batch=batch, reason=form.cleaned_data["reason"], request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "折旧批次已确认，实际分录已追加。")
    return redirect("finance:batch-detail", pk=batch.pk)


@login_required
def batch_reverse(request, pk):
    require_manage_finance(request.user)
    batch = get_object_or_404(DepreciationBatch, pk=pk, company=_company())
    form = IdempotentReasonForm(
        request.POST or None,
        actor=request.user,
        initial={"idempotency_key": uuid.uuid4().hex},
    )
    if request.method == "POST" and form.is_valid():
        try:
            reversal = reverse_depreciation_batch(
                actor=request.user,
                batch=batch,
                reason=form.cleaned_data["reason"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "原批次已通过反向分录冲销，原历史保持不变。")
            return redirect("finance:batch-detail", pk=reversal.pk)
    return render(request, "finance/form.html", {"form": form, "title": "冲销折旧批次"})


def _profile_for_asset(company, pk):
    asset = get_object_or_404(
        Asset.objects.select_related("finance"), pk=pk, company=company
    )
    _require_depreciable_asset(asset)
    target_date = timezone.localdate()
    profiles = list(
        AssetDepreciationProfile.objects.filter(
            asset=asset,
            status__in=("active", "suspended", "completed", "stopped"),
            effective_from__lte=target_date,
        )
        .filter(
            Q(effective_to__isnull=True)
            | Q(effective_to__gte=target_date)
        )
        .order_by("-effective_from", "-version")[:2]
    )
    if not profiles:
        raise Http404("当前业务日没有生效的折旧 折旧参数。")
    if len(profiles) != 1:
        raise ValidationError("当前业务日存在多个生效 折旧参数，请停止并复核数据。")
    return profiles[0]


@login_required
def profile_continuation_review(request, profile_pk):
    require_manage_finance(request.user)
    company = _company()
    profile = get_object_or_404(
        AssetDepreciationProfile.objects.select_related("asset", "asset__finance"),
        pk=profile_pk,
        company=company,
        asset__in=scoped_finance_assets(request.user, company),
    )
    _require_depreciable_asset(profile.asset)
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form = ProfileContinuationReviewForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data.copy()
        values.pop("confirm", None)
        try:
            review_profile_actual_continuation_date(
                actor=request.user,
                profile=profile,
                request=request,
                **values,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "实际接续日已完成一次性财务复核。")
            return redirect("finance:asset-finance-detail", pk=profile.asset_id)
    return render(
        request,
        "finance/form.html",
        {"form": form, "title": f"复核 折旧参数 v{profile.version} 实际接续日"},
    )


@login_required
def work_usage(request, pk):
    require_manage_finance(request.user)
    profile = _profile_for_asset(_company(), pk)
    if profile.method != "units_of_production":
        messages.info(request, "当前折旧方法无需录入工作量。")
        return redirect("finance:asset-finance-detail", pk=pk)
    form = WorkUsageForm(request.POST or None, actor=request.user, initial={"work_unit": profile.work_unit})
    if request.method == "POST" and form.is_valid():
        values = {**form.cleaned_data, **form.service_period_values()}
        try:
            record_work_usage(actor=request.user, profile=profile, request=request, **values)
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "当期工作量已保存。")
            return redirect("finance:asset-finance-detail", pk=pk)
    return render(request, "finance/form.html", {"form": form, "title": "录入当期工作量"})


@login_required
def profile_event(request, pk):
    require_manage_finance(request.user)
    profile = _profile_for_asset(_company(), pk)
    form = ProfileEventForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data.copy()
        values.pop("confirm", None)
        try:
            create_profile_event(actor=request.user, profile=profile, request=request, **values)
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "折旧状态事件已追加，既有历史未改写。")
            return redirect("finance:asset-finance-detail", pk=pk)
    return render(request, "finance/form.html", {"form": form, "title": "折旧暂停、恢复或停止"})


@login_required
def profile_version(request, pk):
    require_manage_finance(request.user)
    profile = _profile_for_asset(_company(), pk)
    initial = {
        field: getattr(profile, field)
        for field in (
            "method", "posting_period", "start_rule", "stop_rule",
            "useful_life_months", "salvage_mode", "salvage_rate",
            "salvage_amount", "expected_total_units", "work_unit",
            "annual_posting_month",
        )
    }
    form = ProfileVersionForm(request.POST or None, actor=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        values = dict(form.cleaned_data)
        values.pop("confirm", None)
        effective_from = values.pop("effective_from")
        reason = values.pop("reason")
        try:
            clone_asset_depreciation_profile(
                actor=request.user,
                profile=profile,
                data=values,
                effective_from=effective_from,
                reason=reason,
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "已创建前瞻生效的新 折旧参数 版本，历史版本未改写。")
            return redirect("finance:asset-finance-detail", pk=pk)
    return render(request, "finance/form.html", {"form": form, "title": "新建折旧 折旧参数 版本"})


@login_required
def value_adjustment(request, pk):
    require_manage_finance(request.user)
    asset = get_object_or_404(
        Asset.objects.select_related("finance"), pk=pk, company=_company()
    )
    _require_depreciable_asset(asset)
    form = ValueAdjustmentForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data.copy()
        values.pop("confirm", None)
        try:
            create_value_adjustment(actor=request.user, asset=asset, request=request, **values)
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "资产价值调整已确认并追加留痕。")
            return redirect("finance:asset-finance-detail", pk=pk)
    return render(request, "finance/form.html", {"form": form, "title": "资产价值调整"})


@login_required
def value_adjustment_reverse(request, pk, adjustment_pk):
    require_manage_finance(request.user)
    asset = get_object_or_404(Asset, pk=pk, company=_company())
    adjustment = get_object_or_404(
        asset.value_adjustments, pk=adjustment_pk, company=asset.company
    )
    form = ReasonForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            reverse_value_adjustment(
                actor=request.user,
                adjustment=adjustment,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except ValidationError as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "原调整已通过精确反向记录冲销，原历史仍保留。")
            return redirect("finance:asset-finance-detail", pk=pk)
    return render(request, "finance/form.html", {"form": form, "title": "冲销价值调整"})


@login_required
def theoretical_run(request, pk):
    require_manage_finance(request.user)
    asset = get_object_or_404(
        Asset.objects.select_related("finance"), pk=pk, company=_company()
    )
    _require_depreciable_asset(asset)
    form = TheoreticalBusinessForm(request.POST or None, actor=request.user, asset=asset)
    if request.method == "POST" and form.is_valid():
        try:
            run = run_theoretical_depreciation(
                actor=request.user,
                asset=asset,
                as_of_date=form.cleaned_data["as_of_date"],
                parameters=form.cleaned_data["parameters_json"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except (ValidationError, ValueError) as exc:
            _service_error(form, exc)
        else:
            messages.success(request, "理论历史试算已保存为只读参考，不影响实际账面。")
            return redirect("finance:theoretical-detail", pk=pk, run_pk=run.pk)
    return render(request, "finance/form.html", {"form": form, "title": "理论历史折旧试算", "description": "参数默认引用资产现有财务资料，可调整后作参考试算；结果不改变实际账面。", "cancel_url": reverse("finance:asset-finance-detail", args=[asset.pk]), "submit_label": "生成参考试算"})


@login_required
def theoretical_detail(request, pk, run_pk):
    require_view_finance(request.user)
    company = _company()
    asset = get_object_or_404(scoped_finance_assets(request.user, company), pk=pk)
    run = get_object_or_404(asset.theoretical_depreciation_runs, pk=run_pk)
    return render(
        request,
        "finance/theoretical_detail.html",
        {"asset": asset, "run": run, "lines": run.lines.all()},
    )
