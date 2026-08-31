from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.encoding import escape_uri_path
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.imports.forms import ImportUploadForm
from apps.imports.services import (
    TEMPLATE_REGISTRY,
    build_template_workbook,
    cancel_import_batch,
    confirm_import_batch,
    get_template_definition,
    require_import_permission,
    upload_and_validate_import,
)
from apps.masterdata.permissions import current_company, role_names_for


def _company_or_404():
    company = current_company()
    if company is None:
        raise Http404("请先完成公司初始化。")
    return company


def _definition_or_404(import_type, *, company=None):
    try:
        return get_template_definition(import_type, company=company)
    except ValidationError as exc:
        raise Http404("不支持的导入类型。") from exc


def _require(actor, import_type, *, company=None):
    require_import_permission(actor, import_type, company=company)


def _require_batch(actor, batch):
    """Apply the import gate and the asset-initialization object boundary."""

    _require(actor, batch.import_type, company=batch.company)
    if batch.import_type == "item_master":
        roles = role_names_for(actor)
        if roles.intersection({"system_admin", "finance", "warehouse"}):
            return
        if batch.uploaded_by_id != actor.pk:
            raise PermissionDenied(
                "equipment 只能查看本人上传的低值物品档案导入批次。"
            )
        return
    if batch.import_type != "asset_initialization":
        return
    # Asset workbooks can contain F1 fields.  Finance may inspect company-wide
    # initialization evidence; every physical creator revisits only batches
    # they uploaded.  Concrete rows are also rechecked against current scope by
    # the Service during validation and confirmation.
    roles = role_names_for(actor)
    if "finance" not in roles and batch.uploaded_by_id != actor.pk:
        raise PermissionDenied("您没有查看此资产初始化导入批次的权限。")


@never_cache
@login_required
@require_GET
def import_home(request):
    company = _company_or_404()
    allowed = []
    for import_type in TEMPLATE_REGISTRY:
        try:
            _require(request.user, import_type, company=company)
        except PermissionDenied:
            continue
        allowed.append(_definition_or_404(import_type, company=company))
    if not allowed:
        raise PermissionDenied("您没有执行导入的权限。")
    return render(request, "imports/home.html", {"company": company, "definitions": allowed})


@never_cache
@login_required
@require_GET
def download_template(request, import_type):
    company = _company_or_404()
    definition = _definition_or_404(import_type, company=company)
    _require(request.user, import_type, company=company)
    response = HttpResponse(
        build_template_workbook(import_type, company=company),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f"attachment; filename={definition.import_type}-{definition.version}.xlsx"
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def upload_import(request, import_type):
    company = _company_or_404()
    definition = _definition_or_404(import_type, company=company)
    _require(request.user, import_type, company=company)
    form = ImportUploadForm(request.POST or None, request.FILES or None, import_type=import_type)
    if request.method == "POST" and form.is_valid():
        try:
            batch = upload_and_validate_import(
                actor=request.user,
                company=company,
                import_type=import_type,
                uploaded_file=form.cleaned_data["file"],
                idempotency_key=form.cleaned_data["idempotency_key"],
                request=request,
            )
        except ValidationError as exc:
            form.add_error("file", exc)
        else:
            return redirect("imports:batch_detail", pk=batch.pk)
    return render(
        request,
        "imports/upload.html",
        {"form": form, "definition": definition, "company": company},
    )


@never_cache
@login_required
@require_GET
def batch_detail(request, pk):
    from apps.masterdata.models import ImportBatch

    company = _company_or_404()
    batch = get_object_or_404(
        ImportBatch.objects.select_related("file_attachment", "uploaded_by", "confirmed_by"),
        pk=pk,
        company=company,
    )
    _require_batch(request.user, batch)
    if set(request.GET) - {"page"}:
        return HttpResponse("包含不支持的导入预览参数。", status=400)
    paginator = Paginator(batch.rows.order_by("row_number"), 50)
    try:
        page_obj = paginator.page(request.GET.get("page", "1"))
    except (PageNotAnInteger, EmptyPage):
        return HttpResponse("页码无效。", status=400)
    return render(
        request,
        "imports/batch_detail.html",
        {
            "batch": batch,
            "rows": page_obj.object_list,
            "page_obj": page_obj,
            "definition": _definition_or_404(batch.import_type, company=company),
            "is_asset_initialization": batch.import_type == "asset_initialization",
            "is_item_master": batch.import_type == "item_master",
            "is_opening_stock": batch.import_type == "opening_stock",
            "is_opening_custody": batch.import_type == "opening_custody",
        },
    )


@never_cache
@login_required
def confirm_batch(request, pk):
    if request.method != "POST":
        raise Http404
    from apps.masterdata.models import ImportBatch

    company = _company_or_404()
    batch = get_object_or_404(ImportBatch, pk=pk, company=company)
    _require_batch(request.user, batch)
    if request.POST.get("confirm") != "1":
        messages.error(request, "请勾选确认后再执行整批导入。")
        return redirect("imports:batch_detail", pk=batch.pk)
    try:
        confirm_import_batch(actor=request.user, batch=batch, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "导入已整批确认成功。")
    return redirect("imports:batch_detail", pk=batch.pk)


@never_cache
@login_required
def cancel_batch(request, pk):
    if request.method != "POST":
        raise Http404
    from apps.masterdata.models import ImportBatch

    company = _company_or_404()
    batch = get_object_or_404(ImportBatch, pk=pk, company=company)
    _require_batch(request.user, batch)
    try:
        cancel_import_batch(
            actor=request.user,
            batch=batch,
            reason=request.POST.get("reason"),
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "导入批次已取消，未创建业务数据。")
    return redirect("imports:batch_detail", pk=batch.pk)


@never_cache
@login_required
@require_GET
def download_source(request, pk):
    from apps.masterdata.models import ImportBatch

    company = _company_or_404()
    batch = get_object_or_404(
        ImportBatch.objects.select_related("file_attachment"), pk=pk, company=company
    )
    _require_batch(request.user, batch)
    attachment = batch.file_attachment
    if not attachment.is_available or not default_storage.exists(attachment.storage_key):
        raise Http404("原文件不可用。")
    source = default_storage.open(attachment.storage_key, "rb")
    try:
        write_business_audit_log(
            company=batch.company,
            user=request.user,
            action="import_source_download",
            object_type="ImportBatch",
            object_id=batch.pk,
            old_data={},
            new_data={
                "import_type": batch.import_type,
                "file_sha256": batch.file_sha256,
            },
            **request_audit_context(request),
        )
    except Exception:
        source.close()
        raise
    response = FileResponse(
        source,
        as_attachment=True,
        filename=Path(attachment.safe_filename).name,
        content_type=attachment.mime_type,
    )
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + escape_uri_path(Path(attachment.safe_filename).name)
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response
