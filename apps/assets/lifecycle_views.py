"""Server-rendered Sprint 7 lifecycle and disposal views."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.encoding import escape_uri_path

from apps.assets.lifecycle_forms import (
    ArchiveAssetForm,
    AssetLoanForm,
    AssetLoanReturnForm,
    AssetStatusForm,
    AssetTransferForm,
    CorrectAssetCodeForm,
    DisposalActualDetailsForm,
    DisposalAttachmentUploadForm,
    DisposalAttachmentVoidForm,
    DisposalCompleteForm,
    DisposalFinanceLockForm,
    DisposalInitiateForm,
    DisposalReversalForm,
    ReasonForm,
)
from apps.assets.lifecycle_permissions import (
    can_lifecycle_action,
    can_manage_disposal_attachment,
    can_view_disposal_attachment,
    can_view_disposal_financial_fields,
    scoped_disposals,
    scoped_lifecycle_assets,
)
from apps.assets.lifecycle_services import (
    activate_asset,
    archive_asset,
    cancel_disposal,
    complete_asset_repair,
    complete_disposal,
    correct_asset_code,
    initiate_disposal,
    loan_asset,
    lock_disposal_financial_snapshot,
    record_disposal_actual_details,
    restore_asset_visibility,
    return_loan,
    reverse_disposal,
    send_asset_for_repair,
    set_asset_idle,
    transfer_asset,
    upload_disposal_attachment,
    void_disposal_attachment,
)
from apps.assets.models import Asset, AssetDisposal, AssetLoan, AttachmentLink
from apps.masterdata.models import Attachment, InitializationSetting
from apps.masterdata.permissions import current_company


def _company():
    company = current_company()
    if company is None or not company.is_active:
        raise Http404("尚未配置启用公司。")
    if not InitializationSetting.objects.filter(
        company=company, initialization_completed=True
    ).exists():
        raise PermissionDenied("系统初始化尚未完成。")
    return company


def _asset(request, pk):
    company = _company()
    return get_object_or_404(
        scoped_lifecycle_assets(
            request.user,
            company,
            Asset.objects.select_related(
                "company", "category", "department", "responsible_employee", "location"
            ),
            include_archived=True,
        ),
        pk=pk,
    )


def _disposal(request, pk):
    company = _company()
    return get_object_or_404(
        scoped_disposals(
            request.user,
            company,
            AssetDisposal.objects.select_related(
                "company", "asset", "asset__department",
                "asset__responsible_employee", "asset__location",
                "initiated_by", "handled_by", "finance_locked_by",
                "confirmed_by", "cancelled_by",
            ),
        ),
        pk=pk,
    )


def _form_error(form, exc):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for error in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, error)


def _action_form(
    request,
    *,
    asset,
    action,
    form_class,
    title,
    description,
    callback,
    button_label="确认执行",
    button_class="primary",
    initial=None,
    enctype=False,
):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    form_args = [request.POST or None]
    if enctype:
        form_args.append(request.FILES or None)
    form = form_class(
        *form_args,
        actor=request.user,
        asset=asset,
        action=action,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            result = callback(form.cleaned_data)
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, PermissionDenied):
                raise
            _form_error(form, exc)
        else:
            messages.success(request, f"{title}已完成。")
            if isinstance(result, AssetDisposal):
                return redirect("assets:disposal-detail", pk=result.pk)
            return redirect("assets:asset-detail", pk=asset.pk)
    return render(
        request,
        "assets/lifecycle_action.html",
        {
            "asset": asset,
            "form": form,
            "title": title,
            "description": description,
            "button_label": button_label,
            "button_class": button_class,
            "multipart": enctype,
        },
    )


@login_required
def asset_transfer(request, pk):
    asset = _asset(request, pk)
    return _action_form(
        request, asset=asset, action="transfer", form_class=AssetTransferForm,
        title="资产调拨 / 转交",
        description="当前部门、责任人和位置已预填。只修改实际变化的项目，保存后系统会保留完整变动记录。",
        callback=lambda data: transfer_asset(
            actor=request.user, asset=asset,
            to_department=data["to_department"],
            to_responsible_employee=data["to_responsible_employee"],
            to_location=data["to_location"], effective_at=data["effective_at"],
            reason=data["reason"], remark=data["remark"],
            idempotency_key=data["idempotency_key"],
            expected_department_id=data["expected_department_id"],
            expected_responsible_employee_id=data["expected_responsible_employee_id"],
            expected_location_id=data["expected_location_id"],
            expected_status=data["expected_status"], request=request,
        ),
        initial={"effective_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
    )


def _status_action(request, pk, *, action, title, description, callback):
    asset = _asset(request, pk)
    return _action_form(
        request, asset=asset, action=action, form_class=AssetStatusForm,
        title=title, description=description,
        callback=lambda data: callback(asset, data),
        initial={"effective_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
    )


@login_required
def asset_idle(request, pk):
    return _status_action(
        request, pk, action="idle", title="转为闲置", description="在用资产将转为闲置并追加状态历史。",
        callback=lambda asset, data: set_asset_idle(
            actor=request.user, asset=asset, effective_at=data["effective_at"],
            reason=data["reason"], remark=data["remark"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def asset_activate(request, pk):
    return _status_action(
        request, pk, action="activate", title="启用资产", description="闲置资产将恢复在用并追加状态历史。",
        callback=lambda asset, data: activate_asset(
            actor=request.user, asset=asset, effective_at=data["effective_at"],
            reason=data["reason"], remark=data["remark"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def asset_repair_start(request, pk):
    return _status_action(
        request, pk, action="repair_start", title="送修", description="V1 仅记录维修占用状态和历史，不创建维修工单或费用。",
        callback=lambda asset, data: send_asset_for_repair(
            actor=request.user, asset=asset, effective_at=data["effective_at"],
            reason=data["reason"], remark=data["remark"],
            expected_status=data["expected_status"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def asset_repair_complete(request, pk):
    return _status_action(
        request, pk, action="repair_complete", title="维修完成", description="资产将恢复最近一次送修前的在用或闲置状态。",
        callback=lambda asset, data: complete_asset_repair(
            actor=request.user, asset=asset, effective_at=data["effective_at"],
            result=data["reason"], idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def asset_loan(request, pk):
    asset = _asset(request, pk)
    return _action_form(
        request, asset=asset, action="loan", form_class=AssetLoanForm,
        title="资产借出", description="借出会保存结构化借用方、预计归还日和一对一变动历史。",
        callback=lambda data: loan_asset(
            actor=request.user, asset=asset, borrower_type=data["borrower_type"],
            borrower_employee=data.get("borrower_employee"),
            borrower_name=data.get("borrower_name", ""),
            borrower_organization=data.get("borrower_organization", ""),
            loan_date=data["loan_date"], expected_return_date=data["expected_return_date"],
            handled_by=request.user, reason=data["reason"], remark=data["remark"],
            expected_status=data["expected_status"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def asset_loan_return(request, pk):
    asset = _asset(request, pk)
    loan = get_object_or_404(AssetLoan.objects.select_related("asset"), asset=asset, status="active")
    return _action_form(
        request, asset=asset, action="loan_return", form_class=AssetLoanReturnForm,
        title="资产归还", description="归还必须指定接收人、责任归属、叶级位置和归还后状态。",
        callback=lambda data: return_loan(
            actor=request.user, loan=loan, returned_at=data["returned_at"],
            received_by_employee=data["received_by_employee"],
            return_department=data["return_department"],
            return_responsible_employee=data["return_responsible_employee"],
            return_location=data["return_location"],
            return_asset_status=data["return_asset_status"], remark=data["remark"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def asset_code_correct(request, pk):
    asset = _asset(request, pk)
    return _action_form(
        request, asset=asset, action="code_correction", form_class=CorrectAssetCodeForm,
        title="正式编号修正",
        description="新编号通过编码引擎正式发号；旧号永久占用，旧 Token 立即失效，新标签进入待打印。",
        button_class="danger",
        callback=lambda data: correct_asset_code(
            actor=request.user, asset=asset, effective_date=data["effective_date"],
            coding_scheme=data.get("coding_scheme"), reason=data["reason"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


def _record_action(request, pk, *, restore=False):
    asset = _asset(request, pk)
    action = "restore_visibility" if restore else "archive"
    return _action_form(
        request, asset=asset, action=action, form_class=ArchiveAssetForm,
        title="恢复归档资产显示" if restore else "归档终态资产",
        description="只改变记录显示状态，不改变终态、编号、二维码、财务或历史。",
        button_class="danger" if not restore else "primary",
        callback=lambda data: (
            restore_asset_visibility if restore else archive_asset
        )(
            actor=request.user, asset=asset, reason=data["reason"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def asset_archive(request, pk):
    return _record_action(request, pk)


@login_required
def asset_restore(request, pk):
    return _record_action(request, pk, restore=True)


@login_required
def disposal_start(request, pk):
    asset = _asset(request, pk)
    return _action_form(
        request, asset=asset, action="disposal_start", form_class=DisposalInitiateForm,
        title="发起资产处置", description="拟处置日期只用于计划，不会锁定财务快照或完成处置。",
        button_class="danger",
        callback=lambda data: initiate_disposal(
            actor=request.user, asset=asset, disposal_type=data["disposal_type"],
            application_date=data["application_date"],
            planned_disposal_date=data["planned_disposal_date"],
            reason=data["reason"], description=data["description"],
            recipient_name=data["recipient_name"], expected_status=data["expected_status"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def disposal_detail(request, pk):
    disposal = _disposal(request, pk)
    can_financial = can_view_disposal_financial_fields(request.user, disposal)
    links = []
    for link in disposal.attachment_links.select_related("attachment", "created_by").order_by("created_at"):
        if can_view_disposal_attachment(request.user, link):
            links.append({
                "link": link,
                "can_void": can_manage_disposal_attachment(
                    request.user, disposal, security_class=link.security_class
                ),
            })
    return render(request, "assets/disposal_detail.html", {
        "asset": disposal.asset,
        "disposal": disposal,
        "can_financial": can_financial,
        "attachments": links,
        "actions": {
            "actual": can_lifecycle_action(request.user, disposal.asset, "disposal_actual_details") and disposal.status == "draft",
            "finance_lock": can_lifecycle_action(request.user, disposal.asset, "disposal_finance_lock") and disposal.status == "draft" and disposal.actual_disposal_date is not None,
            "cancel": can_lifecycle_action(request.user, disposal.asset, "disposal_cancel") and disposal.status in {"draft", "finance_locked"},
            "complete": can_lifecycle_action(request.user, disposal.asset, "disposal_complete") and disposal.status == "finance_locked",
            "reverse": can_lifecycle_action(request.user, disposal.asset, "disposal_reversal") and disposal.status == "confirmed" and disposal.asset.record_status == "active",
            "upload_a0": can_manage_disposal_attachment(request.user, disposal, security_class="A0"),
            "upload_a1": can_manage_disposal_attachment(request.user, disposal, security_class="A1"),
        },
    })


def _disposal_form(request, pk, *, action, form_class, title, description, callback, button_class="primary"):
    disposal = _disposal(request, pk)
    return _action_form(
        request, asset=disposal.asset, action=action, form_class=form_class,
        title=title, description=description, button_class=button_class,
        callback=lambda data: callback(disposal, data),
    )


@login_required
def disposal_actual(request, pk):
    return _disposal_form(
        request, pk, action="disposal_actual_details", form_class=DisposalActualDetailsForm,
        title="登记实际处置信息", description="实际日期不得早于申请日或晚于当前上海业务日。",
        callback=lambda disposal, data: record_disposal_actual_details(
            actor=request.user, disposal=disposal,
            actual_disposal_date=data["actual_disposal_date"],
            recipient_name=data["recipient_name"], handled_by=request.user,
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def disposal_finance_lock(request, pk):
    return _disposal_form(
        request, pk, action="disposal_finance_lock", form_class=DisposalFinanceLockForm,
        title="锁定处置财务快照",
        description="金额只从实际日期截止的已确认财务真源生成；缺少应计期间将阻断。",
        button_class="danger",
        callback=lambda disposal, data: lock_disposal_financial_snapshot(
            actor=request.user, disposal=disposal, disposal_income=data["disposal_income"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def disposal_cancel(request, pk):
    return _disposal_form(
        request, pk, action="disposal_cancel", form_class=ReasonForm,
        title="取消未完成处置", description="原处置、快照和附件全部保留，资产恢复处置前状态。",
        button_class="danger",
        callback=lambda disposal, data: cancel_disposal(
            actor=request.user, disposal=disposal, reason=data["reason"],
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def disposal_complete(request, pk):
    return _disposal_form(
        request, pk, action="disposal_complete", form_class=DisposalCompleteForm,
        title="完成资产处置", description="完成后进入永久处置终态，并按实际日期停止未来折旧。",
        button_class="danger",
        callback=lambda disposal, data: complete_disposal(
            actor=request.user, disposal=disposal,
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def disposal_reverse(request, pk):
    return _disposal_form(
        request, pk, action="disposal_reversal", form_class=DisposalReversalForm,
        title="冲销终态处置",
        description="仅在不存在后续变动、Profile、人工折旧事件或确认分录时允许；全部证据永久保留。",
        button_class="danger",
        callback=lambda disposal, data: reverse_disposal(
            actor=request.user, disposal=disposal, reason=data["reason"],
            replacement_responsible_employee=data.get("replacement_responsible_employee"),
            idempotency_key=data["idempotency_key"], request=request,
        ),
    )


@login_required
def disposal_attachment_upload(request, pk):
    disposal = _disposal(request, pk)
    return _action_form(
        request, asset=disposal.asset, action="disposal_complete",
        form_class=DisposalAttachmentUploadForm, title="上传处置证据",
        description="文件保存在私有存储，下载必须重新通过处置对象权限。",
        enctype=True,
        callback=lambda data: upload_disposal_attachment(
            actor=request.user, disposal=disposal,
            uploaded_file=data["uploaded_file"], security_class=data["security_class"],
            request=request,
        ),
    )


@login_required
def disposal_attachment_download(request, disposal_pk, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    disposal = _disposal(request, disposal_pk)
    link = get_object_or_404(
        AttachmentLink._base_manager.select_related("attachment", "asset_disposal"),
        pk=pk, company=disposal.company, asset_disposal=disposal,
        status=AttachmentLink.Status.ACTIVE,
        attachment__is_available=True,
        attachment__malware_scan_status__in=(
            Attachment.MalwareScanStatus.POLICY_LIMITED,
            Attachment.MalwareScanStatus.CLEAN,
        ),
    )
    if not can_view_disposal_attachment(request.user, link):
        raise PermissionDenied("您没有下载此处置附件的权限。")
    attachment = link.attachment
    if not default_storage.exists(attachment.storage_key):
        raise Http404("附件文件当前不可用。")
    response = FileResponse(
        default_storage.open(attachment.storage_key, "rb"),
        content_type=attachment.mime_type,
    )
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + escape_uri_path(attachment.safe_filename)
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def disposal_attachment_void(request, disposal_pk, pk):
    disposal = _disposal(request, disposal_pk)
    link = get_object_or_404(
        AttachmentLink._base_manager.select_related("attachment", "asset_disposal"),
        pk=pk, company=disposal.company, asset_disposal=disposal,
    )
    return _action_form(
        request, asset=disposal.asset, action="disposal_complete",
        form_class=DisposalAttachmentVoidForm, title="作废处置附件",
        description=f"附件：{link.attachment.safe_filename}。作废只改变业务关联状态，不删除历史证据。",
        button_class="danger",
        callback=lambda data: void_disposal_attachment(
            actor=request.user, link=link, reason=data["reason"], request=request,
        ),
    )
