from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.encoding import escape_uri_path

from apps.imports.forms import ImportUploadForm
from apps.imports.services import (
    TEMPLATE_REGISTRY,
    build_template_workbook,
    confirm_import_batch,
    get_template_definition,
    upload_and_validate_import,
)
from apps.masterdata.permissions import current_company, require_manage_masterdata


def _company_or_404():
    company = current_company()
    if company is None:
        raise Http404("请先完成公司初始化。")
    return company


def _require(actor, import_type):
    require_manage_masterdata(actor, import_type)


@login_required
def import_home(request):
    company = _company_or_404()
    allowed = []
    for import_type, definition in TEMPLATE_REGISTRY.items():
        try:
            _require(request.user, import_type)
        except PermissionDenied:
            continue
        allowed.append(definition)
    if not allowed:
        raise PermissionDenied("您没有执行基础资料导入的权限。")
    return render(request, "imports/home.html", {"company": company, "definitions": allowed})


@login_required
def download_template(request, import_type):
    definition = get_template_definition(import_type)
    _require(request.user, import_type)
    response = HttpResponse(
        build_template_workbook(import_type),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f"attachment; filename={definition.import_type}-{definition.version}.xlsx"
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def upload_import(request, import_type):
    definition = get_template_definition(import_type)
    company = _company_or_404()
    _require(request.user, import_type)
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


@login_required
def batch_detail(request, pk):
    from apps.masterdata.models import ImportBatch

    company = _company_or_404()
    batch = get_object_or_404(
        ImportBatch.objects.select_related("file_attachment", "uploaded_by", "confirmed_by"),
        pk=pk,
        company=company,
    )
    _require(request.user, batch.import_type)
    return render(
        request,
        "imports/batch_detail.html",
        {"batch": batch, "rows": batch.rows.order_by("row_number"), "definition": get_template_definition(batch.import_type)},
    )


@login_required
def confirm_batch(request, pk):
    if request.method != "POST":
        raise Http404
    from apps.masterdata.models import ImportBatch

    company = _company_or_404()
    batch = get_object_or_404(ImportBatch, pk=pk, company=company)
    _require(request.user, batch.import_type)
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


@login_required
def download_source(request, pk):
    from apps.masterdata.models import ImportBatch

    company = _company_or_404()
    batch = get_object_or_404(
        ImportBatch.objects.select_related("file_attachment"), pk=pk, company=company
    )
    _require(request.user, batch.import_type)
    attachment = batch.file_attachment
    if not attachment.is_available or not default_storage.exists(attachment.storage_key):
        raise Http404("原文件不可用。")
    response = FileResponse(
        default_storage.open(attachment.storage_key, "rb"),
        as_attachment=True,
        filename=Path(attachment.safe_filename).name,
        content_type=attachment.mime_type,
    )
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + escape_uri_path(Path(attachment.safe_filename).name)
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response
