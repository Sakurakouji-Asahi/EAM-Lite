from __future__ import annotations

from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.permissions import current_company
from apps.operations.forms import (
    BackupDownloadAuthorizationForm,
    ManualBackupForm,
)
from apps.operations.models import BackupDownloadGrant, BackupSet
from apps.operations.permissions import (
    require_manage_backups,
    require_recent_backup_authentication,
)
from apps.operations.services import (
    create_backup_set,
    finish_download_grant,
    issue_download_grant,
    start_download_grant,
)


def _company_or_404():
    company = current_company()
    if company is None:
        from django.http import Http404

        raise Http404("尚未配置启用公司。")
    return company


def _backup_for_user(request, pk):
    company = _company_or_404()
    require_manage_backups(request.user)
    return get_object_or_404(
        BackupSet.objects.select_related("requested_by"), company=company, pk=pk
    )


def _audit_reauthentication_failure(request, company, object_id=""):
    write_business_audit_log(
        company=company,
        user=request.user,
        action="backup.reauthentication_failed",
        object_type="UserAuthentication",
        object_id=object_id or request.user.pk,
        old_data={},
        new_data={"result": "failed"},
        **request_audit_context(request),
    )


@never_cache
@login_required
def backup_list(request):
    company = _company_or_404()
    require_manage_backups(request.user)
    backups = BackupSet.objects.filter(company=company).select_related("requested_by")[:100]
    response = render(request, "operations/backup_list.html", {"backups": backups})
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def backup_create(request):
    company = _company_or_404()
    require_manage_backups(request.user)
    form = ManualBackupForm(
        request.POST or None,
        actor=request.user,
    )
    if request.method == "POST" and form.is_valid():
        require_recent_backup_authentication(request.user)
        try:
            backup = create_backup_set(
                actor=request.user,
                company=company,
                kind=BackupSet.Kind.MANUAL,
                idempotency_key=form.cleaned_data["idempotency_key"],
                passphrase=form.cleaned_data["backup_passphrase"],
                request=request,
            )
        except (ValidationError, PermissionDenied) as exc:
            form.add_error(
                None,
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else str(exc),
            )
        else:
            messages.success(
                request,
                "加密备份已生成。请妥善保存刚才输入的口令；系统不会保存或找回该口令。",
            )
            return redirect("operations:backup-detail", pk=backup.pk)
    elif request.method == "POST" and "current_password" in form.errors:
        _audit_reauthentication_failure(request, company)
    response = render(request, "operations/backup_create.html", {"form": form})
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@login_required
def backup_detail(request, pk):
    backup = _backup_for_user(request, pk)
    form = BackupDownloadAuthorizationForm(actor=request.user)
    response = render(
        request,
        "operations/backup_detail.html",
        {"backup": backup, "download_form": form},
    )
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@login_required
@require_POST
def backup_authorize_download(request, pk):
    backup = _backup_for_user(request, pk)
    require_recent_backup_authentication(request.user)
    form = BackupDownloadAuthorizationForm(request.POST, actor=request.user)
    if not form.is_valid():
        if "current_password" in form.errors:
            _audit_reauthentication_failure(request, backup.company, str(backup.pk))
        response = render(
            request,
            "operations/backup_detail.html",
            {"backup": backup, "download_form": form},
            status=400,
        )
        response["Cache-Control"] = "private, no-store"
        return response
    try:
        grant = issue_download_grant(
            actor=request.user,
            backup_set=backup,
            idempotency_key=form.cleaned_data["idempotency_key"],
            request=request,
        )
    except (ValidationError, PermissionDenied) as exc:
        form.add_error(
            None,
            "; ".join(exc.messages)
            if isinstance(exc, ValidationError)
            else str(exc),
        )
        response = render(
            request,
            "operations/backup_detail.html",
            {"backup": backup, "download_form": form},
            status=400,
        )
        response["Cache-Control"] = "private, no-store"
        return response
    response = render(
        request,
        "operations/backup_download_ready.html",
        {"backup": backup, "grant": grant},
    )
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@login_required
@require_POST
def backup_download(request, grant_pk):
    company = _company_or_404()
    require_manage_backups(request.user)
    grant = get_object_or_404(
        BackupDownloadGrant.objects.select_related("backup_set"),
        company=company,
        pk=grant_pk,
    )
    grant, package_path = start_download_grant(
        actor=request.user, grant=grant, request=request
    )

    def stream_file(path: Path, grant_id):
        succeeded = False
        failure = "下载连接在文件发送完成前中断。"
        try:
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            succeeded = True
            failure = ""
        except (OSError, GeneratorExit) as exc:
            failure = str(exc) or failure
            raise
        finally:
            finish_download_grant(
                grant_id=grant_id,
                succeeded=succeeded,
                reason=failure,
            )

    response = StreamingHttpResponse(
        stream_file(package_path, grant.pk),
        content_type="application/octet-stream",
    )
    response["Content-Length"] = str(package_path.stat().st_size)
    response["Content-Disposition"] = (
        f'attachment; filename="{grant.backup_set.backup_set_id}.eambak"'
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
