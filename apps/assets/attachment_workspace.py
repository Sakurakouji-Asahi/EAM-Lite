"""Attachment display metadata derived only from already-authorized rows."""

IMAGE_PREVIEW_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


def attachment_workspace(rows):
    roles = {}
    total_bytes = 0
    for row in rows:
        link = row["link"]
        row["can_preview"] = link.attachment.mime_type in IMAGE_PREVIEW_MIMES
        total_bytes += link.attachment.file_size
        bucket = roles.setdefault(link.role, {"value": link.role, "label": link.get_role_display(), "count": 0})
        bucket["count"] += 1
    return {"count": len(rows), "total_bytes": total_bytes, "roles": list(roles.values())}
