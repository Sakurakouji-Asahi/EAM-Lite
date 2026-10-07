"""Download an explicitly selected set of authorized asset attachments."""
import json
import re
import uuid
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.audit.services import request_audit_context, write_business_audit_log
from apps.masterdata.models import Attachment
from .access import asset_company_for_request, asset_or_404
from .models import AttachmentLink
from .permissions import can_view_attachment

BUNDLE_MAX_FILES = 20
BUNDLE_MAX_TOTAL_BYTES = 50 * 1024 * 1024


def _filename(value):
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:160] or "附件"


def _error(request, asset, message):
    return render(request, "assets/attachment_bundle_error.html", {"asset": asset, "bundle_error": message}, status=400)


class BundleTooLarge(Exception):
    pass


@login_required
@never_cache
@require_GET
def attachment_bundle(request, pk):
    company = asset_company_for_request()
    asset = asset_or_404(request.user, company, pk)
    raw_ids = request.GET.getlist("attachment")
    if not raw_ids or len(raw_ids) > BUNDLE_MAX_FILES:
        return _error(request, asset, f"请选择 1 至 {BUNDLE_MAX_FILES} 个附件后再下载。")
    try:
        ids = list(dict.fromkeys(uuid.UUID(value) for value in raw_ids))
    except (ValueError, AttributeError):
        return _error(request, asset, "附件选择无效，请返回资产重新勾选。")
    links = list(AttachmentLink.objects.filter(company=company, asset=asset, pk__in=ids)
                 .select_related("asset", "attachment").order_by("created_at", "pk"))
    if len(links) != len(ids):
        raise Http404("所选附件已变化，请返回资产重新核对。")
    for link in links:
        if not can_view_attachment(request.user, link):
            raise PermissionDenied("您没有下载所选附件的权限，请返回资产重新核对。")
        attachment = link.attachment
        if not attachment.is_available or attachment.malware_scan_status not in {
                Attachment.MalwareScanStatus.POLICY_LIMITED, Attachment.MalwareScanStatus.CLEAN}:
            raise Http404("所选附件当前不可用，请返回资产重新核对。")
    if sum(link.attachment.file_size for link in links) > BUNDLE_MAX_TOTAL_BYTES:
        return _error(request, asset, "所选附件总大小超过 50 MB，请分批下载。")

    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    manifest = ["资产附件清单", f"资产：{asset.asset_code or '草稿'} · {asset.asset_name}",
                f"文件数：{len(links)}", "文件名前的序号用于区分同名文件。", ""]
    copied = 0
    try:
        with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=1) as archive:
            for number, link in enumerate(links, start=1):
                attachment = link.attachment
                archive_name = f"{number:02d}_{_filename(attachment.safe_filename)}"
                with default_storage.open(attachment.storage_key, "rb") as source, archive.open(archive_name, "w") as target:
                    while chunk := source.read(64 * 1024):
                        copied += len(chunk)
                        if copied > BUNDLE_MAX_TOTAL_BYTES:
                            raise BundleTooLarge
                        target.write(chunk)
                manifest.extend([archive_name, f"原文件名：{json.dumps(attachment.safe_filename, ensure_ascii=False)}",
                    f"用途：{link.get_role_display()} · {link.get_security_class_display()}",
                    f"原文件大小：{attachment.file_size} 字节", ""])
            archive.writestr("附件清单.txt", "\n".join(manifest).encode("utf-8-sig"))
        # Only successful, complete bundles receive the existing per-file download audit.
        with transaction.atomic():
            for link in links:
                write_business_audit_log(company=company, user=request.user, action="asset_attachment_download",
                    object_type="AttachmentLink", object_id=link.pk, old_data={},
                    new_data={"asset": str(asset.pk), "role": link.role, "security_class": link.security_class},
                    **request_audit_context(request))
        output.seek(0)
        response = FileResponse(output, as_attachment=True,
            filename=f"asset-{_filename(asset.asset_code or str(asset.pk)[:8])}-attachments.zip", content_type="application/zip")
        response["X-Content-Type-Options"] = "nosniff"
        return response
    except BundleTooLarge:
        output.close()
        return _error(request, asset, "所选附件实际总大小超过 50 MB，请分批下载。")
    except OSError as exc:
        output.close()
        raise Http404("所选附件文件当前不可用，请返回资产重新核对。") from exc
    except Exception:
        output.close()
        raise
